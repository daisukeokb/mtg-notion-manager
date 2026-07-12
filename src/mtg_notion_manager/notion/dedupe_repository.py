"""MTGカードDBの重複統合(dedupe-cards)機能用のNotionアクセス層。

CardRepository(import-cards用)とは責務を分ける:
- CardRepository: 1カードずつの重複検索・作成・リレーション追加
- DedupeRepository: カードDB全件を「同名グループ」として俯瞰し、
  スキーマ変更・統合適用(ページ更新)を行う
"""

from __future__ import annotations

from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.parsers.card_names import normalize_card_name

TITLE_PROPERTY = "カード名"
ENGLISH_NAME_PROPERTY = "英語名"
OWNED_PROPERTY = "所持"
DECKS_RELATION_PROPERTY = "採用デッキ"
QUANTITY_PROPERTY = "所持枚数"
MERGED_PROPERTY = "統合済み"
NOTE_PROPERTY = "メモ"

# スキーマ変更で追加するプロパティの定義(MVP最小構成)。
SCHEMA_ADDITIONS: dict[str, dict] = {
    QUANTITY_PROPERTY: {"number": {"format": "number"}},
    MERGED_PROPERTY: {"checkbox": {}},
}


class DedupeRepository:
    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client = client
        self._data_source_id = data_source_id
        self._pages: list[dict] = []
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._pages = self._client.query_data_source_all(self._data_source_id)
        self._loaded = True

    def get_schema(self) -> dict:
        return self._client.get_data_source(self._data_source_id)

    def missing_schema_properties(self) -> list[str]:
        """SCHEMA_ADDITIONS のうち、まだNotion側に存在しないプロパティ名を返す。"""
        schema = self.get_schema().get("properties", {})
        return [name for name in SCHEMA_ADDITIONS if name not in schema]

    def apply_schema_migration(self, property_names: list[str]) -> dict:
        properties = {name: SCHEMA_ADDITIONS[name] for name in property_names}
        return self._client.update_data_source_schema(self._data_source_id, properties)

    def active_pages(self) -> list[dict]:
        """統合済みでないページ一覧(冪等性の要: 統合済みは重複候補から除外する)。"""
        if not self._loaded:
            raise RuntimeError("DedupeRepository.load() を先に呼んでください")
        return [page for page in self._pages if not _is_merged(page)]

    def find_duplicate_groups(self, card_name: str | None = None) -> dict[str, list[dict]]:
        """正規化カード名でグループ化し、2件以上のグループのみ返す。"""
        groups: dict[str, list[dict]] = {}
        target_key = normalize_card_name(card_name) if card_name is not None else None

        for page in self.active_pages():
            title = _plain_text(page, TITLE_PROPERTY)
            if not title:
                continue
            key = normalize_card_name(title)
            if target_key is not None and key != target_key:
                continue
            groups.setdefault(key, []).append(page)

        return {key: pages for key, pages in groups.items() if len(pages) > 1}

    def get_full_relation_ids(
        self, page: dict, property_name: str = DECKS_RELATION_PROPERTY
    ) -> list[str]:
        prop = page.get("properties", {}).get(property_name, {})
        relation = prop.get("relation", [])
        if not prop.get("has_more"):
            return [item["id"] for item in relation]

        property_id = prop.get("id")
        if not property_id:
            return [item["id"] for item in relation]
        items = self._client.get_page_property_item(page["id"], property_id)
        return [
            item["relation"]["id"]
            for item in items
            if item.get("type") == "relation" and "relation" in item
        ]

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._client.update_page(page_id, properties)


def _is_merged(page: dict) -> bool:
    prop = page.get("properties", {}).get(MERGED_PROPERTY)
    if prop is None:
        return False
    return bool(prop.get("checkbox"))


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
