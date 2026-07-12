"""監査レポート(audit-duplicates出力)の「自動統合可能」分類だけを対象に、
段階的(カナリア→バッチ)にdedupe-cardsを適用するオーケストレーション。

安全設計:
- レポートの分類が "auto" のグループのみを対象にする(それ以外は一切扱わない)。
- 適用直前に必ず対象カード名を現在のNotion状態で再監査し、
  分類が変わっていないか・ページ構成(page_id集合)が変わっていないかを照合する。
  どちらかが変化していれば、そのグループはスキップする(処理は止めない)。
- 実際の統合(代表選択・属性マージ・書き込み)は既存の dedupe_cards.py の
  build_dedupe_plan/execute_dedupe_plan をそのまま再利用する(ロジックの二重化を避ける)。
- 削除・ゴミ箱移動のAPIは一切呼ばない。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services.audit_duplicates import (
    CATEGORY_AUTO,
    ExclusionList,
    _page_summary,
    audit_duplicate_groups,
)
from mtg_notion_manager.services.dedupe_cards import build_dedupe_plan, execute_dedupe_plan

STATUS_PLANNED = "planned"
STATUS_APPLIED = "applied"
STATUS_SKIPPED_STALE = "skipped_stale"
STATUS_SKIPPED_NOT_DUPLICATE = "skipped_no_longer_duplicate"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ReportGroup:
    card_name: str
    duplicate_count: int
    merged_deck_relation_count: int
    recommended_representative_id: str | None
    page_ids: list[str]


def load_audit_report(path: Path, classification: str = CATEGORY_AUTO) -> list[ReportGroup]:
    """監査レポートJSONを読み込み、指定分類のグループだけを抽出する。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        ReportGroup(
            card_name=item["card_name"],
            duplicate_count=item["duplicate_count"],
            merged_deck_relation_count=item["merged_deck_relation_count"],
            recommended_representative_id=item.get("recommended_representative_id"),
            page_ids=[p["page_id"] for p in item.get("pages", [])],
        )
        for item in data
        if item["category"] == classification
    ]


def select_target_groups(
    groups: list[ReportGroup], limit: int | None = None, offset: int = 0
) -> list[ReportGroup]:
    """リスクが低い順(重複件数少 → 採用デッキ件数少)にソートし、offset/limitで切り出す。

    重複件数2件・採用デッキ0件に近いグループほど変更が小さく安全なため先頭に来る
    (カナリア適用で最初にlimitを絞るだけで自然に低リスク優先になる)。
    """
    ranked = sorted(groups, key=lambda g: (g.duplicate_count, g.merged_deck_relation_count))
    sliced = ranked[offset:]
    if limit is not None:
        sliced = sliced[:limit]
    return sliced


@dataclass(frozen=True)
class GroupApplyOutcome:
    card_name: str
    status: str
    reason: str = ""
    representative_page_id: str | None = None
    merged_page_ids: list[str] = field(default_factory=list)
    error: str | None = None
    before_snapshot: list[dict] = field(default_factory=list)
    after_snapshot: dict | None = None


def apply_dedupe_batch(
    repo: DedupeRepository,
    target_groups: list[ReportGroup],
    apply: bool,
    exclusions: ExclusionList | None = None,
    note: str = "",
) -> list[GroupApplyOutcome]:
    """対象グループを1件ずつ鮮度チェックしてから適用(またはdry-run計画表示)する。"""
    exclusions = exclusions or ExclusionList()
    outcomes: list[GroupApplyOutcome] = []

    for group in target_groups:
        outcomes.append(_process_one_group(repo, group, apply=apply, exclusions=exclusions))

    return outcomes


def _process_one_group(
    repo: DedupeRepository, group: ReportGroup, apply: bool, exclusions: ExclusionList
) -> GroupApplyOutcome:
    fresh_audits = audit_duplicate_groups(repo, card_name=group.card_name, exclusions=exclusions)

    if not fresh_audits:
        return GroupApplyOutcome(
            card_name=group.card_name,
            status=STATUS_SKIPPED_NOT_DUPLICATE,
            reason="現在は重複していません(既に統合済み、または単一レコードになっています)",
        )

    fresh = fresh_audits[0]

    if fresh.category != CATEGORY_AUTO:
        return GroupApplyOutcome(
            card_name=group.card_name,
            status=STATUS_SKIPPED_STALE,
            reason=f"現在の分類が '{fresh.category}' に変化しています(監査時: auto)",
        )

    fresh_page_ids = {p["id"] for p in fresh.pages}
    report_page_ids = set(group.page_ids)
    if fresh_page_ids != report_page_ids:
        return GroupApplyOutcome(
            card_name=group.card_name,
            status=STATUS_SKIPPED_STALE,
            reason=(
                "ページ構成が監査時と異なります"
                f"(監査時: {sorted(report_page_ids)}, 現在: {sorted(fresh_page_ids)})"
            ),
        )

    before_snapshot = [_page_summary(p) for p in fresh.pages]

    plan = build_dedupe_plan(repo, card_name=group.card_name)
    if not plan.merge_plans:
        return GroupApplyOutcome(
            card_name=group.card_name,
            status=STATUS_SKIPPED_NOT_DUPLICATE,
            reason="統合計画を作成できませんでした(直前で状態が変化した可能性)",
            before_snapshot=before_snapshot,
        )
    merge_plan = plan.merge_plans[0]

    after_snapshot = {
        "representative_page_id": merge_plan.representative_page_id,
        "owned": merge_plan.owned,
        "quantity": merge_plan.quantity,
        "english_name": merge_plan.english_name,
        "deck_relation_count": len(merge_plan.merged_deck_relation_ids),
        "attributes": merge_plan.single_valued_attributes,
    }

    if not apply:
        return GroupApplyOutcome(
            card_name=group.card_name,
            status=STATUS_PLANNED,
            representative_page_id=merge_plan.representative_page_id,
            merged_page_ids=[p["id"] for p in merge_plan.duplicate_pages],
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )

    result = execute_dedupe_plan(plan, repo)
    group_result = result.results[0]

    if group_result.error:
        return GroupApplyOutcome(
            card_name=group.card_name,
            status=STATUS_FAILED,
            error=group_result.error,
            representative_page_id=group_result.representative_page_id,
            before_snapshot=before_snapshot,
        )

    return GroupApplyOutcome(
        card_name=group.card_name,
        status=STATUS_APPLIED,
        representative_page_id=group_result.representative_page_id,
        merged_page_ids=group_result.duplicate_page_ids_marked,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
    )


@dataclass(frozen=True)
class ApplyLogPaths:
    json_path: Path


def write_apply_log(
    outcomes: list[GroupApplyOutcome],
    audit_report_path: str,
    output_dir: Path,
    applied: bool,
    timestamp: str | None = None,
) -> ApplyLogPaths:
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"dedupe-apply-{timestamp}.json"

    api_update_count = sum(
        1 + len(o.merged_page_ids) for o in outcomes if o.status == STATUS_APPLIED
    )

    log = {
        "executed_at": datetime.datetime.now().isoformat(),
        "audit_report": str(audit_report_path),
        "applied": applied,
        "summary": {
            "total": len(outcomes),
            "applied": sum(1 for o in outcomes if o.status == STATUS_APPLIED),
            "planned": sum(1 for o in outcomes if o.status == STATUS_PLANNED),
            "skipped_stale": sum(1 for o in outcomes if o.status == STATUS_SKIPPED_STALE),
            "skipped_no_longer_duplicate": sum(
                1 for o in outcomes if o.status == STATUS_SKIPPED_NOT_DUPLICATE
            ),
            "failed": sum(1 for o in outcomes if o.status == STATUS_FAILED),
        },
        "api_update_count": api_update_count,
        "delete_count": 0,
        "groups": [
            {
                "card_name": o.card_name,
                "status": o.status,
                "reason": o.reason,
                "representative_page_id": o.representative_page_id,
                "merged_page_ids": o.merged_page_ids,
                "error": o.error,
                "before_snapshot": o.before_snapshot,
                "after_snapshot": o.after_snapshot,
            }
            for o in outcomes
        ],
    }

    json_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return ApplyLogPaths(json_path=json_path)
