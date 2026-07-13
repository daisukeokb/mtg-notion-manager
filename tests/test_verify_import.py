from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.models import (
    CardDecision,
    DeckCard,
    ExistingCard,
    ExistingDeck,
    ParsedDeckList,
)
from mtg_notion_manager.notion.card_repository import CardMatch
from mtg_notion_manager.services import import_article, verify_import
from mtg_notion_manager.services import import_cards as import_cards_module

SOURCE_URL = "https://magic.wizards.com/ja/news/announcements/secrets-of-strixhaven-commander-decklists"

DECK_NAMES = ["シルバークイルの威勢", "プリズマリの技巧"]


def _card(name_en: str, quantity: int = 1) -> DeckCard:
    return DeckCard(
        name_ja=None, name_en=name_en, quantity=quantity, is_commander=False, source_url=SOURCE_URL
    )


def _parsed(deck_name: str, cards: list[DeckCard]) -> ParsedDeckList:
    commander = cards[0].display_name if cards else "?"
    return ParsedDeckList(
        deck_name=deck_name, commander_name=commander, cards=cards, source_url=SOURCE_URL
    )


class FakeFetcher:
    def __init__(self, deck_names: list[str]) -> None:
        self.deck_names = deck_names

    def list_deck_names(self, html: str, source_url: str) -> list[str]:
        return self.deck_names


class FakeWriter:
    def __init__(self, existing: dict[str, list[ExistingDeck]] | None = None) -> None:
        self.existing = existing or {}
        self.lookup_calls: list[str] = []

    def find_existing_decks(self, name: str) -> list[ExistingDeck]:
        self.lookup_calls.append(name)
        return list(self.existing.get(name, []))

    def find_existing_deck(self, name: str) -> ExistingDeck | None:
        matches = self.find_existing_decks(name)
        return matches[0] if matches else None


class FakeCardRepository:
    def __init__(
        self,
        matches: dict[str, CardMatch] | None = None,
        deck_relation_ids: dict[str, list[str]] | None = None,
        owned: dict[str, bool] | None = None,
        by_page_id: dict[str, ExistingCard] | None = None,
    ) -> None:
        self.matches = matches or {}
        self.deck_relation_ids = deck_relation_ids or {}
        self.owned = owned or {}
        self.by_page_id = by_page_id or {}
        self._loaded = False
        self.load_call_count = 0

    def load(self) -> None:
        if self._loaded:
            return
        self.load_call_count += 1
        self._loaded = True

    def find_match(self, card: DeckCard) -> CardMatch:
        key = card.name_ja or card.name_en or ""
        return self.matches.get(key, CardMatch(card=None, ambiguous_candidates=[]))

    def get_deck_relation_ids(self, existing: ExistingCard) -> list[str]:
        return self.deck_relation_ids.get(existing.page_id, [])

    def is_owned(self, existing: ExistingCard) -> bool:
        return self.owned.get(existing.page_id, False)

    def get_by_page_id(self, page_id: str) -> ExistingCard | None:
        return self.by_page_id.get(page_id)

    def create_card(self, card: DeckCard, deck_page_id: str, note: str = "") -> dict:
        raise AssertionError("verify-import must never create cards")

    def apply_relation_update(
        self, existing: ExistingCard, deck_page_id: str, current_deck_ids: list[str]
    ) -> dict:
        raise AssertionError("verify-import must never update card relations")


class FakeNotionClient:
    """`NotionClient.read_relation_ids` だけを模した最小限のFake。"""

    def __init__(
        self, relation_ids: dict[str, list[str]] | None = None, fail: bool = False
    ) -> None:
        self.relation_ids = relation_ids or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.get_page_calls: list[str] = []

    def read_relation_ids(self, properties: dict, page_id: str, property_name: str) -> list[str]:
        self.calls.append((page_id, property_name))
        if self.fail:
            raise NotionAPIError("simulated relation read failure")
        return list(self.relation_ids.get(page_id, []))

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls.append(page_id)
        return {"id": page_id, "url": f"https://notion.so/{page_id}", "properties": {}}

    def update_page(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("verify-import must never call update_page")

    def create_page(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("verify-import must never call create_page")


def _existing_deck(page_id: str) -> ExistingDeck:
    return ExistingDeck(page_id=page_id, page_url=f"https://notion.so/{page_id}", properties={})


def _existing_card(
    page_id: str, name_en: str | None = None, name_ja: str | None = None
) -> ExistingCard:
    props: dict = {}
    if name_en is not None:
        props["英語名"] = {"type": "rich_text", "rich_text": [{"plain_text": name_en}]}
    if name_ja is not None:
        props["カード名"] = {"type": "title", "title": [{"plain_text": name_ja}]}
    return ExistingCard(page_id=page_id, page_url=f"https://notion.so/{page_id}", properties=props)


def _patch_common(
    monkeypatch: pytest.MonkeyPatch, deck_names: list[str], parse_map: dict[str, ParsedDeckList]
) -> None:
    monkeypatch.setattr(import_article, "download", lambda url: "<html></html>")
    monkeypatch.setattr(import_article, "get_fetcher", lambda url: FakeFetcher(deck_names))

    def fake_parse_decklist(url: str, deck_name: str | None = None, html: str | None = None):
        if deck_name not in parse_map:
            raise KeyError(deck_name)
        return parse_map[deck_name]

    monkeypatch.setattr(import_cards_module, "parse_decklist", fake_parse_decklist)


def _fully_registered_deck(deck_name: str, prefix: str, count: int = 100) -> dict:
    """100枚すべてが既存カードで、既にrelation済みの「完全に登録済み」なデッキ一式を組み立てる。"""
    cards = [_card(f"{prefix}-{i}") for i in range(count)]
    parsed = _parsed(deck_name, cards)
    deck_page_id = f"deck-{prefix}"

    matches: dict[str, CardMatch] = {}
    by_page_id: dict[str, ExistingCard] = {}
    deck_relation_ids: dict[str, list[str]] = {}
    owned: dict[str, bool] = {}
    page_ids: list[str] = []
    for card in cards:
        page_id = f"card-{card.name_en}"
        page_ids.append(page_id)
        existing = _existing_card(page_id, name_en=card.name_en)
        matches[card.name_en] = CardMatch(card=existing, ambiguous_candidates=[])
        by_page_id[page_id] = existing
        deck_relation_ids[page_id] = [deck_page_id]
        owned[page_id] = True

    return {
        "deck_name": deck_name,
        "parsed": parsed,
        "deck_page_id": deck_page_id,
        "deck": _existing_deck(deck_page_id),
        "matches": matches,
        "by_page_id": by_page_id,
        "deck_relation_ids": deck_relation_ids,
        "owned": owned,
        "page_ids": sorted(page_ids),
    }


def _setup_single_verified_deck(monkeypatch: pytest.MonkeyPatch, deck_name: str = DECK_NAMES[0]):
    setup = _fully_registered_deck(deck_name, "c")
    _patch_common(monkeypatch, [deck_name], {deck_name: setup["parsed"]})
    writer = FakeWriter({deck_name: [setup["deck"]]})
    card_repo = FakeCardRepository(
        matches=setup["matches"],
        deck_relation_ids=setup["deck_relation_ids"],
        owned=setup["owned"],
        by_page_id=setup["by_page_id"],
    )
    client = FakeNotionClient({setup["deck_page_id"]: setup["page_ids"]})
    return setup, writer, card_repo, client


class TestVerifiedSingleDeck:
    def test_full_match_is_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        assert report.verification_status == verify_import.VERIFICATION_VERIFIED
        entry = report.entries[0]
        assert entry.is_verified
        assert entry.extracted_card_count == 100
        assert entry.unique_card_count == 100
        assert entry.new_card_count == 0
        assert entry.ambiguous_match_count == 0
        assert entry.error_count == 0
        assert entry.missing_relation_page_ids == []
        assert entry.unexpected_relation_page_ids == []
        assert entry.verification_errors == []

    def test_relation_page_id_order_does_not_matter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        # actual側を意図的に逆順で返す
        client.relation_ids[setup["deck_page_id"]] = list(reversed(setup["page_ids"]))

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        assert report.entries[0].is_verified

    def test_deterministic_output_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert entry.expected_relation_page_ids == sorted(entry.expected_relation_page_ids)
        assert entry.actual_relation_page_ids == sorted(entry.actual_relation_page_ids)


class TestVerifiedMultipleDecks:
    def test_all_decks_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup1 = _fully_registered_deck(DECK_NAMES[0], "a")
        setup2 = _fully_registered_deck(DECK_NAMES[1], "b")
        _patch_common(
            monkeypatch,
            DECK_NAMES,
            {DECK_NAMES[0]: setup1["parsed"], DECK_NAMES[1]: setup2["parsed"]},
        )
        writer = FakeWriter(
            {DECK_NAMES[0]: [setup1["deck"]], DECK_NAMES[1]: [setup2["deck"]]}
        )
        card_repo = FakeCardRepository(
            matches={**setup1["matches"], **setup2["matches"]},
            deck_relation_ids={**setup1["deck_relation_ids"], **setup2["deck_relation_ids"]},
            owned={**setup1["owned"], **setup2["owned"]},
            by_page_id={**setup1["by_page_id"], **setup2["by_page_id"]},
        )
        client = FakeNotionClient(
            {
                setup1["deck_page_id"]: setup1["page_ids"],
                setup2["deck_page_id"]: setup2["page_ids"],
            }
        )

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        assert report.verification_status == verify_import.VERIFICATION_VERIFIED
        assert len(report.entries) == 2
        assert all(e.is_verified for e in report.entries)

    def test_include_deck_limits_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup1 = _fully_registered_deck(DECK_NAMES[0], "a")
        setup2 = _fully_registered_deck(DECK_NAMES[1], "b")
        _patch_common(
            monkeypatch,
            DECK_NAMES,
            {DECK_NAMES[0]: setup1["parsed"], DECK_NAMES[1]: setup2["parsed"]},
        )
        writer = FakeWriter(
            {DECK_NAMES[0]: [setup1["deck"]], DECK_NAMES[1]: [setup2["deck"]]}
        )
        card_repo = FakeCardRepository(
            matches={**setup1["matches"], **setup2["matches"]},
            deck_relation_ids={**setup1["deck_relation_ids"], **setup2["deck_relation_ids"]},
            owned={**setup1["owned"], **setup2["owned"]},
            by_page_id={**setup1["by_page_id"], **setup2["by_page_id"]},
        )
        client = FakeNotionClient(
            {
                setup1["deck_page_id"]: setup1["page_ids"],
                setup2["deck_page_id"]: setup2["page_ids"],
            }
        )

        report = verify_import.build_verify_import_plan(
            SOURCE_URL, client, writer, card_repo, include_deck_names=[DECK_NAMES[1]]
        )

        assert report.all_deck_names == DECK_NAMES
        assert len(report.entries) == 1
        assert report.entries[0].deck_name == DECK_NAMES[1]


class TestOverridesUsed:
    def test_override_used_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c")
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter({DECK_NAMES[0]: [setup["deck"]]})

        # 1件だけオーバーライドありに差し替える
        first_key = next(iter(setup["matches"]))
        original = setup["matches"][first_key]
        setup["matches"][first_key] = CardMatch(
            card=original.card, ambiguous_candidates=[], override_reason="テスト理由"
        )
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({setup["deck_page_id"]: setup["page_ids"]})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert entry.is_verified
        assert entry.overrides_used == [{"card": first_key, "reason": "テスト理由"}]


class TestMismatchScenarios:
    def test_wrong_extracted_count_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c", count=99)
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter({DECK_NAMES[0]: [setup["deck"]]})
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({setup["deck_page_id"]: setup["page_ids"]})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert entry.extracted_card_count == 99
        assert any("抽出枚数" in e for e in entry.verification_errors)
        assert report.verification_status == verify_import.VERIFICATION_MISMATCH

    def test_new_card_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c", count=99)
        new_card = _card("brand-new-card")
        setup["parsed"] = _parsed(DECK_NAMES[0], setup["parsed"].cards + [new_card])
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter({DECK_NAMES[0]: [setup["deck"]]})
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({setup["deck_page_id"]: setup["page_ids"]})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert entry.new_card_count == 1
        assert any("新規カード" in e for e in entry.verification_errors)

    def test_ambiguous_match_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c", count=99)
        ambiguous_card = _card("ambiguous-card")
        setup["parsed"] = _parsed(DECK_NAMES[0], setup["parsed"].cards + [ambiguous_card])
        setup["matches"]["ambiguous-card"] = CardMatch(
            card=None,
            ambiguous_candidates=[
                _existing_card("cand-1", name_en="ambiguous-card"),
                _existing_card("cand-2", name_en="ambiguous-card"),
            ],
        )
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter({DECK_NAMES[0]: [setup["deck"]]})
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({setup["deck_page_id"]: setup["page_ids"]})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert entry.ambiguous_match_count == 1
        assert any("曖昧一致" in e for e in entry.verification_errors)

    def test_zero_deck_records_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c")
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter({})  # デッキレコードなし
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert entry.deck_page_id is None
        assert any("見つかりません" in e for e in entry.verification_errors)
        assert client.calls == []  # デッキが特定できないためrelation読み取りは発生しない

    def test_multiple_deck_records_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c")
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter(
            {DECK_NAMES[0]: [setup["deck"], _existing_deck("deck-duplicate")]}
        )
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert any("2件あり一意に特定できません" in e for e in entry.verification_errors)
        assert client.calls == []

    def test_missing_relation_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        # actual relationから1件だけ抜く(=関連付けが漏れている状態)
        missing_id = setup["page_ids"][0]
        client.relation_ids[setup["deck_page_id"]] = [
            pid for pid in setup["page_ids"] if pid != missing_id
        ]

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert entry.missing_relation_page_ids == [missing_id]
        assert entry.unexpected_relation_page_ids == []
        assert entry.missing_relation_cards[0]["page_id"] == missing_id
        assert entry.missing_relation_cards[0]["name_en"] is not None

    def test_unexpected_relation_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        # actual relationに想定外のページを1件追加する
        client.relation_ids[setup["deck_page_id"]] = [*setup["page_ids"], "unexpected-page"]

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert not entry.is_verified
        assert entry.unexpected_relation_page_ids == ["unexpected-page"]
        assert entry.missing_relation_page_ids == []
        # 索引にないpage_idは名前がnullのまま返る(推測しない)
        assert entry.unexpected_relation_cards[0] == {
            "page_id": "unexpected-page",
            "name_ja": None,
            "name_en": None,
        }

    def test_same_count_different_set_is_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """件数は一致するが集合が食い違う場合、件数だけでは検出できないことを確認する。"""
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        swapped = [*setup["page_ids"][1:], "unexpected-page"]  # 1件抜けて1件混入(件数は同じ)
        client.relation_ids[setup["deck_page_id"]] = swapped

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        entry = report.entries[0]
        assert len(entry.actual_relation_page_ids) == len(entry.expected_relation_page_ids)
        assert not entry.is_verified
        assert entry.missing_relation_page_ids == [setup["page_ids"][0]]
        assert entry.unexpected_relation_page_ids == ["unexpected-page"]

    def test_one_deck_failure_makes_article_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup1 = _fully_registered_deck(DECK_NAMES[0], "a")
        setup2 = _fully_registered_deck(DECK_NAMES[1], "b")
        _patch_common(
            monkeypatch,
            DECK_NAMES,
            {DECK_NAMES[0]: setup1["parsed"], DECK_NAMES[1]: setup2["parsed"]},
        )
        writer = FakeWriter({DECK_NAMES[0]: [setup1["deck"]]})  # setup2のデッキは未登録
        card_repo = FakeCardRepository(
            matches={**setup1["matches"], **setup2["matches"]},
            deck_relation_ids={**setup1["deck_relation_ids"], **setup2["deck_relation_ids"]},
            owned={**setup1["owned"], **setup2["owned"]},
            by_page_id={**setup1["by_page_id"], **setup2["by_page_id"]},
        )
        client = FakeNotionClient({setup1["deck_page_id"]: setup1["page_ids"]})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        assert report.verification_status == verify_import.VERIFICATION_MISMATCH
        statuses = {e.deck_name: e.is_verified for e in report.entries}
        assert statuses[DECK_NAMES[0]] is True
        assert statuses[DECK_NAMES[1]] is False


class TestErrorCountFromDecisions:
    """CardDecision.action == "error" のケースを直接構成して集計ロジックを検証する
    (現行の_decide()はerrorを生成しないため、_verify_one_deck単体で検証する)。
    """

    def test_error_decision_is_counted_and_causes_mismatch(self) -> None:
        card = _card("broken-card")
        decision = CardDecision(card=card, action="error", detail="想定外のエラー")
        cards_plan = import_cards_module.ImportCardsPlan(
            parsed=_parsed(DECK_NAMES[0], [card]), deck_page_id="deck-x", decisions=[decision]
        )
        entry = import_article.DeckArticleEntry(
            deck_name=DECK_NAMES[0],
            status=import_article.STATUS_READY,
            deck_page_id="deck-x",
            deck_page_url="https://notion.so/deck-x",
            cards_plan=cards_plan,
            resolution_method="exact_name_match",
        )
        card_repo = FakeCardRepository()
        client = FakeNotionClient({"deck-x": []})

        result = verify_import._verify_one_deck(entry, client, card_repo)

        assert result.error_count == 1
        assert not result.is_verified
        assert any("照合エラー" in e for e in result.verification_errors)


class TestReadOnlyGuarantee:
    def test_no_write_methods_are_called_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        assert report.entries[0].is_verified  # create_card/apply_relation_updateが
        # 呼ばれていれば FakeCardRepository が AssertionError を送出しテスト自体が失敗する

    def test_no_write_methods_are_called_on_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = _fully_registered_deck(DECK_NAMES[0], "c", count=99)  # 抽出枚数不正
        _patch_common(monkeypatch, [DECK_NAMES[0]], {DECK_NAMES[0]: setup["parsed"]})
        writer = FakeWriter({DECK_NAMES[0]: [setup["deck"]]})
        card_repo = FakeCardRepository(
            matches=setup["matches"],
            deck_relation_ids=setup["deck_relation_ids"],
            owned=setup["owned"],
            by_page_id=setup["by_page_id"],
        )
        client = FakeNotionClient({setup["deck_page_id"]: setup["page_ids"]})

        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        assert not report.entries[0].is_verified  # 書き込みメソッドは一切呼ばれない

    def test_verify_import_module_does_not_import_execute_functions(self) -> None:
        """apply系サービスへ到達できないことをモジュールレベルでも確認する。"""
        assert not hasattr(verify_import, "execute_import_cards")
        assert not hasattr(verify_import, "execute_article_import")


class TestExecutionErrors:
    def test_notion_relation_read_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        client.fail = True

        with pytest.raises(NotionAPIError):
            verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

    def test_article_fetch_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_download(url: str) -> str:
            raise NotionAPIError("fetch failed")  # ダウンロード層の例外を模す

        monkeypatch.setattr(import_article, "download", fake_download)
        writer = FakeWriter({})
        card_repo = FakeCardRepository()
        client = FakeNotionClient({})

        with pytest.raises(NotionAPIError):
            verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)


class TestWriteVerifyReport:
    def test_writes_json_with_required_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        paths = verify_import.write_verify_report(
            report, output_dir=tmp_path, timestamp="20260101-000000"
        )

        assert paths.json_path.exists()
        data = json.loads(paths.json_path.read_text(encoding="utf-8"))

        assert data["schema_version"] == 1
        assert data["command"] == "verify-import"
        assert data["article_url"] == SOURCE_URL
        assert "generated_at" in data
        assert data["detected_deck_count"] == 1
        assert data["selected_deck_count"] == 1
        assert data["verification_status"] == "verified"
        assert data["summary"]["verified_deck_count"] == 1
        assert data["summary"]["mismatch_deck_count"] == 0

        deck = data["decks"][0]
        for key in (
            "deck_name",
            "extracted_card_count",
            "unique_card_count",
            "existing_card_count",
            "new_card_count",
            "ambiguous_match_count",
            "error_count",
            "overrides_used",
            "expected_relation_page_ids",
            "actual_relation_page_ids",
            "missing_relation_page_ids",
            "unexpected_relation_page_ids",
            "verification_status",
            "verification_errors",
        ):
            assert key in deck

    def test_no_secrets_in_report(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        setup, writer, card_repo, client = _setup_single_verified_deck(monkeypatch)
        report = verify_import.build_verify_import_plan(SOURCE_URL, client, writer, card_repo)

        paths = verify_import.write_verify_report(
            report, output_dir=tmp_path, timestamp="20260101-000000"
        )
        content = paths.json_path.read_text(encoding="utf-8").lower()
        assert "api_key" not in content
        assert "authorization" not in content
        assert "bearer" not in content
