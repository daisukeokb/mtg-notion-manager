"""要確認だった「価格・販売リンク差異のみ」グループ(A分類)と「血染めのぬかるみ」
(手動代表指定)を、安全に段階適用するオーケストレーション。

安全設計:
- review-duplicate-conflicts のレポート(price_only / manual_representative)のみを対象にする。
- 適用直前に必ず review_duplicate_conflicts() で対象カード名を再監査し、
  分類・ページ構成(page_id集合)がレポート作成時から変化していないか照合する。
  変化していればそのグループはスキップする(処理は止めない)。
- 実際の統合(代表選択・属性マージ・書き込み)は既存の dedupe_cards.py の
  build_dedupe_plan/execute_dedupe_plan をそのまま再利用する(ロジックの二重化を避ける)。
- 代表ページの販売価格・販売リンクは一切上書きしない
  (dedupe_cards.build_representative_update が最初から含めていないため)。
- 削除・ゴミ箱移動のAPIは一切呼ばない。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

from mtg_notion_manager.notion.dedupe_repository import (
    DECKS_RELATION_PROPERTY,
    ENGLISH_NAME_PROPERTY,
    MERGED_PROPERTY,
    NOTE_PROPERTY,
    OWNED_PROPERTY,
    QUANTITY_PROPERTY,
    TITLE_PROPERTY,
    DedupeRepository,
)
from mtg_notion_manager.services.audit_duplicates import (
    ExclusionList,
    _multi_select_value,
    _page_summary,
    _plain_text,
    _single_value,
    _url_value,
)
from mtg_notion_manager.services.dedupe_cards import (
    build_dedupe_plan,
    build_merge_history_note,
    execute_dedupe_plan,
)
from mtg_notion_manager.services.review_duplicate_conflicts import (
    CATEGORY_MANUAL,
    CATEGORY_PRICE_ONLY,
    review_duplicate_conflicts,
)

STATUS_APPLIED = "applied"
STATUS_PLANNED = "planned"
STATUS_SKIPPED_STALE = "skipped_stale"
STATUS_SKIPPED_NOT_DUPLICATE = "skipped_no_longer_duplicate"
STATUS_FAILED = "failed"

TARGET_CATEGORIES = (CATEGORY_PRICE_ONLY, CATEGORY_MANUAL)


# --- バックアップ ------------------------------------------------------------


@dataclass(frozen=True)
class BackupResult:
    path: Path
    count: int
    verified_count: int

    @property
    def verified(self) -> bool:
        return self.count == self.verified_count


def _backup_page_summary(repo: DedupeRepository, page: dict) -> dict:
    return {
        "page_id": page["id"],
        "page_url": page.get("url", ""),
        "card_name": _plain_text(page, TITLE_PROPERTY),
        "english_name": _plain_text(page, ENGLISH_NAME_PROPERTY),
        "owned": bool(page.get("properties", {}).get(OWNED_PROPERTY, {}).get("checkbox")),
        "quantity": page.get("properties", {}).get(QUANTITY_PROPERTY, {}).get("number"),
        "merged": bool(page.get("properties", {}).get(MERGED_PROPERTY, {}).get("checkbox")),
        "deck_relation_ids": repo.get_full_relation_ids(page, DECKS_RELATION_PROPERTY),
        "commander_tags": _multi_select_value(page, "統率者"),
        "type": _single_value(page, "タイプ"),
        "symbols": _multi_select_value(page, "シンボル"),
        "roles": _multi_select_value(page, "役割（標準）"),
        "price": page.get("properties", {}).get("販売価格", {}).get("number"),
        "link": _url_value(page, "販売リンク"),
        "note": _plain_text(page, NOTE_PROPERTY),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
    }


def backup_card_db(
    repo: DedupeRepository, output_dir: Path, timestamp: str | None = None
) -> BackupResult:
    """MTGカードDB全件をJSONへバックアップし、独立した再取得で件数を検証する。"""
    repo.load()
    pages = repo.all_pages()
    summaries = [_backup_page_summary(repo, p) for p in pages]

    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"card-db-before-price-dedupe-{timestamp}.json"
    path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    verify_pages = repo.fetch_all_pages_fresh()
    return BackupResult(path=path, count=len(summaries), verified_count=len(verify_pages))


# --- 対象グループの読み込み ---------------------------------------------------


@dataclass(frozen=True)
class PriceLinkTargetGroup:
    card_name: str
    review_category: str
    page_ids: list[str]
    prices: list[float]
    links: list[str]
    merged_deck_relation_count: int
    representative_page_id: str | None = None

    @property
    def is_price_diff_only(self) -> bool:
        return len(self.prices) > 1 and len(self.links) <= 1

    @property
    def is_link_diff_only(self) -> bool:
        return len(self.links) > 1 and len(self.prices) <= 1


def load_price_link_targets(
    path: Path,
    manual_representative_overrides: dict[str, str] | None = None,
) -> list[PriceLinkTargetGroup]:
    """review-duplicate-conflicts のJSONから price_only / manual_representative を抽出する。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    overrides = manual_representative_overrides or {}
    targets: list[PriceLinkTargetGroup] = []
    for item in data:
        category = item["review_category"]
        if category not in TARGET_CATEGORIES:
            continue
        representative_page_id = None
        if category == CATEGORY_MANUAL:
            representative_page_id = overrides.get(item["card_name"])
        targets.append(
            PriceLinkTargetGroup(
                card_name=item["card_name"],
                review_category=category,
                page_ids=[p["page_id"] for p in item.get("pages", [])],
                prices=list(item.get("prices", [])),
                links=list(item.get("links", [])),
                merged_deck_relation_count=item.get("merged_deck_relation_count", 0),
                representative_page_id=representative_page_id,
            )
        )
    return targets


def select_canary_targets(
    targets: list[PriceLinkTargetGroup], limit: int = 3
) -> list[PriceLinkTargetGroup]:
    """重複2ページ・価格差のみ(リンクは同一)の price_only グループから最も単純な順にlimit件選ぶ。"""
    candidates = [
        t
        for t in targets
        if t.review_category == CATEGORY_PRICE_ONLY
        and len(t.page_ids) == 2
        and t.is_price_diff_only
    ]
    ranked = sorted(candidates, key=lambda t: (t.merged_deck_relation_count, t.card_name))
    return ranked[:limit]


def remaining_price_only_targets(
    targets: list[PriceLinkTargetGroup], already_processed: list[PriceLinkTargetGroup]
) -> list[PriceLinkTargetGroup]:
    processed_names = {t.card_name for t in already_processed}
    return [
        t
        for t in targets
        if t.review_category == CATEGORY_PRICE_ONLY and t.card_name not in processed_names
    ]


def batch_targets(
    targets: list[PriceLinkTargetGroup], batch_size: int = 7
) -> list[list[PriceLinkTargetGroup]]:
    return [targets[i : i + batch_size] for i in range(0, len(targets), batch_size)]


def select_remaining_batch(
    remaining: list[PriceLinkTargetGroup], limit: int | None = None, offset: int = 0
) -> list[PriceLinkTargetGroup]:
    """リスクが低い順(採用デッキ件数少 → カード名)にソートし、offset/limitで切り出す。"""
    ranked = sorted(remaining, key=lambda t: (t.merged_deck_relation_count, t.card_name))
    sliced = ranked[offset:]
    if limit is not None:
        sliced = sliced[:limit]
    return sliced


# --- 適用 --------------------------------------------------------------------


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
    history_note_appended: str | None = None


def apply_price_link_targets(
    repo: DedupeRepository,
    targets: list[PriceLinkTargetGroup],
    apply: bool,
    exclusions: ExclusionList | None = None,
) -> list[GroupApplyOutcome]:
    """対象グループを1件ずつ鮮度チェックしてから適用(またはdry-run計画表示)する。"""
    exclusions = exclusions or ExclusionList()
    return [
        _process_one_group(repo, target, apply=apply, exclusions=exclusions) for target in targets
    ]


def _process_one_group(
    repo: DedupeRepository,
    target: PriceLinkTargetGroup,
    apply: bool,
    exclusions: ExclusionList,
) -> GroupApplyOutcome:
    fresh_reviews = review_duplicate_conflicts(
        repo, card_name=target.card_name, exclusions=exclusions
    )

    if not fresh_reviews:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_SKIPPED_NOT_DUPLICATE,
            reason="現在は重複していません(既に統合済み、または単一レコードになっています)",
        )

    fresh = fresh_reviews[0]

    if fresh.review_category != target.review_category:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_SKIPPED_STALE,
            reason=(
                f"現在の分類が '{fresh.review_category}' に変化しています"
                f"(監査時: {target.review_category})"
            ),
        )

    fresh_page_ids = {p["id"] for p in fresh.pages}
    report_page_ids = set(target.page_ids)
    if fresh_page_ids != report_page_ids:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_SKIPPED_STALE,
            reason=(
                "ページ構成がレポート作成時と異なります"
                f"(レポート時: {sorted(report_page_ids)}, 現在: {sorted(fresh_page_ids)})"
            ),
        )

    if target.review_category == CATEGORY_MANUAL and not target.representative_page_id:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_FAILED,
            error="手動代表指定が必要ですが representative_page_id が指定されていません",
        )

    before_snapshot = [_page_summary(p) for p in fresh.pages]

    plan = build_dedupe_plan(
        repo, card_name=target.card_name, representative_page_id=target.representative_page_id
    )
    if plan.group_errors:
        err = plan.group_errors[0]
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_FAILED,
            error=err.message,
            before_snapshot=before_snapshot,
        )
    if not plan.merge_plans:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_SKIPPED_NOT_DUPLICATE,
            reason="統合計画を作成できませんでした(直前で状態が変化した可能性)",
            before_snapshot=before_snapshot,
        )
    merge_plan = plan.merge_plans[0]

    existing_note = _plain_text(merge_plan.representative.page, NOTE_PROPERTY)
    history_note = build_merge_history_note(merge_plan.duplicate_pages, existing_note)

    after_snapshot = {
        "representative_page_id": merge_plan.representative_page_id,
        "owned": merge_plan.owned,
        "quantity": merge_plan.quantity,
        "english_name": merge_plan.english_name,
        "deck_relation_count": len(merge_plan.merged_deck_relation_ids),
        "commander_tags": merge_plan.multi_valued_attributes.get("統率者", []),
    }

    if not apply:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_PLANNED,
            representative_page_id=merge_plan.representative_page_id,
            merged_page_ids=[p["id"] for p in merge_plan.duplicate_pages],
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            history_note_appended=history_note,
        )

    result = execute_dedupe_plan(plan, repo)
    group_result = result.results[0]

    if group_result.error:
        return GroupApplyOutcome(
            card_name=target.card_name,
            status=STATUS_FAILED,
            error=group_result.error,
            representative_page_id=group_result.representative_page_id,
            before_snapshot=before_snapshot,
        )

    return GroupApplyOutcome(
        card_name=target.card_name,
        status=STATUS_APPLIED,
        representative_page_id=group_result.representative_page_id,
        merged_page_ids=group_result.duplicate_page_ids_marked,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        history_note_appended=history_note,
    )


# --- 適用ログ出力 --------------------------------------------------------------


@dataclass(frozen=True)
class ApplyLogPaths:
    json_path: Path


def write_price_link_apply_log(
    outcomes: list[GroupApplyOutcome],
    targets_report_path: str | Path,
    output_dir: Path,
    applied: bool,
    timestamp: str | None = None,
) -> ApplyLogPaths:
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"dedupe-price-apply-{timestamp}.json"

    api_update_count = sum(
        1 + len(o.merged_page_ids) for o in outcomes if o.status == STATUS_APPLIED
    )

    log = {
        "executed_at": datetime.datetime.now().isoformat(),
        "targets_report": str(targets_report_path),
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
                "history_note_appended": o.history_note_appended,
            }
            for o in outcomes
        ],
    }

    json_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return ApplyLogPaths(json_path=json_path)
