from __future__ import annotations

from mtg_notion_manager.models import DeckCard
from mtg_notion_manager.notion.card_repository import CardRepository

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class FakeNotionClient:
    """テスト用のNotionClient代替。実際のHTTP通信は行わない。"""

    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = pages or []
        self.created_pages: list[tuple[str, dict]] = []
        self.updated_pages: list[tuple[str, dict]] = []
        self.property_item_calls: list[tuple[str, str]] = []
        self._property_items: dict[tuple[str, str], list[dict]] = {}

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self.pages

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        self.created_pages.append((data_source_id, properties))
        return {"id": "new-card-id", "url": "https://notion.so/new-card-id"}

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


def _card_page(
    page_id: str,
    name_ja: str,
    name_en: str | None = None,
    owned: bool = False,
    deck_relation_ids: list[str] | None = None,
    relation_has_more: bool = False,
    relation_property_id: str = "rel-prop-id",
    merged: bool = False,
) -> dict:
    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name_ja}]},
        "所持": {"type": "checkbox", "checkbox": owned},
        "統合済み": {"type": "checkbox", "checkbox": merged},
        "採用デッキ": {
            "type": "relation",
            "id": relation_property_id,
            "relation": [{"id": rid} for rid in (deck_relation_ids or [])],
            "has_more": relation_has_more,
        },
    }
    if name_en is not None:
        properties["英語名"] = {"type": "rich_text", "rich_text": [{"plain_text": name_en}]}
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": properties,
    }


def _deck_card(
    name_ja: str | None = None, name_en: str | None = None, quantity: int = 1
) -> DeckCard:
    return DeckCard(
        name_ja=name_ja,
        name_en=name_en,
        quantity=quantity,
        is_commander=False,
        source_url="https://example.com",
    )


class TestFindMatch:
    def test_matches_by_english_name_first(self) -> None:
        pages = [_card_page("p1", "沼", name_en="Swamp")]
        client = FakeNotionClient(pages)
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        match = repo.find_match(_deck_card(name_ja="沼", name_en="Swamp"))

        assert match.card is not None
        assert match.card.page_id == "p1"
        assert not match.is_ambiguous

    def test_falls_back_to_japanese_name_when_no_english_name(self) -> None:
        pages = [_card_page("p1", "沼")]
        client = FakeNotionClient(pages)
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        match = repo.find_match(_deck_card(name_ja="沼"))

        assert match.card is not None
        assert match.card.page_id == "p1"

    def test_no_match_returns_none_without_ambiguous_candidates(self) -> None:
        pages = [_card_page("p1", "山")]
        client = FakeNotionClient(pages)
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        match = repo.find_match(_deck_card(name_ja="沼"))

        assert match.card is None
        assert not match.is_ambiguous

    def test_multiple_candidates_are_ambiguous(self) -> None:
        pages = [_card_page("p1", "沼"), _card_page("p2", "沼")]
        client = FakeNotionClient(pages)
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        match = repo.find_match(_deck_card(name_ja="沼"))

        assert match.card is None
        assert match.is_ambiguous
        assert len(match.ambiguous_candidates) == 2

    def test_normalization_absorbs_whitespace_and_case_differences(self) -> None:
        pages = [_card_page("p1", "Sol Ring", name_en="Sol Ring")]
        client = FakeNotionClient(pages)
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        match = repo.find_match(_deck_card(name_en="  sol   ring  "))

        assert match.card is not None
        assert match.card.page_id == "p1"

    def test_merged_pages_are_excluded_from_matching(self) -> None:
        # dedupe-cards で統合済み(統合済み=true)としてマークされたページは、
        # 情報が代表ページへ集約済みのため索引・照合から除外されるべき
        # (回帰テスト: この除外がないと、dedupe-cards後もimport-cardsの
        # 曖昧一致が解消されない)。
        pages = [
            _card_page("p1", "太陽の指輪", name_en="Sol Ring", merged=False),
            _card_page("p2", "太陽の指輪", name_en="Sol Ring", merged=True),
            _card_page("p3", "太陽の指輪", name_en="Sol Ring", merged=True),
        ]
        client = FakeNotionClient(pages)
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        match = repo.find_match(_deck_card(name_ja="太陽の指輪", name_en="Sol Ring"))

        assert match.card is not None
        assert match.card.page_id == "p1"
        assert not match.is_ambiguous


class TestGetDeckRelationIds:
    def test_returns_ids_from_page_properties_when_not_truncated(self) -> None:
        page = _card_page("p1", "沼", deck_relation_ids=["deck-1", "deck-2"])
        client = FakeNotionClient([page])
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        existing = repo.find_match(_deck_card(name_ja="沼")).card
        assert existing is not None
        ids = repo.get_deck_relation_ids(existing)

        assert ids == ["deck-1", "deck-2"]
        assert client.property_item_calls == []

    def test_paginates_via_property_endpoint_when_truncated(self) -> None:
        page = _card_page(
            "p1",
            "沼",
            deck_relation_ids=["deck-1"],
            relation_has_more=True,
            relation_property_id="rel-prop-id",
        )
        client = FakeNotionClient([page])
        client.set_property_items(
            "p1",
            "rel-prop-id",
            [
                {"type": "relation", "relation": {"id": "deck-1"}},
                {"type": "relation", "relation": {"id": "deck-2"}},
            ],
        )
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        existing = repo.find_match(_deck_card(name_ja="沼")).card
        assert existing is not None
        ids = repo.get_deck_relation_ids(existing)

        assert ids == ["deck-1", "deck-2"]
        assert client.property_item_calls == [("p1", "rel-prop-id")]


class TestApplyRelationUpdate:
    def test_adds_relation_when_missing(self) -> None:
        page = _card_page("p1", "沼", owned=True, deck_relation_ids=["deck-existing"])
        client = FakeNotionClient([page])
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()
        existing = repo.find_match(_deck_card(name_ja="沼")).card
        assert existing is not None

        repo.apply_relation_update(existing, "deck-new", current_deck_ids=["deck-existing"])

        assert len(client.updated_pages) == 1
        page_id, properties = client.updated_pages[0]
        assert page_id == "p1"
        relation_ids = [r["id"] for r in properties["採用デッキ"]["relation"]]
        assert set(relation_ids) == {"deck-existing", "deck-new"}
        assert "所持" not in properties  # 既に所持済みなので更新しない

    def test_does_not_duplicate_existing_relation(self) -> None:
        page = _card_page("p1", "沼", owned=True, deck_relation_ids=["deck-1"])
        client = FakeNotionClient([page])
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()
        existing = repo.find_match(_deck_card(name_ja="沼")).card
        assert existing is not None

        repo.apply_relation_update(existing, "deck-1", current_deck_ids=["deck-1"])

        # リレーションは既にあるため関連プロパティは送らない。所持も既にtrueなので更新なし。
        assert client.updated_pages == []

    def test_updates_owned_flag_when_false(self) -> None:
        page = _card_page("p1", "沼", owned=False, deck_relation_ids=["deck-1"])
        client = FakeNotionClient([page])
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()
        existing = repo.find_match(_deck_card(name_ja="沼")).card
        assert existing is not None

        repo.apply_relation_update(existing, "deck-1", current_deck_ids=["deck-1"])

        assert len(client.updated_pages) == 1
        _, properties = client.updated_pages[0]
        assert properties["所持"]["checkbox"] is True
        assert "採用デッキ" not in properties  # 既にリレーション済みなので更新しない


class TestCreateCard:
    def test_creates_with_required_fields_only(self) -> None:
        client = FakeNotionClient([])
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        repo.create_card(_deck_card(name_ja="新カード"), "deck-1")

        assert len(client.created_pages) == 1
        data_source_id, properties = client.created_pages[0]
        assert data_source_id == DATA_SOURCE_ID
        assert properties["カード名"]["title"][0]["text"]["content"] == "新カード"
        assert properties["所持"]["checkbox"] is True
        assert properties["採用デッキ"]["relation"] == [{"id": "deck-1"}]
        assert "英語名" not in properties  # 取得できていない場合は設定しない

    def test_creates_with_english_name_and_note_when_available(self) -> None:
        client = FakeNotionClient([])
        repo = CardRepository(client, DATA_SOURCE_ID)
        repo.load()

        repo.create_card(
            _deck_card(name_ja="新カード", name_en="New Card"),
            "deck-1",
            note="吸血鬼の血統プレコン由来",
        )

        _, properties = client.created_pages[0]
        assert properties["英語名"]["rich_text"][0]["text"]["content"] == "New Card"
        assert properties["メモ"]["rich_text"][0]["text"]["content"] == "吸血鬼の血統プレコン由来"
