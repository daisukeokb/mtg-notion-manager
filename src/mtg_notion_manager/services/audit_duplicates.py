"""残りの重複カードグループを監査し、統合可否を分類する(Notionへの書き込みは一切行わない)。

分類:
- auto: 自動統合可能(dedupe-cardsでそのまま統合してよい)
- needs_review: 要確認(属性競合・特殊仕様の疑いなど、人が内容を見るべき)
- manual_representative: 代表候補が同点で自動決定できない
- excluded: 除外リスト(config/dedupe_exclusions.json)により対象外
"""

from __future__ import annotations

import csv
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.intentional_duplicates import (
    IntentionalDuplicateConfig,
    IntentionalDuplicateGroup,
)
from mtg_notion_manager.notion.dedupe_repository import (
    DECKS_RELATION_PROPERTY,
    ENGLISH_NAME_PROPERTY,
    TITLE_PROPERTY,
    DedupeRepository,
)
from mtg_notion_manager.parsers.card_names import normalize_card_name
from mtg_notion_manager.services.dedupe_cards import evaluate_representative

CATEGORY_AUTO = "auto"
CATEGORY_NEEDS_REVIEW = "needs_review"
CATEGORY_MANUAL_REPRESENTATIVE = "manual_representative"
CATEGORY_EXCLUDED = "excluded"
CATEGORY_INTENTIONAL_DUPLICATE = "intentional_duplicates"

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_AUTO: "自動統合可能",
    CATEGORY_NEEDS_REVIEW: "要確認",
    CATEGORY_MANUAL_REPRESENTATIVE: "手動代表指定が必要",
    CATEGORY_EXCLUDED: "統合対象外",
    CATEGORY_INTENTIONAL_DUPLICATE: "意図的に保持する重複",
}

INTENTIONAL_DUPLICATE_SOURCE = "config/intentional_duplicate_cards.json"

# 自動統合可否を判定する属性競合の対象(仕様どおり: 英語名/タイプ/シンボル)。
_CONFLICT_SINGLE_PROPERTIES = (ENGLISH_NAME_PROPERTY, "タイプ")
_CONFLICT_MULTI_PROPERTIES = ("シンボル",)

SPECIAL_VERSION_KEYWORDS = (
    "版違い",
    "Foil",
    "foil",
    "プロモ",
    "ショーケース",
    "旧枠",
    "拡張アート",
)

DEFAULT_EXCLUSIONS_PATH = Path("config/dedupe_exclusions.json")


@dataclass(frozen=True)
class ExclusionList:
    card_names: frozenset[str] = frozenset()
    page_ids: frozenset[str] = frozenset()


def load_exclusions(path: Path = DEFAULT_EXCLUSIONS_PATH) -> ExclusionList:
    if not path.exists():
        return ExclusionList()
    data = json.loads(path.read_text(encoding="utf-8"))
    names = frozenset(normalize_card_name(n) for n in data.get("card_names", []))
    ids = frozenset(data.get("page_ids", []))
    return ExclusionList(card_names=names, page_ids=ids)


@dataclass(frozen=True)
class AttributeConflict:
    property_name: str
    values: list[str]


@dataclass(frozen=True)
class GroupAudit:
    card_name: str
    pages: list[dict]
    category: str
    recommended_representative_id: str | None
    representative_reasons: list[str]
    conflicts: list[AttributeConflict]
    special_version_flags: list[str]
    price_link_differs: bool
    merged_deck_relation_count: int
    estimated_quantity: int
    risks: list[str]
    recommended_action: str
    excluded_reason: str | None = None
    intentional_duplicate_reason: str | None = None


def audit_duplicate_groups(
    repo: DedupeRepository,
    card_name: str | None = None,
    exclusions: ExclusionList | None = None,
    intentional_duplicates: IntentionalDuplicateConfig | None = None,
) -> list[GroupAudit]:
    """重複グループを検出し、各グループを分類する(読み取りのみ)。

    intentional_duplicates を指定しない場合は空扱い(既存の分類結果は一切変わらない)。
    """
    repo.load()
    exclusions = exclusions or ExclusionList()
    intentional_duplicates = intentional_duplicates or IntentionalDuplicateConfig(groups=[])
    groups = repo.find_duplicate_groups(card_name)
    return [
        _audit_one_group(repo, pages, exclusions, intentional_duplicates)
        for pages in groups.values()
    ]


def _audit_one_group(
    repo: DedupeRepository,
    pages: list[dict],
    exclusions: ExclusionList,
    intentional_duplicates: IntentionalDuplicateConfig,
) -> GroupAudit:
    display_name = _plain_text(pages[0], TITLE_PROPERTY) or "(不明)"

    intentional_match = _check_intentional_duplicate(pages, intentional_duplicates)
    if intentional_match is not None:
        return GroupAudit(
            card_name=display_name,
            pages=pages,
            category=CATEGORY_INTENTIONAL_DUPLICATE,
            recommended_representative_id=None,
            representative_reasons=[],
            conflicts=[],
            special_version_flags=[],
            price_link_differs=False,
            merged_deck_relation_count=0,
            estimated_quantity=len(pages),
            risks=[],
            recommended_action=(
                "意図的に別レコードとして保持されています"
                f"({INTENTIONAL_DUPLICATE_SOURCE})。統合・代表指定は行いません。"
            ),
            intentional_duplicate_reason=intentional_match.reason,
        )

    excluded_reason = _check_exclusion(display_name, pages, exclusions)
    if excluded_reason is not None:
        return GroupAudit(
            card_name=display_name,
            pages=pages,
            category=CATEGORY_EXCLUDED,
            recommended_representative_id=None,
            representative_reasons=[],
            conflicts=[],
            special_version_flags=[],
            price_link_differs=False,
            merged_deck_relation_count=0,
            estimated_quantity=len(pages),
            risks=["除外リストにより対象外"],
            recommended_action="このグループは統合しない(除外リストに従う)",
            excluded_reason=excluded_reason,
        )

    evaluation = evaluate_representative(repo, pages)
    conflicts = _detect_conflicts(pages)
    special_flags = _detect_special_version_keywords(pages)
    price_link_differs = _detect_price_link_differences(pages)
    relation_count = len(_union_relation_ids(repo, pages))

    if evaluation.winner is None:
        return GroupAudit(
            card_name=display_name,
            pages=pages,
            category=CATEGORY_MANUAL_REPRESENTATIVE,
            recommended_representative_id=None,
            representative_reasons=[],
            conflicts=conflicts,
            special_version_flags=special_flags,
            price_link_differs=price_link_differs,
            merged_deck_relation_count=relation_count,
            estimated_quantity=len(pages),
            risks=["代表候補が複数の基準で完全に同点のため自動選択できません"],
            recommended_action="--representative-page-id で手動指定してください",
        )

    risks: list[str] = []
    if conflicts:
        risks.append(f"属性競合: {', '.join(c.property_name for c in conflicts)}")
    if special_flags:
        risks.append(f"特殊仕様の記載を検出: {', '.join(special_flags)}")
    if price_link_differs:
        risks.append("販売価格または販売リンクがページごとに異なる")

    if risks:
        category = CATEGORY_NEEDS_REVIEW
        action = "内容を確認したうえで、問題なければ手動でdedupe-cardsを実行してください"
    else:
        category = CATEGORY_AUTO
        action = "dedupe-cards --card-name で自動統合可能"

    return GroupAudit(
        card_name=display_name,
        pages=pages,
        category=category,
        recommended_representative_id=evaluation.winner["id"],
        representative_reasons=evaluation.reasons,
        conflicts=conflicts,
        special_version_flags=special_flags,
        price_link_differs=price_link_differs,
        merged_deck_relation_count=relation_count,
        estimated_quantity=len(pages),
        risks=risks,
        recommended_action=action,
    )


def _check_intentional_duplicate(
    pages: list[dict], config: IntentionalDuplicateConfig
) -> IntentionalDuplicateGroup | None:
    """ページID集合とカード名が、意図的重複設定と完全一致する場合のみそのグループを返す。"""
    page_ids = frozenset(p["id"] for p in pages)
    name_ja = _plain_text(pages[0], TITLE_PROPERTY)
    name_en = _plain_text(pages[0], ENGLISH_NAME_PROPERTY)
    return config.find_matching_group(page_ids, name_ja, name_en)


def _check_exclusion(display_name: str, pages: list[dict], exclusions: ExclusionList) -> str | None:
    if normalize_card_name(display_name) in exclusions.card_names:
        return (
            f"カード名 '{display_name}' が除外リスト(config/dedupe_exclusions.json)に含まれています"
        )
    matching_ids = [p["id"] for p in pages if p["id"] in exclusions.page_ids]
    if matching_ids:
        return f"ページID {matching_ids} が除外リストに含まれています"
    return None


def _detect_conflicts(pages: list[dict]) -> list[AttributeConflict]:
    conflicts: list[AttributeConflict] = []

    for prop_name in _CONFLICT_SINGLE_PROPERTIES:
        values = {v for page in pages if (v := _single_value(page, prop_name))}
        if len(values) > 1:
            conflicts.append(AttributeConflict(prop_name, sorted(values)))

    for prop_name in _CONFLICT_MULTI_PROPERTIES:
        value_sets = {
            tuple(sorted(vs))
            for page in pages
            if (vs := _multi_select_value(page, prop_name))
        }
        if len(value_sets) > 1:
            conflicts.append(
                AttributeConflict(prop_name, ["/".join(vs) for vs in sorted(value_sets)])
            )

    return conflicts


def _detect_special_version_keywords(pages: list[dict]) -> list[str]:
    found: set[str] = set()
    for page in pages:
        note = (_plain_text(page, "メモ") or "").lower()
        for keyword in SPECIAL_VERSION_KEYWORDS:
            if keyword.lower() in note:
                found.add(keyword)
    return sorted(found)


def _detect_price_link_differences(pages: list[dict]) -> bool:
    prices = {
        page.get("properties", {}).get("販売価格", {}).get("number")
        for page in pages
        if page.get("properties", {}).get("販売価格", {}).get("number") is not None
    }
    links = {v for page in pages if (v := _url_value(page, "販売リンク"))}
    return len(prices) > 1 or len(links) > 1


def _union_relation_ids(repo: DedupeRepository, pages: list[dict]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for page in pages:
        for rid in repo.get_full_relation_ids(page, DECKS_RELATION_PROPERTY):
            if rid not in seen:
                seen.add(rid)
                result.append(rid)
    return result


def _single_value(page: dict, prop_name: str) -> str | None:
    prop = page.get("properties", {}).get(prop_name)
    if prop is None:
        return None
    prop_type = prop.get("type")
    if prop_type in ("title", "rich_text"):
        return _plain_text(page, prop_name)
    if prop_type == "select":
        select = prop.get("select")
        return select.get("name") if select else None
    return None


def _multi_select_value(page: dict, prop_name: str) -> list[str]:
    prop = page.get("properties", {}).get(prop_name, {})
    return [opt.get("name") for opt in prop.get("multi_select", []) if opt.get("name")]


def _url_value(page: dict, prop_name: str) -> str | None:
    prop = page.get("properties", {}).get(prop_name)
    if prop is None:
        return None
    return prop.get("url") or None


def _plain_text(page: dict, prop_name: str) -> str | None:
    prop = page.get("properties", {}).get(prop_name)
    if prop is None:
        return None
    prop_type = prop.get("type")
    if prop_type == "title":
        text = "".join(t.get("plain_text", "") for t in prop.get("title", []))
    elif prop_type == "rich_text":
        text = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    else:
        return None
    return text or None


# --- レポート出力 -----------------------------------------------------------


@dataclass(frozen=True)
class AuditReportPaths:
    json_path: Path
    csv_path: Path
    markdown_path: Path


def write_audit_reports(
    audits: list[GroupAudit], output_dir: Path, timestamp: str | None = None
) -> AuditReportPaths:
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"dedupe-audit-{timestamp}.json"
    csv_path = output_dir / f"dedupe-audit-{timestamp}.csv"
    md_path = output_dir / f"dedupe-audit-{timestamp}.md"

    json_path.write_text(
        json.dumps([_audit_to_dict(a) for a in audits], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(audits, csv_path)
    _write_markdown(audits, md_path)

    return AuditReportPaths(json_path=json_path, csv_path=csv_path, markdown_path=md_path)


def _page_summary(page: dict) -> dict:
    return {
        "page_id": page["id"],
        "page_url": page.get("url", ""),
        "card_name": _plain_text(page, TITLE_PROPERTY),
        "english_name": _plain_text(page, ENGLISH_NAME_PROPERTY),
        "type": _single_value(page, "タイプ"),
        "symbols": _multi_select_value(page, "シンボル"),
        "roles": _multi_select_value(page, "役割（標準）"),
        "owned": bool(page.get("properties", {}).get("所持", {}).get("checkbox")),
        "quantity": page.get("properties", {}).get("所持枚数", {}).get("number"),
        "deck_relation_count": len(
            page.get("properties", {}).get(DECKS_RELATION_PROPERTY, {}).get("relation", [])
        ),
        "commander_tags": _multi_select_value(page, "統率者"),
        "price": page.get("properties", {}).get("販売価格", {}).get("number"),
        "link": _url_value(page, "販売リンク"),
        "note": _plain_text(page, "メモ"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
    }


def _audit_to_dict(audit: GroupAudit) -> dict:
    result: dict = {
        "card_name": audit.card_name,
        "category": audit.category,
        "category_label": CATEGORY_LABELS[audit.category],
        "duplicate_count": len(audit.pages),
        "recommended_representative_id": audit.recommended_representative_id,
        "representative_reasons": audit.representative_reasons,
        "conflicts": [{"property": c.property_name, "values": c.values} for c in audit.conflicts],
        "special_version_flags": audit.special_version_flags,
        "price_link_differs": audit.price_link_differs,
        "merged_deck_relation_count": audit.merged_deck_relation_count,
        "estimated_quantity": audit.estimated_quantity,
        "risks": audit.risks,
        "recommended_action": audit.recommended_action,
        "excluded_reason": audit.excluded_reason,
        "pages": [_page_summary(p) for p in audit.pages],
    }
    if audit.category == CATEGORY_INTENTIONAL_DUPLICATE:
        result["card_name_en"] = (
            _plain_text(audit.pages[0], ENGLISH_NAME_PROPERTY) if audit.pages else None
        )
        result["card_name_ja"] = audit.card_name
        result["page_ids"] = [p["id"] for p in audit.pages]
        result["reason"] = audit.intentional_duplicate_reason
        result["status"] = "intentional_duplicate"
        result["source"] = INTENTIONAL_DUPLICATE_SOURCE
    return result


_CSV_FIELDNAMES = [
    "card_name",
    "category",
    "duplicate_count",
    "recommended_representative_id",
    "representative_reasons",
    "conflicts",
    "special_version_flags",
    "price_link_differs",
    "merged_deck_relation_count",
    "estimated_quantity",
    "risks",
    "recommended_action",
    "excluded_reason",
    "intentional_duplicate_reason",
]


def _write_csv(audits: list[GroupAudit], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        for audit in audits:
            writer.writerow(
                {
                    "card_name": audit.card_name,
                    "category": audit.category,
                    "duplicate_count": len(audit.pages),
                    "recommended_representative_id": audit.recommended_representative_id or "",
                    "representative_reasons": "; ".join(audit.representative_reasons),
                    "conflicts": "; ".join(
                        f"{c.property_name}:{'/'.join(c.values)}" for c in audit.conflicts
                    ),
                    "special_version_flags": ", ".join(audit.special_version_flags),
                    "price_link_differs": audit.price_link_differs,
                    "merged_deck_relation_count": audit.merged_deck_relation_count,
                    "estimated_quantity": audit.estimated_quantity,
                    "risks": "; ".join(audit.risks),
                    "recommended_action": audit.recommended_action,
                    "excluded_reason": audit.excluded_reason or "",
                    "intentional_duplicate_reason": audit.intentional_duplicate_reason or "",
                }
            )


def _write_markdown(audits: list[GroupAudit], path: Path) -> None:
    counts = {cat: 0 for cat in CATEGORY_LABELS}
    for audit in audits:
        counts[audit.category] += 1

    lines = [
        "# 重複カード監査レポート",
        "",
        f"- 全グループ数: {len(audits)}",
        f"- 自動統合可能: {counts[CATEGORY_AUTO]}",
        f"- 要確認: {counts[CATEGORY_NEEDS_REVIEW]}",
        f"- 手動代表指定が必要: {counts[CATEGORY_MANUAL_REPRESENTATIVE]}",
        f"- 統合対象外: {counts[CATEGORY_EXCLUDED]}",
        f"- 意図的に保持する重複: {counts[CATEGORY_INTENTIONAL_DUPLICATE]}",
        "",
    ]

    for category in (
        CATEGORY_AUTO,
        CATEGORY_NEEDS_REVIEW,
        CATEGORY_MANUAL_REPRESENTATIVE,
        CATEGORY_EXCLUDED,
        CATEGORY_INTENTIONAL_DUPLICATE,
    ):
        lines.append(f"## {CATEGORY_LABELS[category]}")
        lines.append("")
        group_list = [a for a in audits if a.category == category]
        if not group_list:
            lines.append("(該当なし)")
            lines.append("")
            continue
        for audit in group_list:
            lines.append(f"### {audit.card_name}")
            lines.append(f"- 重複件数: {len(audit.pages)}")
            lines.append(f"- 推奨代表ページ: {audit.recommended_representative_id or '(なし)'}")
            lines.append(f"- 代表選択理由: {', '.join(audit.representative_reasons) or '(なし)'}")
            conflict_desc = (
                ", ".join(f"{c.property_name}({'/'.join(c.values)})" for c in audit.conflicts)
                or "なし"
            )
            lines.append(f"- 属性競合: {conflict_desc}")
            lines.append(f"- 採用デッキ統合後件数: {audit.merged_deck_relation_count}")
            lines.append(f"- 想定所持枚数: {audit.estimated_quantity}")
            lines.append(f"- リスク: {'; '.join(audit.risks) or 'なし'}")
            lines.append(f"- 推奨アクション: {audit.recommended_action}")
            if audit.excluded_reason:
                lines.append(f"- 除外理由: {audit.excluded_reason}")
            if audit.intentional_duplicate_reason:
                lines.append(f"- 保持理由: {audit.intentional_duplicate_reason}")
                lines.append("- 状態: intentional_duplicate")
                lines.append(f"- ページID: {', '.join(p['id'] for p in audit.pages)}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
