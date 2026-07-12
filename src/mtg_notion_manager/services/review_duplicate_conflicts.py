"""「要確認」グループをさらに詳細分類する(Notionへの書き込みは一切行わない)。

audit_duplicates.py の分類(auto/needs_review/manual_representative/excluded)のうち
needs_review と manual_representative だけを対象に、以下へ再分類する:

- price_only: 価格・販売リンク差異のみ(統合候補)
- special_version: 特殊仕様差異(統合対象外候補)
- identity_conflict: カード同一性の競合(自動統合禁止)
- other: 上記に当てはまらない要確認
- manual_representative: 代表候補が同点(血染めのぬかるみを含む)

auto/excludedのグループはこのレポートの対象外。
"""

from __future__ import annotations

import csv
import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

from mtg_notion_manager.intentional_duplicates import IntentionalDuplicateConfig
from mtg_notion_manager.notion.dedupe_repository import ENGLISH_NAME_PROPERTY, DedupeRepository
from mtg_notion_manager.services.audit_duplicates import (
    CATEGORY_INTENTIONAL_DUPLICATE,
    CATEGORY_MANUAL_REPRESENTATIVE,
    CATEGORY_NEEDS_REVIEW,
    INTENTIONAL_DUPLICATE_SOURCE,
    AttributeConflict,
    ExclusionList,
    GroupAudit,
    _detect_conflicts,
    _multi_select_value,
    _page_summary,
    _plain_text,
    _url_value,
    audit_duplicate_groups,
)

CATEGORY_PRICE_ONLY = "price_only"
CATEGORY_SPECIAL_VERSION = "special_version"
CATEGORY_IDENTITY_CONFLICT = "identity_conflict"
CATEGORY_OTHER = "other"
CATEGORY_MANUAL = "manual_representative"
CATEGORY_INTENTIONAL = "intentional_duplicates"

REVIEW_CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_PRICE_ONLY: "A: 価格・販売リンク差異のみ",
    CATEGORY_SPECIAL_VERSION: "B: 特殊仕様差異",
    CATEGORY_IDENTITY_CONFLICT: "C: カード同一性の競合",
    CATEGORY_OTHER: "D: その他の要確認",
    CATEGORY_MANUAL: "E: 手動代表指定が必要",
}

INTENTIONAL_DUPLICATE_LABEL = "意図的に保持する重複"

# 特殊仕様の疑いを検出する拡張キーワード一覧(audit_duplicates.pyの基本セットより広い)。
EXPANDED_SPECIAL_KEYWORDS = (
    "Foil",
    "foil",
    "フォイル",
    "ショーケース",
    "拡張アート",
    "ボーダーレス",
    "旧枠",
    "プロモ",
    "シリアル",
    "特別版",
    "Collector",
    "collector",
    "etched",
    "surge foil",
    "galaxy foil",
    "日本語版限定",
    "別イラスト",
)

_ROLE_PROPERTY = "役割（標準）"


@dataclass(frozen=True)
class DetailedGroupReview:
    card_name: str
    pages: list[dict]
    review_category: str
    representative_candidate_id: str | None
    representative_reasons: list[str]
    prices: list[float]
    links: list[str]
    conflicts: list[AttributeConflict]
    role_conflict: bool
    special_flags: list[str]
    merged_deck_relation_count: int
    merged_commander_tags: list[str]
    estimated_quantity: int
    recommended_price_link_handling: str
    integrable: bool
    risks: list[str] = field(default_factory=list)
    intentional_duplicate_reason: str | None = None


def review_duplicate_conflicts(
    repo: DedupeRepository,
    card_name: str | None = None,
    category: str | None = None,
    exclusions: ExclusionList | None = None,
    intentional_duplicates: IntentionalDuplicateConfig | None = None,
) -> list[DetailedGroupReview]:
    """needs_review / manual_representative のグループを詳細分類する(読み取りのみ)。

    intentional_duplicates を指定した場合、audit_duplicate_groups() の判定をそのまま
    再利用し、意図的重複と判定されたグループは専用カテゴリ(CATEGORY_INTENTIONAL)として
    結果に含める(通常の要確認・手動対応グループの件数には含めない)。
    """
    base_audits = audit_duplicate_groups(
        repo,
        card_name=card_name,
        exclusions=exclusions,
        intentional_duplicates=intentional_duplicates,
    )

    reviews: list[DetailedGroupReview] = []
    for audit in base_audits:
        if audit.category == CATEGORY_INTENTIONAL_DUPLICATE:
            review = _build_intentional_duplicate_review(audit)
        elif audit.category in (CATEGORY_NEEDS_REVIEW, CATEGORY_MANUAL_REPRESENTATIVE):
            review = _build_detailed_review(audit)
        else:
            continue  # auto / excluded はこのレポートの対象外
        if category is not None and review.review_category != category:
            continue
        reviews.append(review)

    return reviews


def _build_intentional_duplicate_review(audit: GroupAudit) -> DetailedGroupReview:
    return DetailedGroupReview(
        card_name=audit.card_name,
        pages=audit.pages,
        review_category=CATEGORY_INTENTIONAL,
        representative_candidate_id=None,
        representative_reasons=[],
        prices=[],
        links=[],
        conflicts=[],
        role_conflict=False,
        special_flags=[],
        merged_deck_relation_count=audit.merged_deck_relation_count,
        merged_commander_tags=_union_multi_select(audit.pages, "統率者"),
        estimated_quantity=audit.estimated_quantity,
        recommended_price_link_handling="(意図的に保持する重複のため対応不要)",
        integrable=False,
        risks=[],
        intentional_duplicate_reason=audit.intentional_duplicate_reason,
    )


def _build_detailed_review(audit: GroupAudit) -> DetailedGroupReview:
    pages = audit.pages
    prices = sorted(
        {
            p.get("properties", {}).get("販売価格", {}).get("number")
            for p in pages
            if p.get("properties", {}).get("販売価格", {}).get("number") is not None
        }
    )
    links = sorted({v for p in pages if (v := _url_value(p, "販売リンク"))})
    commander_tags = _union_multi_select(pages, "統率者")

    if audit.category == CATEGORY_MANUAL_REPRESENTATIVE:
        return DetailedGroupReview(
            card_name=audit.card_name,
            pages=pages,
            review_category=CATEGORY_MANUAL,
            representative_candidate_id=None,
            representative_reasons=[],
            prices=prices,
            links=links,
            conflicts=audit.conflicts,
            role_conflict=_detect_role_conflict(pages),
            special_flags=list(audit.special_version_flags),
            merged_deck_relation_count=audit.merged_deck_relation_count,
            merged_commander_tags=commander_tags,
            estimated_quantity=audit.estimated_quantity,
            recommended_price_link_handling="(代表未決定のため判断できません)",
            integrable=False,
            risks=list(audit.risks),
        )

    conflicts = _detect_conflicts(pages)
    role_conflict = _detect_role_conflict(pages)
    special_flags = _detect_expanded_special_keywords(pages)
    dual_face_mismatch = _detect_dual_face_mismatch(pages)
    price_link_differs = bool(len(prices) > 1 or len(links) > 1)

    if conflicts or dual_face_mismatch:
        review_category = CATEGORY_IDENTITY_CONFLICT
        integrable = False
        handling = "自動統合禁止。カードとしての同一性を人手で確認してください"
    elif special_flags:
        review_category = CATEGORY_SPECIAL_VERSION
        integrable = False
        handling = "版・仕様の異なる物理コピーの可能性。カード種類単位での統合対象外候補"
    elif role_conflict:
        review_category = CATEGORY_OTHER
        integrable = False
        handling = "役割（標準）が競合しているため要確認(自動分類はA/B/Cに当てはまらない)"
    elif price_link_differs:
        review_category = CATEGORY_PRICE_ONLY
        integrable = True
        handling = "価格・販売リンクの扱いは3案を比較(下記レポート参照)。方針決定後に統合可能"
    else:
        review_category = CATEGORY_OTHER
        integrable = False
        handling = "既存分類基準に当てはまらない差異あり。個別確認が必要"

    risks: list[str] = []
    if conflicts:
        risks.append(f"属性競合: {', '.join(c.property_name for c in conflicts)}")
    if dual_face_mismatch:
        risks.append("両面/分割カードの面構成が一致しない可能性")
    if special_flags:
        risks.append(f"特殊仕様の記載を検出: {', '.join(special_flags)}")
    if role_conflict:
        risks.append("役割（標準）が競合")
    if price_link_differs and review_category == CATEGORY_PRICE_ONLY:
        risks.append("販売価格または販売リンクがページごとに異なる(カード同一性に問題なし)")

    return DetailedGroupReview(
        card_name=audit.card_name,
        pages=pages,
        review_category=review_category,
        representative_candidate_id=audit.recommended_representative_id,
        representative_reasons=audit.representative_reasons,
        prices=prices,
        links=links,
        conflicts=conflicts,
        role_conflict=role_conflict,
        special_flags=special_flags,
        merged_deck_relation_count=audit.merged_deck_relation_count,
        merged_commander_tags=commander_tags,
        estimated_quantity=audit.estimated_quantity,
        recommended_price_link_handling=handling,
        integrable=integrable,
        risks=risks,
    )


def _detect_role_conflict(pages: list[dict]) -> bool:
    value_sets = {
        tuple(sorted(vs)) for page in pages if (vs := _multi_select_value(page, _ROLE_PROPERTY))
    }
    return len(value_sets) > 1


def _detect_expanded_special_keywords(pages: list[dict]) -> list[str]:
    found: set[str] = set()
    for page in pages:
        note = (_plain_text(page, "メモ") or "").lower()
        for keyword in EXPANDED_SPECIAL_KEYWORDS:
            if keyword.lower() in note:
                found.add(keyword)
    return sorted(found)


def _detect_dual_face_mismatch(pages: list[dict]) -> bool:
    """英語名の "//" (両面/分割カード区切り)有無がページ間で不一致かを検出する簡易判定。"""
    names = [name for p in pages if (name := _plain_text(p, "英語名"))]
    has_separator = {"//" in name for name in names}
    return len(has_separator) > 1


def _union_multi_select(pages: list[dict], prop_name: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for page in pages:
        for value in _multi_select_value(page, prop_name):
            if value not in seen:
                seen.add(value)
                result.append(value)
    return result


# --- レポート出力 -----------------------------------------------------------

PRICE_LINK_STRATEGY_NOTES = """## 価格・販売リンク差異の統合ルール案(A分類のみ対象)

### 案1: 代表ページの値をそのまま保持
- 実装が最も単純(現行のdedupe-cards実装は既にこの方式)
- 古い価格が残る可能性がある

### 案2: 最終更新日時が最も新しい非空の価格・リンクを採用
- 現時点の情報に近い可能性がある
- 更新日時が価格更新を意味するとは限らない(他項目の編集で更新日時だけ進むこともある)

### 案3: 代表ページでは価格・リンクを更新せず、異なる値をメモへ履歴として保存
- 情報を失わない
- メモが肥大化する

### 判断材料
このカードDBは、同名カードでも統率者タグ(旧「統率者」multi_select)がページごとに異なり、
「どのデッキ用に確保した物理コピーか」を1ページ1コピー単位で管理してきた形跡がある
(例: 太陽の指輪の統合前6ページはそれぞれ異なる統率者タグを持っていた)。
これは「現在の販売情報を1件に集約する」運用ではなく、
「購入コピーごとの履歴を残す」運用に近いと考えられる。

**推奨**: 短期的には案1(現行実装)を維持しつつ、A分類グループは統合前に案3の要領で
メモへ購入時価格・リンクの履歴を残すことを推奨する。恒久的には後述の
「所持コピーDB」で物理コピー単位の価格・購入先情報を分離管理する方が実態に合う。
"""

COPY_DB_DESIGN_NOTES = """## 所持コピーDB設計案(今回は作成しない)

カードの色・タイプなど「カードそのもの」の情報はMTGカードDBに残しつつ、
価格・購入先・言語・Foilなど「物理コピーごと」に異なりうる情報を分離するための
別データベース案。

名称例: `MTG所持コピーDB`

| プロパティ | 型 | 説明 |
|---|---|---|
| コピー名 | title | 例: 「太陽の指輪 #1」など識別用 |
| カード | relation (→MTGカードDB) | どのカードの物理コピーか |
| 言語 | select | 日本語版/英語版など |
| Foil | checkbox | |
| 特殊仕様 | multi_select | ショーケース/拡張アート/旧枠など |
| 枚数 | number | 通常は1(コピー単位管理のため) |
| 購入価格 | number | |
| 購入先 | text または select | 店舗名など |
| 購入URL | url | |
| 購入日 | date | |
| 保管場所 | text | |
| メモ | text | |

MTGカードDB側は「このカード名を何枚所持しているか(集計値)」を持ち、
所持コピーDB側が「どのコピーをいくらでどこから買ったか」の履歴を持つ、
という役割分担になる。

### 所持コピーDBを追加すべきか

**判断: 現時点では追加を急ぐ必要はない(将来的な検討課題)。**
今回検出された「価格・リンク差異のみ」グループは少数であり、A分類統合時に
メモへ履歴を残す(案3)運用で当面は実害を避けられる。ただし、今後も
物理コピー単位の購入記録を継続的に増やしたい場合は、所持コピーDBの
新設によって「カードの静的情報」と「購入・所持の履歴」を分離した方が、
長期的にはメモの肥大化やproperty競合を避けやすい。
"""


@dataclass(frozen=True)
class ReviewReportPaths:
    json_path: Path
    csv_path: Path
    markdown_path: Path


def write_review_reports(
    reviews: list[DetailedGroupReview], output_dir: Path, timestamp: str | None = None
) -> ReviewReportPaths:
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"dedupe-review-details-{timestamp}.json"
    csv_path = output_dir / f"dedupe-review-details-{timestamp}.csv"
    md_path = output_dir / f"dedupe-review-details-{timestamp}.md"

    json_path.write_text(
        json.dumps([_review_to_dict(r) for r in reviews], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(reviews, csv_path)
    _write_markdown(reviews, md_path)

    return ReviewReportPaths(json_path=json_path, csv_path=csv_path, markdown_path=md_path)


def _review_to_dict(review: DetailedGroupReview) -> dict:
    result: dict = {
        "card_name": review.card_name,
        "review_category": review.review_category,
        "review_category_label": REVIEW_CATEGORY_LABELS.get(
            review.review_category, INTENTIONAL_DUPLICATE_LABEL
        ),
        "duplicate_count": len(review.pages),
        "representative_candidate_id": review.representative_candidate_id,
        "representative_reasons": review.representative_reasons,
        "prices": review.prices,
        "links": review.links,
        "conflicts": [{"property": c.property_name, "values": c.values} for c in review.conflicts],
        "role_conflict": review.role_conflict,
        "special_flags": review.special_flags,
        "merged_deck_relation_count": review.merged_deck_relation_count,
        "merged_commander_tags": review.merged_commander_tags,
        "estimated_quantity": review.estimated_quantity,
        "recommended_price_link_handling": review.recommended_price_link_handling,
        "integrable": review.integrable,
        "risks": review.risks,
        "action_required": review.review_category != CATEGORY_INTENTIONAL,
        "pages": [_page_summary(p) for p in review.pages],
    }
    if review.review_category == CATEGORY_INTENTIONAL:
        result["card_name_en"] = (
            _plain_text(review.pages[0], ENGLISH_NAME_PROPERTY) if review.pages else None
        )
        result["card_name_ja"] = review.card_name
        result["page_ids"] = [p["id"] for p in review.pages]
        result["reason"] = review.intentional_duplicate_reason
        result["status"] = "intentional_duplicate"
        result["source"] = INTENTIONAL_DUPLICATE_SOURCE
    return result


_CSV_FIELDNAMES = [
    "card_name",
    "review_category",
    "duplicate_count",
    "representative_candidate_id",
    "prices",
    "links",
    "role_conflict",
    "special_flags",
    "conflicts",
    "integrable",
    "recommended_price_link_handling",
    "risks",
    "action_required",
    "intentional_duplicate_reason",
    "status",
    "source",
]


def _write_csv(reviews: list[DetailedGroupReview], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()
        for review in reviews:
            is_intentional = review.review_category == CATEGORY_INTENTIONAL
            writer.writerow(
                {
                    "card_name": review.card_name,
                    "review_category": review.review_category,
                    "duplicate_count": len(review.pages),
                    "representative_candidate_id": review.representative_candidate_id or "",
                    "prices": ", ".join(str(p) for p in review.prices),
                    "links": ", ".join(review.links),
                    "role_conflict": review.role_conflict,
                    "special_flags": ", ".join(review.special_flags),
                    "conflicts": "; ".join(
                        f"{c.property_name}:{'/'.join(c.values)}" for c in review.conflicts
                    ),
                    "integrable": review.integrable,
                    "recommended_price_link_handling": review.recommended_price_link_handling,
                    "risks": "; ".join(review.risks),
                    "action_required": not is_intentional,
                    "intentional_duplicate_reason": review.intentional_duplicate_reason or "",
                    "status": "intentional_duplicate" if is_intentional else "",
                    "source": INTENTIONAL_DUPLICATE_SOURCE if is_intentional else "",
                }
            )


def _write_markdown(reviews: list[DetailedGroupReview], path: Path) -> None:
    regular_reviews = [r for r in reviews if r.review_category in REVIEW_CATEGORY_LABELS]
    intentional_reviews = [r for r in reviews if r.review_category == CATEGORY_INTENTIONAL]

    counts = {cat: 0 for cat in REVIEW_CATEGORY_LABELS}
    for review in regular_reviews:
        counts[review.review_category] += 1

    lines = [
        "# 要確認グループ詳細分類レポート",
        "",
        f"- 要確認総数(A〜E対象): {len(regular_reviews)}",
        f"- {REVIEW_CATEGORY_LABELS[CATEGORY_PRICE_ONLY]}: {counts[CATEGORY_PRICE_ONLY]}",
        f"- {REVIEW_CATEGORY_LABELS[CATEGORY_SPECIAL_VERSION]}: {counts[CATEGORY_SPECIAL_VERSION]}",
        f"- {REVIEW_CATEGORY_LABELS[CATEGORY_IDENTITY_CONFLICT]}: "
        f"{counts[CATEGORY_IDENTITY_CONFLICT]}",
        f"- {REVIEW_CATEGORY_LABELS[CATEGORY_OTHER]}: {counts[CATEGORY_OTHER]}",
        f"- {REVIEW_CATEGORY_LABELS[CATEGORY_MANUAL]}: {counts[CATEGORY_MANUAL]}",
        f"- {INTENTIONAL_DUPLICATE_LABEL}: {len(intentional_reviews)}",
        "",
        f"価格・リンク差異のみで統合可能な件数: {counts[CATEGORY_PRICE_ONLY]}",
        f"特殊仕様により統合対象外候補となる件数: {counts[CATEGORY_SPECIAL_VERSION]}",
        f"カード同一性競合件数: {counts[CATEGORY_IDENTITY_CONFLICT]}",
        "",
        "推奨する次の適用範囲: "
        f"A分類({counts[CATEGORY_PRICE_ONLY]}件)について、価格・リンク処理方針の決定後に"
        "個別適用を検討する。B/C/D/Eは自動処理せず個別確認を継続する。",
        "",
        PRICE_LINK_STRATEGY_NOTES,
        COPY_DB_DESIGN_NOTES,
        "## 個別判断が必要なカード一覧",
        "",
    ]

    for category in (
        CATEGORY_PRICE_ONLY,
        CATEGORY_SPECIAL_VERSION,
        CATEGORY_IDENTITY_CONFLICT,
        CATEGORY_OTHER,
        CATEGORY_MANUAL,
    ):
        lines.append(f"### {REVIEW_CATEGORY_LABELS[category]}")
        lines.append("")
        group_list = [r for r in regular_reviews if r.review_category == category]
        if not group_list:
            lines.append("(該当なし)")
            lines.append("")
            continue
        for review in group_list:
            lines.append(f"#### {review.card_name}")
            lines.append(f"- 重複件数: {len(review.pages)}")
            lines.append(
                f"- 代表候補: {review.representative_candidate_id or '(未決定)'}"
            )
            lines.append(
                f"- 代表選択理由: {', '.join(review.representative_reasons) or '(なし)'}"
            )
            lines.append(f"- 現在の販売価格一覧: {review.prices or '(なし)'}")
            lines.append(f"- 現在の販売リンク一覧: {review.links or '(なし)'}")
            lines.append(f"- 採用デッキ統合後件数: {review.merged_deck_relation_count}")
            lines.append(
                f"- 旧統率者タグ和集合: {', '.join(review.merged_commander_tags) or '(なし)'}"
            )
            lines.append(f"- 想定所持枚数: {review.estimated_quantity}")
            lines.append(f"- 推奨価格・リンク処理: {review.recommended_price_link_handling}")
            lines.append(f"- 統合可能か: {'可能' if review.integrable else '不可(要判断)'}")
            lines.append(f"- リスク: {'; '.join(review.risks) or 'なし'}")
            lines.append("")

    lines.append(f"### {INTENTIONAL_DUPLICATE_LABEL}")
    lines.append("")
    if not intentional_reviews:
        lines.append("(該当なし)")
        lines.append("")
    else:
        for review in intentional_reviews:
            lines.append(f"#### {review.card_name}")
            lines.append(f"- ページ数: {len(review.pages)}")
            lines.append(f"- 理由: {review.intentional_duplicate_reason}")
            lines.append("- 状態: intentional_duplicate")
            lines.append("- 対応要否: 不要")
            lines.append(f"- ページID: {', '.join(p['id'] for p in review.pages)}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
