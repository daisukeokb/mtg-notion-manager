from __future__ import annotations

from mtg_notion_manager.notion.dedupe_repository import DedupeRepository

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class FakeNotionClient:
    def __init__(self, pages: list[dict] | None = None, schema: dict | None = None) -> None:
        self.pages = pages or []
        self.schema = schema or {"properties": {}}
        self.updated_pages: list[tuple[str, dict]] = []
        self.schema_updates: list[tuple[str, dict]] = []
        self.property_item_calls: list[tuple[str, str]] = []
        self._property_items: dict[tuple[str, str], list[dict]] = {}

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self.pages

    def get_data_source(self, data_source_id: str) -> dict:
        return self.schema

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        self.schema_updates.append((data_source_id, properties))
        self.schema["properties"].update(properties)
        return self.schema

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.updated_pages.append((page_id, properties))
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        self.property_item_calls.append((page_id, property_id))
        return self._property_items.get((page_id, property_id), [])

    def set_property_items(self, page_id: str, property_id: str, items: list[dict]) -> None:
        self._property_items[(page_id, property_id)] = items


def _page(
    page_id: str,
    name: str,
    merged: bool = False,
    deck_ids: list[str] | None = None,
    relation_has_more: bool = False,
) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "カード名": {"type": "title", "title": [{"plain_text": name}]},
            "統合済み": {"type": "checkbox", "checkbox": merged},
            "採用デッキ": {
                "type": "relation",
                "id": "rel-prop",
                "relation": [{"id": rid} for rid in (deck_ids or [])],
                "has_more": relation_has_more,
            },
        },
    }


class TestFindDuplicateGroups:
    def test_groups_pages_with_same_normalized_title(self) -> None:
        pages = [_page("p1", "沼"), _page("p2", "沼"), _page("p3", "山")]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)
        repo.load()

        groups = repo.find_duplicate_groups()

        assert len(groups) == 1
        key = next(iter(groups))
        assert len(groups[key]) == 2

    def test_normalization_absorbs_whitespace_differences(self) -> None:
        pages = [_page("p1", "Sol Ring"), _page("p2", "  sol   ring  ")]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)
        repo.load()

        groups = repo.find_duplicate_groups()

        assert len(groups) == 1

    def test_single_record_is_not_a_group(self) -> None:
        pages = [_page("p1", "統率の塔")]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)
        repo.load()

        groups = repo.find_duplicate_groups()

        assert groups == {}

    def test_already_merged_pages_are_excluded(self) -> None:
        pages = [_page("p1", "沼"), _page("p2", "沼", merged=True), _page("p3", "沼", merged=True)]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)
        repo.load()

        groups = repo.find_duplicate_groups()

        # p1のみアクティブなので単一レコード扱い(重複グループなし)
        assert groups == {}

    def test_card_name_filter_restricts_to_one_group(self) -> None:
        pages = [_page("p1", "沼"), _page("p2", "沼"), _page("p3", "山"), _page("p4", "山")]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)
        repo.load()

        groups = repo.find_duplicate_groups(card_name="山")

        assert len(groups) == 1
        assert len(next(iter(groups.values()))) == 2


class TestGetFullRelationIds:
    def test_returns_ids_when_not_truncated(self) -> None:
        page = _page("p1", "沼", deck_ids=["d1", "d2"])
        client = FakeNotionClient([page])
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        ids = repo.get_full_relation_ids(page)

        assert ids == ["d1", "d2"]
        assert client.property_item_calls == []

    def test_paginates_when_truncated(self) -> None:
        page = _page("p1", "沼", deck_ids=["d1"], relation_has_more=True)
        client = FakeNotionClient([page])
        client.set_property_items(
            "p1",
            "rel-prop",
            [
                {"type": "relation", "relation": {"id": "d1"}},
                {"type": "relation", "relation": {"id": "d2"}},
            ],
        )
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        ids = repo.get_full_relation_ids(page)

        assert ids == ["d1", "d2"]


class TestSchemaMigration:
    def test_missing_schema_properties_detects_gap(self) -> None:
        client = FakeNotionClient(schema={"properties": {}})
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        missing = repo.missing_schema_properties()

        assert set(missing) == {"所持枚数", "統合済み"}

    def test_missing_schema_properties_empty_when_present(self) -> None:
        client = FakeNotionClient(
            schema={
                "properties": {
                    "所持枚数": {"type": "number"},
                    "統合済み": {"type": "checkbox"},
                }
            }
        )
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        assert repo.missing_schema_properties() == []

    def test_apply_schema_migration_sends_only_requested_properties(self) -> None:
        client = FakeNotionClient(schema={"properties": {}})
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        repo.apply_schema_migration(["所持枚数"])

        assert len(client.schema_updates) == 1
        _, properties = client.schema_updates[0]
        assert list(properties.keys()) == ["所持枚数"]
