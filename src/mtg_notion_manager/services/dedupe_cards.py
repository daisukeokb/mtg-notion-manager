"""MTGカードDBの重複統合(dedupe-cards)オーケストレーション。

冪等性の方針: 統合済み(=true)のページは常に重複候補から除外される
(DedupeRepository.active_pages())。そのため代表レコード1件だけが残った
グループはサイズ1になり自動的に対象外となり、再実行しても何も変化しない。

1グループの競合/代表決定失敗が他のグループの計画作成を止めないよう、
グループ単位でエラーを分離して DedupePlan.group_errors に集める。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from mtg_notion_manager.exceptions import (
    ConflictError,
    NotionAPIError,
    RepresentativeSelectionError,
)
from mtg_notion_manager.notion.dedupe_repository import (
    DECKS_RELATION_PROPERTY,
    ENGLISH_NAME_PROPERTY,
    MERGED_PROPERTY,
    NOTE_PROPERTY,
    OWNED_PROPERTY,
    QUANTITY_PROPERTY,
    DedupeRepository,
)

# 単一値として扱い、競合したら停止するプロパティ(rich_text/select)。
SINGLE_VALUED_TEXT_PROPERTIES = (ENGLISH_NAME_PROPERTY,)
SINGLE_VALUED_SELECT_PROPERTIES = ("タイプ",)

# 和集合として扱う複数値プロパティ(multi_select)。
MULTI_VALUED_PROPERTIES = ("シンボル", "役割（標準）", "旧タグ", "統率者")


@dataclass(frozen=True)
class DuplicateGroup:
    card_name: str
    pages: list[dict]


@dataclass(frozen=True)
class RepresentativeChoice:
    page: dict
    reasons: list[str]


@dataclass(frozen=True)
class MergePlan:
    group: DuplicateGroup
    representative: RepresentativeChoice
    merged_deck_relation_ids: list[str]
    owned: bool
    quantity: int
    english_name: str | None
    single_valued_attributes: dict[str, str | None]
    multi_valued_attributes: dict[str, list[str]]
    duplicate_pages: list[dict] = field(default_factory=list)

    @property
    def representative_page_id(self) -> str:
        return self.representative.page["id"]

    @property
    def representative_page_url(self) -> str:
        return self.representative.page.get("url", "")


@dataclass(frozen=True)
class GroupError:
    card_name: str
    pages: list[dict]
    error_type: str  # "conflict" | "representative_selection"
    message: str


@dataclass(frozen=True)
class DedupePlan:
    merge_plans: list[MergePlan]
    group_errors: list[GroupError]
    schema_missing_properties: list[str]

    @property
    def has_schema_gap(self) -> bool:
        return bool(self.schema_missing_properties)

    @property
    def has_group_errors(self) -> bool:
        return bool(self.group_errors)


def build_dedupe_plan(
    repo: DedupeRepository,
    card_name: str | None = None,
    representative_page_id: str | None = None,
) -> DedupePlan:
    """重複グループを検出し、代表レコードと統合内容の計画を作る(書き込みなし)。

    representative_page_id は card_name で1グループに絞り込んだ場合のみ有効
    (複数グループに対して同じページIDを強制すると意味を成さないため)。
    """
    repo.load()
    missing_schema = repo.missing_schema_properties()

    groups = repo.find_duplicate_groups(card_name)

    merge_plans: list[MergePlan] = []
    group_errors: list[GroupError] = []

    for key, pages in groups.items():
        group = DuplicateGroup(card_name=key, pages=pages)
        override = representative_page_id if card_name is not None else None
        try:
            merge_plans.append(_build_merge_plan(repo, group, override))
        except RepresentativeSelectionError as exc:
            group_errors.append(
                GroupError(
                    card_name=key,
                    pages=pages,
                    error_type="representative_selection",
                    message=str(exc),
                )
            )
        except ConflictError as exc:
            group_errors.append(
                GroupError(card_name=key, pages=pages, error_type="conflict", message=str(exc))
            )

    return DedupePlan(
        merge_plans=merge_plans, group_errors=group_errors, schema_missing_properties=missing_schema
    )


def _build_merge_plan(
    repo: DedupeRepository, group: DuplicateGroup, representative_page_id: str | None
) -> MergePlan:
    if representative_page_id is not None:
        representative = _use_manual_representative(group.pages, representative_page_id)
    else:
        representative = _choose_representative(repo, group.pages)

    duplicate_pages = [p for p in group.pages if p["id"] != representative.page["id"]]

    all_relation_ids: list[str] = []
    seen_relation_ids: set[str] = set()
    for page in group.pages:
        for rid in repo.get_full_relation_ids(page, DECKS_RELATION_PROPERTY):
            if rid not in seen_relation_ids:
                seen_relation_ids.add(rid)
                all_relation_ids.append(rid)

    owned = any(_checkbox(page, OWNED_PROPERTY) for page in group.pages)

    single_valued: dict[str, str | None] = {}
    for prop_name in (*SINGLE_VALUED_TEXT_PROPERTIES, *SINGLE_VALUED_SELECT_PROPERTIES):
        single_valued[prop_name] = _merge_single_valued(group.pages, prop_name)

    multi_valued: dict[str, list[str]] = {
        prop_name: _merge_multi_valued(group.pages, prop_name)
        for prop_name in MULTI_VALUED_PROPERTIES
    }

    return MergePlan(
        group=group,
        representative=representative,
        merged_deck_relation_ids=all_relation_ids,
        owned=owned,
        # 既に代表ページに設定済みの所持枚数を下回らないようにする(部分失敗後の
        # 再実行でグループが縮小しても枚数が減らないようにするため)。
        quantity=int(max(_number(representative.page, QUANTITY_PROPERTY) or 0, len(group.pages))),
        english_name=single_valued[ENGLISH_NAME_PROPERTY],
        single_valued_attributes=single_valued,
        multi_valued_attributes=multi_valued,
        duplicate_pages=duplicate_pages,
    )


def _use_manual_representative(
    pages: list[dict], representative_page_id: str
) -> RepresentativeChoice:
    for page in pages:
        if page["id"] == representative_page_id:
            return RepresentativeChoice(page=page, reasons=["--representative-page-id で手動指定"])
    raise RepresentativeSelectionError(
        f"指定されたページID '{representative_page_id}' はこの重複グループに含まれていません。"
    )


@dataclass(frozen=True)
class RepresentativeEvaluation:
    """代表レコード選択の評価結果(例外を送出しない版)。

    winner が None の場合は同点で自動決定できないことを意味し、
    tied_candidates に同点候補が入る(audit-duplicates で利用する)。
    """

    winner: dict | None
    reasons: list[str]
    tied_candidates: list[dict] = field(default_factory=list)


def evaluate_representative(repo: DedupeRepository, pages: list[dict]) -> RepresentativeEvaluation:
    """代表レコード候補を評価する(同点でも例外を出さず結果を返す)。"""
    scored = []
    for page in pages:
        has_english = bool(_plain_text(page, ENGLISH_NAME_PROPERTY))
        deck_count = len(repo.get_full_relation_ids(page, DECKS_RELATION_PROPERTY))
        attr_count = _count_filled_attributes(page)
        last_edited = page.get("last_edited_time", "")
        created = page.get("created_time", "")
        scored.append((page, has_english, deck_count, attr_count, last_edited, created))

    # 優先順位: 英語名あり > 採用デッキ数 > 属性数 > 最終更新日時(新しい) の順に降順ソート。
    ranked = sorted(scored, key=lambda item: (item[1], item[2], item[3], item[4]), reverse=True)

    top_rank_key = ranked[0][1:5]
    tied = [item for item in ranked if item[1:5] == top_rank_key]

    if len(tied) > 1:
        # created_time が古い方を優先する最終タイブレーク
        tied_sorted = sorted(tied, key=lambda item: item[5])
        oldest_created = tied_sorted[0][5]
        still_tied = [item for item in tied_sorted if item[5] == oldest_created]
        if len(still_tied) > 1:
            return RepresentativeEvaluation(
                winner=None, reasons=[], tied_candidates=[item[0] for item in still_tied]
            )
        winner = tied_sorted[0][0]
        reasons = _describe_reasons(tied_sorted[0], tie_broken_by_created=True)
        return RepresentativeEvaluation(winner=winner, reasons=reasons)

    winner = ranked[0][0]
    reasons = _describe_reasons(ranked[0])
    return RepresentativeEvaluation(winner=winner, reasons=reasons)


def _choose_representative(repo: DedupeRepository, pages: list[dict]) -> RepresentativeChoice:
    evaluation = evaluate_representative(repo, pages)
    if evaluation.winner is None:
        candidates = ", ".join(
            p.get("url", p["id"]) for p in evaluation.tied_candidates
        )
        raise RepresentativeSelectionError(
            f"代表レコードを一意に決定できません(候補: {candidates})。"
            " --representative-page-id で手動指定してください。"
        )
    return RepresentativeChoice(page=evaluation.winner, reasons=evaluation.reasons)


def _describe_reasons(scored_item: tuple, tie_broken_by_created: bool = False) -> list[str]:
    _, has_english, deck_count, attr_count, _last_edited, _created = scored_item
    reasons = [
        "英語名あり" if has_english else "英語名なし",
        f"採用デッキ{deck_count}件",
        f"属性情報{attr_count}件",
    ]
    reasons.append(
        "作成日時が最も古い(同条件のため)" if tie_broken_by_created else "最終更新日時が最新"
    )
    return reasons


_DESCRIPTIVE_ATTRIBUTE_PROPERTIES = (
    "タイプ",
    "シンボル",
    "役割（標準）",
    "優先度",
    "メモ",
    "旧タグ",
    "統率者",
    "販売リンク",
    "販売価格",
)


def _count_filled_attributes(page: dict) -> int:
    count = 0
    for prop_name in _DESCRIPTIVE_ATTRIBUTE_PROPERTIES:
        prop = page.get("properties", {}).get(prop_name)
        if prop is None:
            continue
        prop_type = prop.get("type")
        if prop_type == "rich_text":
            if _plain_text(page, prop_name):
                count += 1
        elif prop_type == "select":
            if prop.get("select") is not None:
                count += 1
        elif prop_type == "multi_select":
            if prop.get("multi_select"):
                count += 1
        elif prop_type == "url":
            if prop.get("url"):
                count += 1
        elif prop_type == "number":
            if prop.get("number") is not None:
                count += 1
    return count


def _merge_single_valued(pages: list[dict], prop_name: str) -> str | None:
    values: dict[str, None] = {}
    for page in pages:
        prop = page.get("properties", {}).get(prop_name)
        if prop is None:
            continue
        prop_type = prop.get("type")
        if prop_type == "rich_text":
            value = _plain_text(page, prop_name)
        elif prop_type == "select":
            select = prop.get("select")
            value = select.get("name") if select else None
        else:
            value = None
        if value:
            values[value] = None

    if len(values) > 1:
        raise ConflictError(
            f"「{prop_name}」の値が競合しています(候補: {sorted(values)})。"
            " 手動で解決してから再実行してください。"
        )
    return next(iter(values), None)


def _merge_multi_valued(pages: list[dict], prop_name: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for page in pages:
        prop = page.get("properties", {}).get(prop_name, {})
        for opt in prop.get("multi_select", []):
            name = opt.get("name")
            if name and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _checkbox(page: dict, prop_name: str) -> bool:
    prop = page.get("properties", {}).get(prop_name, {})
    return bool(prop.get("checkbox"))


def _number(page: dict, prop_name: str) -> float | None:
    prop = page.get("properties", {}).get(prop_name)
    if prop is None or prop.get("type") != "number":
        return None
    return prop.get("number")


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


def build_representative_update(plan: MergePlan) -> dict:
    """代表レコードへ適用するNotionプロパティ更新差分を作る。"""
    properties: dict = {
        OWNED_PROPERTY: {"checkbox": plan.owned},
        QUANTITY_PROPERTY: {"number": plan.quantity},
        DECKS_RELATION_PROPERTY: {
            "relation": [{"id": rid} for rid in plan.merged_deck_relation_ids]
        },
    }

    if plan.english_name:
        properties[ENGLISH_NAME_PROPERTY] = {
            "rich_text": [{"text": {"content": plan.english_name}}]
        }

    card_type = plan.single_valued_attributes.get("タイプ")
    if card_type:
        properties["タイプ"] = {"select": {"name": card_type}}

    for prop_name in MULTI_VALUED_PROPERTIES:
        values = plan.multi_valued_attributes.get(prop_name, [])
        if values:
            properties[prop_name] = {"multi_select": [{"name": v} for v in values]}

    existing_note = _plain_text(plan.representative.page, NOTE_PROPERTY)
    merged_ids_note = ", ".join(p["id"] for p in plan.duplicate_pages)
    migration_note = (
        f"[統合] {_today()}: 重複ページ{len(plan.duplicate_pages)}件を統合({merged_ids_note})"
    )
    combined_note = f"{existing_note}\n{migration_note}" if existing_note else migration_note
    properties[NOTE_PROPERTY] = {"rich_text": [{"text": {"content": combined_note}}]}

    return properties


def build_duplicate_page_update(plan: MergePlan, duplicate_page: dict) -> dict:
    """統合される側(非代表)のページへ適用するNotionプロパティ更新差分を作る。"""
    existing_note = _plain_text(duplicate_page, NOTE_PROPERTY)
    reference_note = f"[統合済み] 代表ページ: {plan.representative_page_url}"
    combined_note = f"{existing_note}\n{reference_note}" if existing_note else reference_note
    return {
        MERGED_PROPERTY: {"checkbox": True},
        NOTE_PROPERTY: {"rich_text": [{"text": {"content": combined_note}}]},
    }


def _today() -> str:
    return datetime.date.today().isoformat()


@dataclass(frozen=True)
class GroupApplyResult:
    card_name: str
    representative_page_id: str
    representative_updated: bool
    duplicate_page_ids_marked: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class DedupeApplyResult:
    results: list[GroupApplyResult]

    @property
    def succeeded(self) -> list[GroupApplyResult]:
        return [r for r in self.results if r.error is None]

    @property
    def failed(self) -> list[GroupApplyResult]:
        return [r for r in self.results if r.error is not None]


def execute_dedupe_plan(plan: DedupePlan, repo: DedupeRepository) -> DedupeApplyResult:
    """計画をNotionへ適用する(代表レコード更新 → 重複ページに統合済み設定の順)。

    グループ単位で独立して適用する。1グループの失敗が他グループの適用を止めない。
    """
    results: list[GroupApplyResult] = []
    for merge_plan in plan.merge_plans:
        results.append(_apply_one_group(merge_plan, repo))
    return DedupeApplyResult(results=results)


def _apply_one_group(merge_plan: MergePlan, repo: DedupeRepository) -> GroupApplyResult:
    try:
        representative_properties = build_representative_update(merge_plan)
        repo.update_page(merge_plan.representative_page_id, representative_properties)

        marked: list[str] = []
        for duplicate_page in merge_plan.duplicate_pages:
            duplicate_properties = build_duplicate_page_update(merge_plan, duplicate_page)
            repo.update_page(duplicate_page["id"], duplicate_properties)
            marked.append(duplicate_page["id"])

        return GroupApplyResult(
            card_name=merge_plan.group.card_name,
            representative_page_id=merge_plan.representative_page_id,
            representative_updated=True,
            duplicate_page_ids_marked=marked,
        )
    except NotionAPIError as exc:
        return GroupApplyResult(
            card_name=merge_plan.group.card_name,
            representative_page_id=merge_plan.representative_page_id,
            representative_updated=False,
            error=str(exc),
        )
