"""MTGカードDBの索引化・重複判定・作成/更新を担当する。

実スキーマ(2026-07-11実測)には「枚数」「Staple」プロパティが存在しないため、
このリポジトリはそれらを一切読み書きしない(quantityはNotionへは書き込まず、
呼び出し側のデッキ合計枚数検証にのみ使う)。
"""

from __future__ import annotations

from dataclasses import dataclass

from mtg_notion_manager.card_match_overrides import CardMatchOverrides
from mtg_notion_manager.exceptions import CardMatchOverrideError
from mtg_notion_manager.models import DeckCard, ExistingCard
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.parsers.card_names import normalize_card_name

TITLE_PROPERTY = "カード名"
ENGLISH_NAME_PROPERTY = "英語名"
OWNED_PROPERTY = "所持"
DECKS_RELATION_PROPERTY = "採用デッキ"
NOTE_PROPERTY = "メモ"
MERGED_PROPERTY = "統合済み"


@dataclass(frozen=True)
class CardMatch:
    """カードDB内の重複判定結果。

    一意に決まった場合は card に結果を、複数候補があり決定できない場合は
    ambiguous_candidates に候補一覧を入れる(両方空なら新規)。
    """

    card: ExistingCard | None
    ambiguous_candidates: list[ExistingCard]
    override_reason: str | None = None

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.ambiguous_candidates)


class CardRepository:
    """カードDBをメモリ上に索引化し、重複検索・作成・更新を行う。

    100回個別検索する代わりに、load() で全件を1度だけ取得してインデックスを作る。
    """

    def __init__(
        self,
        client: NotionClient,
        data_source_id: str,
        overrides: CardMatchOverrides | None = None,
    ) -> None:
        self._client = client
        self._data_source_id = data_source_id
        self._by_english_name: dict[str, list[ExistingCard]] = {}
        self._by_japanese_name: dict[str, list[ExistingCard]] = {}
        self._by_page_id: dict[str, ExistingCard] = {}
        self._loaded = False
        self._overrides = overrides or CardMatchOverrides(
            by_japanese_name={}, by_english_name={}
        )

    def load(self) -> None:
        if self._loaded:
            return
        pages = self._client.query_data_source_all(self._data_source_id)
        for page in pages:
            if _is_merged(page):
                # dedupe-cards により統合済みとしてマークされたページは、
                # 代表ページに情報が集約済みのため索引・照合の対象から除外する。
                continue
            existing = _to_existing_card(page)
            props = page.get("properties", {})
            name_ja = _plain_text(props.get(TITLE_PROPERTY))
            name_en = _plain_text(props.get(ENGLISH_NAME_PROPERTY))
            if name_ja:
                self._by_japanese_name.setdefault(normalize_card_name(name_ja), []).append(existing)
            if name_en:
                self._by_english_name.setdefault(normalize_card_name(name_en), []).append(existing)
            self._by_page_id[existing.page_id] = existing
        self._loaded = True

    def get_by_page_id(self, page_id: str) -> ExistingCard | None:
        """load()済みの索引からpage_idで既存カードを引く(追加のAPI呼び出しなし)。"""
        if not self._loaded:
            raise RuntimeError("CardRepository.load() を先に呼んでください")
        return self._by_page_id.get(page_id)

    def candidates_by_english_name(self, name: str) -> list[ExistingCard]:
        """指定した英語名(未正規化)に完全一致する候補一覧を返す(doctorの検証用)。"""
        if not self._loaded:
            raise RuntimeError("CardRepository.load() を先に呼んでください")
        return list(self._by_english_name.get(normalize_card_name(name), []))

    def candidates_by_japanese_name(self, name: str) -> list[ExistingCard]:
        """指定した日本語名(未正規化)に完全一致する候補一覧を返す(doctorの検証用)。"""
        if not self._loaded:
            raise RuntimeError("CardRepository.load() を先に呼んでください")
        return list(self._by_japanese_name.get(normalize_card_name(name), []))

    def find_match(self, card: DeckCard) -> CardMatch:
        """英語名→日本語名の順で完全一致を検索する。

        英語名で一致すれば日本語名は見ない(仕様どおり英語名を第一候補とする)。
        複数候補になった場合は card_match_overrides を確認し、指定page_idが
        候補内にあればそれを採用する(fuzzy matchや自動選択は一切行わない)。
        """
        if not self._loaded:
            raise RuntimeError("CardRepository.load() を先に呼んでください")

        if card.name_en:
            candidates = self._by_english_name.get(normalize_card_name(card.name_en), [])
            if len(candidates) == 1:
                return CardMatch(card=candidates[0], ambiguous_candidates=[])
            if len(candidates) > 1:
                return self._resolve_ambiguous(card, candidates)

        if card.name_ja:
            candidates = self._by_japanese_name.get(normalize_card_name(card.name_ja), [])
            if len(candidates) == 1:
                return CardMatch(card=candidates[0], ambiguous_candidates=[])
            if len(candidates) > 1:
                return self._resolve_ambiguous(card, candidates)

        return CardMatch(card=None, ambiguous_candidates=[])

    def _resolve_ambiguous(self, card: DeckCard, candidates: list[ExistingCard]) -> CardMatch:
        override = self._overrides.resolve(card.name_ja, card.name_en)
        if override is None:
            return CardMatch(card=None, ambiguous_candidates=candidates)

        matched = next(
            (c for c in candidates if c.page_id == override.canonical_page_id), None
        )
        if matched is None:
            candidate_ids = [c.page_id for c in candidates]
            raise CardMatchOverrideError(
                f"カード '{card.display_name}' のオーバーライド指定"
                f" page_id '{override.canonical_page_id}' が曖昧一致の候補内に"
                f" 見つかりません(候補: {candidate_ids})。"
                " config/card_match_overrides.json を確認してください。"
            )
        return CardMatch(card=matched, ambiguous_candidates=[], override_reason=override.reason)

    def get_deck_relation_ids(self, existing: ExistingCard) -> list[str]:
        """「採用デッキ」リレーションの全ページIDを取得する(25件超はページングして取得)。"""
        return self._client.read_relation_ids(
            existing.properties, existing.page_id, DECKS_RELATION_PROPERTY
        )

    def is_owned(self, existing: ExistingCard) -> bool:
        prop = existing.properties.get(OWNED_PROPERTY, {})
        return bool(prop.get("checkbox"))

    def apply_relation_update(
        self, existing: ExistingCard, deck_page_id: str, current_deck_ids: list[str]
    ) -> dict:
        """既存カードへ採用デッキを追記し、未所持なら所持=trueにする。

        current_deck_ids は呼び出し側が事前に get_deck_relation_ids() で
        取得済みの値を渡す(冪等性判定と二重APIコールを避けるため)。
        """
        properties: dict = {}
        if deck_page_id not in current_deck_ids:
            new_ids = [*current_deck_ids, deck_page_id]
            properties[DECKS_RELATION_PROPERTY] = {"relation": [{"id": pid} for pid in new_ids]}
        if not self.is_owned(existing):
            properties[OWNED_PROPERTY] = {"checkbox": True}

        if not properties:
            return {"id": existing.page_id, "url": existing.page_url}
        return self._client.update_page(existing.page_id, properties)

    def create_card(self, card: DeckCard, deck_page_id: str, note: str = "") -> dict:
        """新規カードを作成する。確実に取得できた項目のみ設定する。"""
        properties: dict = {
            TITLE_PROPERTY: {"title": [{"text": {"content": card.display_name}}]},
            OWNED_PROPERTY: {"checkbox": True},
            DECKS_RELATION_PROPERTY: {"relation": [{"id": deck_page_id}]},
        }
        if card.name_en:
            properties[ENGLISH_NAME_PROPERTY] = {"rich_text": [{"text": {"content": card.name_en}}]}
        if note:
            properties[NOTE_PROPERTY] = {"rich_text": [{"text": {"content": note}}]}

        return self._client.create_page(self._data_source_id, properties)


def _is_merged(page: dict) -> bool:
    prop = page.get("properties", {}).get(MERGED_PROPERTY)
    if prop is None:
        return False
    return bool(prop.get("checkbox"))


def _to_existing_card(page: dict) -> ExistingCard:
    return ExistingCard(
        page_id=page["id"],
        page_url=page.get("url", ""),
        properties=page.get("properties", {}),
    )


def _plain_text(prop: dict | None) -> str | None:
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
