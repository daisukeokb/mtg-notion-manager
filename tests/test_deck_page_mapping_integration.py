"""明示的デッキページマッピングの結合テスト(import-article/verify-import横断)。

ローウィンの昏明で実際に発生した症状(記事側deck-titleが英語、Notion側が日本語)を
最小fixtureで再現し、以下を確認する:
- --deck-page-map指定でimport-articleが両デッキを処理可能と判定できること
- 各デッキが期待どおり100枚・曖昧一致0・未解決0で解析されること
- import-articleとverify-importが同一のページ解決結果を使うこと(resolver共通化)
- verify-importが「デッキレコードが見つからない」失敗をもう起こさないこと
- 実ネットワーク・実Notionへは一切アクセスしない
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.models import DeckCard, ExistingCard, ExistingDeck, ParsedDeckList
from mtg_notion_manager.notion.card_repository import CardMatch
from mtg_notion_manager.services import import_article, verify_import
from mtg_notion_manager.services import import_cards as import_cards_module
from mtg_notion_manager.services.deck_page_mapping import RESOLUTION_EXPLICIT_MAPPING

ARTICLE_URL = "https://magic.wizards.com/ja/news/announcements/lorwyn-eclipsed-commander-decklists"
COMMANDER_DS_ID = "39aa97c8-7142-80a1-85c2-000b7f998d48"

DECK_A_EN = "Dance of the Elements"
DECK_A_JA = "エレメンタルの舞踊"
DECK_A_PAGE_ID = "39aa97c8-7142-813d-9e6c-e3b7b2bb8873"

DECK_B_EN = "Blight Curse"
DECK_B_JA = "枯朽の呪い"
DECK_B_PAGE_ID = "39aa97c8-7142-81a9-8444-e3ab7a9b7a63"


def _card(name_en: str, quantity: int = 1) -> DeckCard:
    return DeckCard(
        name_ja=None,
        name_en=name_en,
        quantity=quantity,
        is_commander=False,
        source_url=ARTICLE_URL,
    )


def _parsed(deck_name: str, cards: list[DeckCard]) -> ParsedDeckList:
    return ParsedDeckList(
        deck_name=deck_name,
        commander_name=cards[0].display_name,
        cards=cards,
        source_url=ARTICLE_URL,
    )


class FakeFetcher:
    def __init__(self, deck_names: list[str]) -> None:
        self.deck_names = deck_names

    def list_deck_names(self, html: str, source_url: str) -> list[str]:
        return self.deck_names


class FakeWriter:
    """名前完全一致では何も見つからない(記事側=英語、Notion側=日本語)状態を模す。"""

    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.data_source_id = COMMANDER_DS_ID
        self.get_page_calls: list[str] = []

    def find_existing_decks(self, name: str) -> list[ExistingDeck]:
        return []  # 英語名では常に見つからない(意図的な再現)

    def find_existing_deck(self, name: str) -> ExistingDeck | None:
        return None

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls.append(page_id)
        if page_id not in self.pages:
            raise NotionAPIError(f"page not found: {page_id}")
        return self.pages[page_id]


class FakeCardRepository:
    def __init__(self, matches: dict[str, CardMatch]) -> None:
        self.matches = matches
        self._loaded = False
        self.load_call_count = 0

    def load(self) -> None:
        if self._loaded:
            return
        self.load_call_count += 1
        self._loaded = True

    def find_match(self, card: DeckCard) -> CardMatch:
        key = card.name_en or ""
        return self.matches.get(key, CardMatch(card=None, ambiguous_candidates=[]))

    def get_deck_relation_ids(self, existing: ExistingCard) -> list[str]:
        return []

    def is_owned(self, existing: ExistingCard) -> bool:
        return False

    def get_by_page_id(self, page_id: str) -> ExistingCard | None:
        return None

    def create_card(self, card: DeckCard, deck_page_id: str, note: str = "") -> dict:
        raise AssertionError("integration test must never write to Notion")

    def apply_relation_update(self, existing, deck_page_id, current_deck_ids):
        raise AssertionError("integration test must never write to Notion")


class FakeNotionClient:
    def __init__(
        self, relation_ids: dict[str, list[str]] | None = None, pages: dict[str, dict] | None = None
    ) -> None:
        self.relation_ids = relation_ids or {}
        self.pages = pages or {}

    def read_relation_ids(self, properties: dict, page_id: str, property_name: str) -> list[str]:
        return list(self.relation_ids.get(page_id, []))

    def get_page(self, page_id: str) -> dict:
        if page_id in self.pages:
            return self.pages[page_id]
        return {"id": page_id, "url": f"https://notion.so/{page_id}", "properties": {}}

    def update_page(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("integration test must never write to Notion")

    def create_page(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("integration test must never write to Notion")


def _deck_page(page_id: str, name: str) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "parent": {"type": "data_source_id", "data_source_id": COMMANDER_DS_ID},
        "properties": {"名前": {"type": "title", "title": [{"plain_text": name}]}},
    }


def _write_mapping(tmp_path: Path) -> Path:
    data = {
        "schema_version": 1,
        "article_url": ARTICLE_URL,
        "decks": [
            {
                "article_deck_name": DECK_A_EN,
                "page_id": DECK_A_PAGE_ID,
                "expected_page_name": DECK_A_JA,
            },
            {
                "article_deck_name": DECK_B_EN,
                "page_id": DECK_B_PAGE_ID,
                "expected_page_name": DECK_B_JA,
            },
        ],
    }
    path = tmp_path / "deck_page_mapping.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _make_matches(prefix: str, count: int) -> dict[str, CardMatch]:
    """全カードを新規(create)として扱うmatches(曖昧一致0・未解決0を維持するため)。"""
    return {
        f"{prefix}-card-{i}": CardMatch(card=None, ambiguous_candidates=[]) for i in range(count)
    }


def _patch_common(monkeypatch: pytest.MonkeyPatch, parse_map: dict[str, ParsedDeckList]) -> None:
    monkeypatch.setattr(import_article, "download", lambda url: "<html></html>")
    monkeypatch.setattr(
        import_article, "get_fetcher", lambda url: FakeFetcher([DECK_A_EN, DECK_B_EN])
    )

    def fake_parse_decklist(url: str, deck_name: str | None = None, html: str | None = None):
        return parse_map[deck_name]

    monkeypatch.setattr(import_cards_module, "parse_decklist", fake_parse_decklist)


class TestLorwynEclipsedScenario:
    def test_import_article_resolves_both_decks_via_mapping(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cards_a = [_card(f"a-card-{i}") for i in range(100)]
        cards_b = [_card(f"b-card-{i}") for i in range(100)]
        parse_map = {DECK_A_EN: _parsed(DECK_A_EN, cards_a), DECK_B_EN: _parsed(DECK_B_EN, cards_b)}
        _patch_common(monkeypatch, parse_map)

        writer = FakeWriter({DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_JA),
                              DECK_B_PAGE_ID: _deck_page(DECK_B_PAGE_ID, DECK_B_JA)})
        matches = {**_make_matches("a", 100), **_make_matches("b", 100)}
        card_repo = FakeCardRepository(matches)
        mapping_path = _write_mapping(tmp_path)

        plan = import_article.build_article_import_plan(
            ARTICLE_URL, writer, card_repo, deck_page_map_path=mapping_path
        )

        assert len(plan.entries) == 2
        entry_a = next(e for e in plan.entries if e.deck_name == DECK_A_EN)
        entry_b = next(e for e in plan.entries if e.deck_name == DECK_B_EN)

        assert entry_a.status == import_article.STATUS_READY
        assert entry_a.deck_page_id == DECK_A_PAGE_ID
        assert entry_a.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert entry_a.cards_plan is not None
        assert entry_a.cards_plan.parsed.total_quantity == 100
        assert entry_a.cards_plan.summary.get("ambiguous", 0) == 0
        assert entry_a.cards_plan.summary.get("error", 0) == 0

        assert entry_b.status == import_article.STATUS_READY
        assert entry_b.deck_page_id == DECK_B_PAGE_ID
        assert entry_b.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert entry_b.cards_plan is not None
        assert entry_b.cards_plan.parsed.total_quantity == 100

    def test_import_and_verify_agree_on_same_page_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cards_a = [_card(f"a-card-{i}") for i in range(100)]
        cards_b = [_card(f"b-card-{i}") for i in range(100)]
        parse_map = {DECK_A_EN: _parsed(DECK_A_EN, cards_a), DECK_B_EN: _parsed(DECK_B_EN, cards_b)}
        _patch_common(monkeypatch, parse_map)

        writer = FakeWriter({DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_JA),
                              DECK_B_PAGE_ID: _deck_page(DECK_B_PAGE_ID, DECK_B_JA)})
        matches = {**_make_matches("a", 100), **_make_matches("b", 100)}
        card_repo = FakeCardRepository(matches)
        client = FakeNotionClient(relation_ids={})  # 既存relationは0件(未登録デッキを再現)
        mapping_path = _write_mapping(tmp_path)

        report = verify_import.build_verify_import_plan(
            ARTICLE_URL, client, writer, card_repo, deck_page_map_path=mapping_path
        )

        assert len(report.entries) == 2
        entry_a = next(e for e in report.entries if e.deck_name == DECK_A_EN)
        entry_b = next(e for e in report.entries if e.deck_name == DECK_B_EN)

        # 「デッキレコードが見つからない」失敗はもう起きない
        assert entry_a.deck_page_id == DECK_A_PAGE_ID
        assert entry_a.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert not any("見つかりません" in e for e in entry_a.verification_errors)
        assert entry_b.deck_page_id == DECK_B_PAGE_ID
        assert entry_b.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert not any("見つかりません" in e for e in entry_b.verification_errors)

        # 全カードが未登録(new_card_count=100)なので、relationの不足以前に
        # 「新規カードあり」でmismatch判定になる(実書き込みは一切行わない)。
        assert entry_a.ambiguous_match_count == 0
        assert entry_a.error_count == 0
        assert entry_a.new_card_count == 100
        assert not entry_a.is_verified
        assert entry_a.unexpected_relation_page_ids == []

    def test_no_network_or_notion_write_calls_occur(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FakeCardRepository/FakeNotionClientのcreate/update系はAssertionErrorを送出する
        よう設計しているため、このテストが例外なく完了すること自体が
        書き込みゼロであることの証明になる。
        """
        cards_a = [_card(f"a-card-{i}") for i in range(100)]
        cards_b = [_card(f"b-card-{i}") for i in range(100)]
        parse_map = {DECK_A_EN: _parsed(DECK_A_EN, cards_a), DECK_B_EN: _parsed(DECK_B_EN, cards_b)}
        _patch_common(monkeypatch, parse_map)

        writer = FakeWriter({DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_JA),
                              DECK_B_PAGE_ID: _deck_page(DECK_B_PAGE_ID, DECK_B_JA)})
        matches = {**_make_matches("a", 100), **_make_matches("b", 100)}
        card_repo = FakeCardRepository(matches)
        client = FakeNotionClient()
        mapping_path = _write_mapping(tmp_path)

        import_article.build_article_import_plan(
            ARTICLE_URL, writer, card_repo, deck_page_map_path=mapping_path
        )
        verify_import.build_verify_import_plan(
            ARTICLE_URL, client, writer, card_repo, deck_page_map_path=mapping_path
        )
        # ここまで例外なく到達すれば、create_card/apply_relation_update/
        # update_page/create_page は一度も呼ばれていない
