from __future__ import annotations

from dataclasses import dataclass

from mtg_notion_manager.models import DeckRecord, ExistingDeck
from mtg_notion_manager.notion.client import NotionClient

TITLE_PROPERTY = "名前"


@dataclass(frozen=True)
class DiffEntry:
    property_name: str
    existing_value: object
    new_value: object


class NotionWriter:
    """MTG統率者DBに対する重複検索・差分表示・書き込みを担当する。"""

    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client = client
        self._data_source_id = data_source_id

    def find_existing_deck(self, name: str) -> ExistingDeck | None:
        results = self._client.query_data_source_by_title(
            self._data_source_id, TITLE_PROPERTY, name
        )
        if not results:
            return None
        page = results[0]
        return ExistingDeck(
            page_id=page["id"],
            page_url=page.get("url", ""),
            properties=page.get("properties", {}),
        )

    def diff_against(self, existing: ExistingDeck, record: DeckRecord) -> list[DiffEntry]:
        existing_values = _extract_comparable_values(existing.properties)
        new_values = record.to_preview_dict()

        diffs: list[DiffEntry] = []
        for key, new_value in new_values.items():
            if key == "名前":
                continue
            old_value = existing_values.get(key)
            if _normalize_for_compare(old_value) != _normalize_for_compare(new_value):
                diffs.append(DiffEntry(key, old_value, new_value))
        return diffs

    def create_deck(self, record: DeckRecord) -> dict:
        return self._client.create_page(self._data_source_id, record.to_notion_properties())


def _extract_comparable_values(properties: dict) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, prop in properties.items():
        prop_type = prop.get("type")
        if prop_type == "title":
            values[key] = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        elif prop_type == "rich_text":
            values[key] = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
        elif prop_type == "select":
            select = prop.get("select")
            values[key] = select.get("name") if select else None
        elif prop_type == "multi_select":
            values[key] = [item.get("name") for item in prop.get("multi_select", [])]
        elif prop_type == "url":
            values[key] = prop.get("url")
    return values


def _normalize_for_compare(value: object) -> object:
    if isinstance(value, list):
        return sorted(value)
    return value
