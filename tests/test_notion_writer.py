from __future__ import annotations

from mtg_notion_manager.models import DeckRecord, ExistingDeck
from mtg_notion_manager.notion.writer import NotionWriter

DATA_SOURCE_ID = "39aa97c8-7142-80a1-85c2-000b7f998d48"


class FakeNotionClient:
    """テスト用のNotionClient代替。実際のHTTP通信は行わない。"""

    def __init__(self, query_results: list[dict] | None = None) -> None:
        self.query_results = query_results or []
        self.created_pages: list[tuple[str, dict]] = []
        self.last_query: tuple[str, str, str] | None = None

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
        self.last_query = (data_source_id, title_property, title)
        return self.query_results

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        self.created_pages.append((data_source_id, properties))
        return {"id": "new-page-id", "properties": properties}


def _existing_page(
    name: str = "動き出した兵隊",
    commander: str = "茨の吟遊詩人、べロ",
    set_name: str = "ブルームバロウ",
    colors: list[str] | None = None,
    deck_list_url: str = "https://mtg-jp.com/reading/publicity/0038046/",
) -> dict:
    colors = colors if colors is not None else ["赤", "緑"]
    return {
        "id": "existing-page-id",
        "url": "https://www.notion.so/existing-page-id",
        "properties": {
            "名前": {"type": "title", "title": [{"plain_text": name}]},
            "統率者": {"type": "rich_text", "rich_text": [{"plain_text": commander}]},
            "発売セット": {"type": "select", "select": {"name": set_name}},
            "色": {"type": "multi_select", "multi_select": [{"name": c} for c in colors]},
            "デッキリスト": {"type": "url", "url": deck_list_url},
            "所有状況": {"type": "select", "select": {"name": "所有"}},
            "タイプ": {"type": "select", "select": {"name": "構築済み"}},
            "改造状況": {"type": "select", "select": {"name": "未改造"}},
        },
    }


def _sample_record(**overrides: object) -> DeckRecord:
    defaults = dict(
        name="動き出した兵隊",
        commander="茨の吟遊詩人、べロ",
        set_name="ブルームバロウ",
        colors=["赤", "緑"],
        deck_list_url="https://mtg-jp.com/reading/publicity/0038046/",
    )
    defaults.update(overrides)
    return DeckRecord(**defaults)


class TestFindExistingDeck:
    def test_returns_none_when_no_results(self) -> None:
        client = FakeNotionClient(query_results=[])
        writer = NotionWriter(client, DATA_SOURCE_ID)

        assert writer.find_existing_deck("存在しないデッキ") is None
        assert client.last_query == (DATA_SOURCE_ID, "名前", "存在しないデッキ")

    def test_returns_existing_deck_when_found(self) -> None:
        client = FakeNotionClient(query_results=[_existing_page()])
        writer = NotionWriter(client, DATA_SOURCE_ID)

        existing = writer.find_existing_deck("動き出した兵隊")
        assert existing is not None
        assert existing.page_id == "existing-page-id"
        assert existing.page_url == "https://www.notion.so/existing-page-id"


class TestDiffAgainst:
    def test_no_diff_when_identical(self) -> None:
        client = FakeNotionClient()
        writer = NotionWriter(client, DATA_SOURCE_ID)
        existing = ExistingDeck(
            page_id="existing-page-id",
            page_url="https://www.notion.so/existing-page-id",
            properties=_existing_page()["properties"],
        )
        record = _sample_record()

        assert writer.diff_against(existing, record) == []

    def test_detects_changed_properties(self) -> None:
        client = FakeNotionClient()
        writer = NotionWriter(client, DATA_SOURCE_ID)
        existing = ExistingDeck(
            page_id="existing-page-id",
            page_url="https://www.notion.so/existing-page-id",
            properties=_existing_page(commander="旧統率者", colors=["赤"])["properties"],
        )
        record = _sample_record(commander="茨の吟遊詩人、べロ", colors=["赤", "緑"])

        diff = writer.diff_against(existing, record)
        diff_props = {entry.property_name for entry in diff}

        assert "統率者" in diff_props
        assert "色" in diff_props
        assert "発売セット" not in diff_props


class TestCreateDeck:
    def test_calls_client_with_notion_properties(self) -> None:
        client = FakeNotionClient()
        writer = NotionWriter(client, DATA_SOURCE_ID)
        record = _sample_record()

        writer.create_deck(record)

        assert len(client.created_pages) == 1
        data_source_id, properties = client.created_pages[0]
        assert data_source_id == DATA_SOURCE_ID
        assert properties["名前"]["title"][0]["text"]["content"] == "動き出した兵隊"
        assert properties["所有状況"]["select"]["name"] == "所有"
        assert properties["タイプ"]["select"]["name"] == "構築済み"
        assert properties["改造状況"]["select"]["name"] == "未改造"
