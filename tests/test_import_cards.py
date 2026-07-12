from __future__ import annotations

import pytest

from mtg_notion_manager.exceptions import AmbiguousCardMatchError
from mtg_notion_manager.models import CardDecision, DeckCard, ExistingCard, ParsedDeckList
from mtg_notion_manager.notion.card_repository import CardMatch
from mtg_notion_manager.services import import_cards

DECK_PAGE_ID = "deck-page-1"
SOURCE_URL = "https://mtg-jp.com/reading/publicity/0035593/"


def _card(name_ja: str = "テストカード", quantity: int = 1, is_commander: bool = False) -> DeckCard:
    return DeckCard(
        name_ja=name_ja,
        name_en=None,
        quantity=quantity,
        is_commander=is_commander,
        source_url=SOURCE_URL,
    )


def _parsed(cards: list[DeckCard]) -> ParsedDeckList:
    return ParsedDeckList(
        deck_name="吸血鬼の血統",
        commander_name="マウアーの太祖、ストレイファン",
        cards=cards,
        source_url=SOURCE_URL,
    )


class FakeCardRepository:
    def __init__(
        self,
        matches: dict[str, CardMatch] | None = None,
        deck_relation_ids: dict[str, list[str]] | None = None,
        owned: dict[str, bool] | None = None,
    ) -> None:
        self.matches = matches or {}
        self.deck_relation_ids = deck_relation_ids or {}
        self.owned = owned or {}
        self.loaded = False
        self.created: list[tuple[DeckCard, str, str]] = []
        self.relation_updates: list[tuple[str, str, list[str]]] = []

    def load(self) -> None:
        self.loaded = True

    def find_match(self, card: DeckCard) -> CardMatch:
        key = card.name_ja or card.name_en or ""
        return self.matches.get(key, CardMatch(card=None, ambiguous_candidates=[]))

    def get_deck_relation_ids(self, existing: ExistingCard) -> list[str]:
        return self.deck_relation_ids.get(existing.page_id, [])

    def is_owned(self, existing: ExistingCard) -> bool:
        return self.owned.get(existing.page_id, False)

    def create_card(self, card: DeckCard, deck_page_id: str, note: str = "") -> dict:
        self.created.append((card, deck_page_id, note))
        return {"id": "new-card-id", "url": "https://notion.so/new-card-id"}

    def apply_relation_update(
        self, existing: ExistingCard, deck_page_id: str, current_deck_ids: list[str]
    ) -> dict:
        self.relation_updates.append((existing.page_id, deck_page_id, current_deck_ids))
        return {"id": existing.page_id, "url": existing.page_url}


def _existing(page_id: str) -> ExistingCard:
    return ExistingCard(page_id=page_id, page_url=f"https://notion.so/{page_id}", properties={})


class TestBuildImportCardsPlan:
    def test_new_card_is_classified_as_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        card = _card("新カード")
        monkeypatch.setattr(
            import_cards, "parse_decklist", lambda url, deck_name, html=None: _parsed([card])
        )
        repo = FakeCardRepository()

        plan = import_cards.build_import_cards_plan(
            SOURCE_URL, DECK_PAGE_ID, repo, allow_count_mismatch=True
        )

        assert repo.loaded is True
        assert len(plan.decisions) == 1
        assert plan.decisions[0].action == "create"

    def test_existing_card_already_related_and_owned_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = _card("既存カード")
        existing = _existing("p1")
        monkeypatch.setattr(
            import_cards, "parse_decklist", lambda url, deck_name, html=None: _parsed([card])
        )
        repo = FakeCardRepository(
            matches={"既存カード": CardMatch(card=existing, ambiguous_candidates=[])},
            deck_relation_ids={"p1": [DECK_PAGE_ID]},
            owned={"p1": True},
        )

        plan = import_cards.build_import_cards_plan(
            SOURCE_URL, DECK_PAGE_ID, repo, allow_count_mismatch=True
        )

        assert plan.decisions[0].action == "unchanged"

    def test_existing_card_not_related_needs_relation_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = _card("既存カード")
        existing = _existing("p1")
        monkeypatch.setattr(
            import_cards, "parse_decklist", lambda url, deck_name, html=None: _parsed([card])
        )
        repo = FakeCardRepository(
            matches={"既存カード": CardMatch(card=existing, ambiguous_candidates=[])},
            deck_relation_ids={"p1": []},
            owned={"p1": True},
        )

        plan = import_cards.build_import_cards_plan(
            SOURCE_URL, DECK_PAGE_ID, repo, allow_count_mismatch=True
        )

        assert plan.decisions[0].action == "relation_update"

    def test_ambiguous_match_is_classified_as_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = _card("曖昧カード")
        candidates = [_existing("p1"), _existing("p2")]
        monkeypatch.setattr(
            import_cards, "parse_decklist", lambda url, deck_name, html=None: _parsed([card])
        )
        repo = FakeCardRepository(
            matches={"曖昧カード": CardMatch(card=None, ambiguous_candidates=candidates)}
        )

        plan = import_cards.build_import_cards_plan(
            SOURCE_URL, DECK_PAGE_ID, repo, allow_count_mismatch=True
        )

        assert plan.decisions[0].action == "ambiguous"
        assert plan.has_blocking_issues is True

    def test_summary_counts_match_decisions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cards = [_card("新カード1"), _card("新カード2")]
        monkeypatch.setattr(
            import_cards, "parse_decklist", lambda url, deck_name, html=None: _parsed(cards)
        )
        repo = FakeCardRepository()

        plan = import_cards.build_import_cards_plan(
            SOURCE_URL, DECK_PAGE_ID, repo, allow_count_mismatch=True
        )

        assert plan.summary == {"create": 2}


class TestExecuteImportCards:
    def test_creates_new_cards(self) -> None:
        card = _card("新カード")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="create")],
        )
        repo = FakeCardRepository()

        result = import_cards.execute_import_cards(plan, repo)

        assert len(repo.created) == 1
        assert result.results[0].action == "created"

    def test_unchanged_cards_do_not_call_notion(self) -> None:
        card = _card("既存カード")
        existing = _existing("p1")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="unchanged", existing=existing)],
        )
        repo = FakeCardRepository()

        result = import_cards.execute_import_cards(plan, repo)

        assert repo.created == []
        assert repo.relation_updates == []
        assert result.results[0].action == "unchanged"

    def test_relation_update_calls_repository_with_fresh_state(self) -> None:
        card = _card("既存カード")
        existing = _existing("p1")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="relation_update", existing=existing)],
        )
        repo = FakeCardRepository(deck_relation_ids={"p1": ["other-deck"]})

        result = import_cards.execute_import_cards(plan, repo)

        assert len(repo.relation_updates) == 1
        page_id, deck_page_id, current_ids = repo.relation_updates[0]
        assert page_id == "p1"
        assert deck_page_id == DECK_PAGE_ID
        assert current_ids == ["other-deck"]
        assert result.results[0].action == "relation_updated"

    def test_blocking_issues_prevent_any_writes(self) -> None:
        ambiguous_card = _card("曖昧カード")
        create_card = _card("新カード")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([ambiguous_card, create_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=ambiguous_card, action="ambiguous", detail="複数候補"),
                CardDecision(card=create_card, action="create"),
            ],
        )
        repo = FakeCardRepository()

        with pytest.raises(AmbiguousCardMatchError):
            import_cards.execute_import_cards(plan, repo)

        assert repo.created == []  # 曖昧一致があるため何も書き込まれない

    def test_rerunning_after_success_is_idempotent(self) -> None:
        """1回目の適用でrelation_updateされたカードは、2回目はunchangedと判定されるべき。"""
        card = _card("既存カード")
        existing = _existing("p1")

        repo = FakeCardRepository(
            matches={"既存カード": CardMatch(card=existing, ambiguous_candidates=[])},
            deck_relation_ids={"p1": []},
            owned={"p1": True},
        )
        # 1回目: リレーション追加が必要と判定される
        decision = import_cards._decide(card, DECK_PAGE_ID, repo)
        assert decision.action == "relation_update"

        # 適用後、リポジトリの状態が更新されたとみなす(実際のNotion側の状態変化を模擬)
        repo.deck_relation_ids["p1"] = [DECK_PAGE_ID]

        # 2回目: 既にリレーション済みなのでunchangedと判定される
        decision_again = import_cards._decide(card, DECK_PAGE_ID, repo)
        assert decision_again.action == "unchanged"
