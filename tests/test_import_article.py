from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.models import DeckCard, ExistingCard, ExistingDeck, ParsedDeckList
from mtg_notion_manager.notion.card_repository import CardMatch
from mtg_notion_manager.services import import_article
from mtg_notion_manager.services import import_cards as import_cards_module

SOURCE_URL = "https://magic.wizards.com/ja/news/announcements/secrets-of-strixhaven-commander-decklists"

DECK_NAMES = [
    "シルバークイルの威勢",
    "プリズマリの技巧",
    "ウィザーブルームの悪疫",
    "ロアホールドの魂",
    "クアンドリクスは留まり知らず",
]


def _card(name_en: str, quantity: int = 1) -> DeckCard:
    # name_jaも設定する: このテストファイルはデッキ単位のオーケストレーション
    # (曖昧一致・複数デッキ独立処理・冪等性等)を検証する目的であり、新規カード
    # provenance検証(name_ja未確認時のブロック)は tests/test_card_resolution.py と
    # test_deck_page_mapping_integration.py が専門にカバーする。ここでは
    # 「name_ja確認済みの新規カード」という現実的なデータを使い、
    # create/relation_update等の既存アサーションが安全機構の影響を受けないようにする。
    return DeckCard(
        name_ja=name_en,
        name_en=name_en,
        quantity=quantity,
        is_commander=False,
        source_url=SOURCE_URL,
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
    def __init__(
        self,
        existing: dict[str, ExistingDeck | list[ExistingDeck]] | None = None,
        data_source_id: str = "commander-ds-id",
        pages: dict[str, dict] | None = None,
    ) -> None:
        self.existing = existing or {}
        self.lookup_calls: list[str] = []
        self.data_source_id = data_source_id
        self.pages = pages or {}
        self.get_page_calls: list[str] = []

    def find_existing_decks(self, name: str) -> list[ExistingDeck]:
        self.lookup_calls.append(name)
        value = self.existing.get(name)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def find_existing_deck(self, name: str) -> ExistingDeck | None:
        matches = self.find_existing_decks(name)
        return matches[0] if matches else None

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls.append(page_id)
        if page_id not in self.pages:
            raise NotionAPIError(f"page not found: {page_id}")
        return self.pages[page_id]


class FakeCardRepository:
    def __init__(
        self,
        matches: dict[str, CardMatch] | None = None,
        deck_relation_ids: dict[str, list[str]] | None = None,
        owned: dict[str, bool] | None = None,
        fail_on_create: set[str] | None = None,
    ) -> None:
        self.matches = matches or {}
        self.deck_relation_ids = deck_relation_ids or {}
        self.owned = owned or {}
        self.fail_on_create = fail_on_create or set()
        self.load_call_count = 0
        self._loaded = False
        self.created: list[tuple[DeckCard, str, str]] = []
        self.relation_updates: list[tuple[str, str, list[str]]] = []

    def load(self) -> None:
        """本物のCardRepository.load()と同じく、初回のみ実際に取得する(冪等)。"""
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

    def create_card(self, card, deck_page_id: str, note: str = "") -> dict:
        if card.name_ja in self.fail_on_create:
            raise NotionAPIError("simulated failure")
        self.created.append((card, deck_page_id, note))
        return {"id": f"new-{card.name_ja}", "url": f"https://notion.so/new-{card.name_ja}"}

    def apply_relation_update(
        self, existing: ExistingCard, deck_page_id: str, current_deck_ids: list[str]
    ) -> dict:
        self.relation_updates.append((existing.page_id, deck_page_id, current_deck_ids))
        return {"id": existing.page_id, "url": existing.page_url}


def _existing_deck(name: str, page_id: str) -> ExistingDeck:
    return ExistingDeck(page_id=page_id, page_url=f"https://notion.so/{page_id}", properties={})


def _all_existing_decks(names: list[str]) -> dict[str, ExistingDeck]:
    return {name: _existing_deck(name, f"deck-{i}") for i, name in enumerate(names)}


def _existing_card(page_id: str) -> ExistingCard:
    return ExistingCard(page_id=page_id, page_url=f"https://notion.so/{page_id}", properties={})


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


class TestBuildArticleImportPlanFiveDecks:
    def test_all_five_decks_are_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parse_map = {
            name: _parsed(name, [_card(f"{name}-card1", 100)]) for name in DECK_NAMES
        }
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES))
        card_repo = FakeCardRepository()

        plan = import_article.build_article_import_plan(SOURCE_URL, writer, card_repo)

        assert plan.all_deck_names == DECK_NAMES
        assert len(plan.entries) == 5
        assert plan.counts[import_article.STATUS_READY] == 5
        assert plan.counts[import_article.STATUS_NEEDS_REVIEW] == 0
        assert plan.counts[import_article.STATUS_ERROR] == 0

    def test_all_deck_names_are_extracted_even_when_some_are_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parse_map = {
            name: _parsed(name, [_card(f"{name}-card1", 100)]) for name in DECK_NAMES
        }
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES))
        card_repo = FakeCardRepository()

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, exclude_deck_names=[DECK_NAMES[0]]
        )

        assert plan.all_deck_names == DECK_NAMES  # 記事内の全デッキ名は常に保持する
        assert plan.excluded_deck_names == [DECK_NAMES[0]]
        assert len(plan.entries) == 4
        assert all(e.deck_name != DECK_NAMES[0] for e in plan.entries)


class TestPerDeckCountValidation:
    def test_deck_with_wrong_total_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parse_map = {
            DECK_NAMES[0]: _parsed(DECK_NAMES[0], [_card("card1", 99)]),  # 99枚(不足)
            **{name: _parsed(name, [_card(f"{name}-c", 100)]) for name in DECK_NAMES[1:]},
        }
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES))
        card_repo = FakeCardRepository()

        plan = import_article.build_article_import_plan(SOURCE_URL, writer, card_repo)

        bad_entry = next(e for e in plan.entries if e.deck_name == DECK_NAMES[0])
        assert bad_entry.status == import_article.STATUS_ERROR
        assert isinstance(bad_entry.cards_plan, type(None))


class TestOneDeckFailureIsolation:
    def test_ambiguous_deck_does_not_block_other_decks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ambiguous_card = _card("曖昧カード")
        parse_map = {
            DECK_NAMES[0]: _parsed(DECK_NAMES[0], [ambiguous_card]),
            **{name: _parsed(name, [_card(f"{name}-c", 1)]) for name in DECK_NAMES[1:]},
        }
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES))
        candidates = [_existing_card("p1"), _existing_card("p2")]
        card_repo = FakeCardRepository(
            matches={"曖昧カード": CardMatch(card=None, ambiguous_candidates=candidates)}
        )

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )

        entries_by_name = {e.deck_name: e for e in plan.entries}
        assert entries_by_name[DECK_NAMES[0]].status == import_article.STATUS_NEEDS_REVIEW
        for name in DECK_NAMES[1:]:
            assert entries_by_name[name].status == import_article.STATUS_READY

    def test_deck_not_found_in_commander_db_is_needs_review_and_not_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parse_map = {name: _parsed(name, [_card(f"{name}-c", 1)]) for name in DECK_NAMES}
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        # 1件だけ統率者DBに存在しない状態を模擬
        existing = _all_existing_decks(DECK_NAMES[1:])
        writer = FakeWriter(existing)
        card_repo = FakeCardRepository()

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )

        missing_entry = next(e for e in plan.entries if e.deck_name == DECK_NAMES[0])
        assert missing_entry.status == import_article.STATUS_NEEDS_REVIEW
        assert missing_entry.deck_page_id is None
        assert missing_entry.cards_plan is None


class TestDryRunDoesNotWrite:
    def test_build_plan_never_calls_create_or_relation_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parse_map = {name: _parsed(name, [_card(f"{name}-c", 1)]) for name in DECK_NAMES}
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES))
        card_repo = FakeCardRepository()

        import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )

        assert card_repo.created == []
        assert card_repo.relation_updates == []


class TestCardDbFetchedOnce:
    def test_load_called_exactly_once_for_whole_article(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parse_map = {name: _parsed(name, [_card(f"{name}-c", 1)]) for name in DECK_NAMES}
        _patch_common(monkeypatch, DECK_NAMES, parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES))
        card_repo = FakeCardRepository()

        import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )

        assert card_repo.load_call_count == 1


class TestIdempotency:
    def test_rerun_after_apply_reports_unchanged_and_no_extra_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        card = _card("既存カード")
        parse_map = {DECK_NAMES[0]: _parsed(DECK_NAMES[0], [card])}
        _patch_common(monkeypatch, [DECK_NAMES[0]], parse_map)
        writer = FakeWriter({DECK_NAMES[0]: _existing_deck(DECK_NAMES[0], "deck-0")})
        existing = _existing_card("p1")
        card_repo = FakeCardRepository(
            matches={"既存カード": CardMatch(card=existing, ambiguous_candidates=[])},
            deck_relation_ids={"p1": []},
            owned={"p1": True},
        )

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        applied_plan = import_article.execute_article_import(plan, card_repo)
        assert len(card_repo.relation_updates) == 1

        # Notion側の状態変化を模擬(実際にはapply_relation_updateの結果を反映)
        card_repo.deck_relation_ids["p1"] = ["deck-0"]

        plan2 = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        applied_plan2 = import_article.execute_article_import(plan2, card_repo)

        entry2 = applied_plan2.entries[0]
        assert entry2.cards_plan is not None
        assert entry2.cards_plan.summary == {"unchanged": 1}
        assert len(card_repo.relation_updates) == 1  # 追加の書き込みなし
        assert applied_plan.entries[0].status == import_article.STATUS_READY


class TestExecuteArticleImport:
    def test_needs_review_deck_blocks_writes_for_entire_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全体preflightゲート: 対象範囲に1件でもneeds_reviewのデッキがあれば、
        他の安全なデッキも含めて今回の実行では一切書き込みを行わない
        (デッキ単位でplanとwriteを交互に行わないための安全不変条件)。
        """
        ambiguous_card = _card("曖昧カード")
        parse_map = {
            DECK_NAMES[0]: _parsed(DECK_NAMES[0], [ambiguous_card]),
            DECK_NAMES[1]: _parsed(DECK_NAMES[1], [_card("新カード")]),
        }
        _patch_common(monkeypatch, DECK_NAMES[:2], parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES[:2]))
        candidates = [_existing_card("p1"), _existing_card("p2")]
        card_repo = FakeCardRepository(
            matches={"曖昧カード": CardMatch(card=None, ambiguous_candidates=candidates)}
        )

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        assert plan.is_fully_applicable is False
        applied = import_article.execute_article_import(plan, card_repo)

        entries_by_name = {e.deck_name: e for e in applied.entries}
        assert entries_by_name[DECK_NAMES[0]].apply_result is None
        assert entries_by_name[DECK_NAMES[1]].apply_result is None  # 安全なデッキも書き込まれない
        assert len(card_repo.created) == 0

    def test_unconfirmed_japanese_name_in_one_deck_blocks_relation_updates_in_other_deck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """デッキA(既存カードのrelation_updateのみ)が完全に安全でも、デッキBに
        日本語名未確認の新規カードが1件でもあれば、対象範囲全体で一切書き込まない
        (作成予定0・relation追加予定0のいずれも実行されないことを確認する)。
        """
        unconfirmed_card = DeckCard(
            name_ja=None,
            name_en="Unconfirmed English Card",
            quantity=1,
            is_commander=False,
            source_url=SOURCE_URL,
        )
        existing_card = _card("既存カード")
        parse_map = {
            DECK_NAMES[0]: _parsed(DECK_NAMES[0], [existing_card]),
            DECK_NAMES[1]: _parsed(DECK_NAMES[1], [unconfirmed_card]),
        }
        _patch_common(monkeypatch, DECK_NAMES[:2], parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES[:2]))
        existing = _existing_card("p1")
        card_repo = FakeCardRepository(
            matches={"既存カード": CardMatch(card=existing, ambiguous_candidates=[])},
            deck_relation_ids={"p1": []},
            owned={"p1": True},
        )

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        entries_by_name = {e.deck_name: e for e in plan.entries}
        assert entries_by_name[DECK_NAMES[0]].status == import_article.STATUS_READY
        assert entries_by_name[DECK_NAMES[1]].status == import_article.STATUS_NEEDS_REVIEW
        assert plan.is_fully_applicable is False

        applied = import_article.execute_article_import(plan, card_repo)
        assert all(e.apply_result is None for e in applied.entries)
        assert card_repo.created == []
        assert card_repo.relation_updates == []
        assert len(card_repo.relation_updates) == 0

    def test_all_decks_ready_are_all_applied_together(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全デッキがreadyであれば、全件preflight成功として通常どおり適用される。"""
        parse_map = {
            DECK_NAMES[0]: _parsed(DECK_NAMES[0], [_card("新カードA")]),
            DECK_NAMES[1]: _parsed(DECK_NAMES[1], [_card("新カードB")]),
        }
        _patch_common(monkeypatch, DECK_NAMES[:2], parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES[:2]))
        card_repo = FakeCardRepository()

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        assert plan.is_fully_applicable is True
        applied = import_article.execute_article_import(plan, card_repo)

        entries_by_name = {e.deck_name: e for e in applied.entries}
        assert entries_by_name[DECK_NAMES[0]].apply_result is not None
        assert entries_by_name[DECK_NAMES[1]].apply_result is not None
        assert len(card_repo.created) == 2

    def test_one_deck_write_failure_does_not_lose_other_deck_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parse_map = {
            DECK_NAMES[0]: _parsed(DECK_NAMES[0], [_card("失敗カード")]),
            DECK_NAMES[1]: _parsed(DECK_NAMES[1], [_card("成功カード")]),
        }
        _patch_common(monkeypatch, DECK_NAMES[:2], parse_map)
        writer = FakeWriter(_all_existing_decks(DECK_NAMES[:2]))
        card_repo = FakeCardRepository(fail_on_create={"失敗カード"})

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        applied = import_article.execute_article_import(plan, card_repo)

        entries_by_name = {e.deck_name: e for e in applied.entries}
        assert entries_by_name[DECK_NAMES[0]].apply_result.failed
        assert entries_by_name[DECK_NAMES[1]].apply_result.succeeded
        assert len(entries_by_name[DECK_NAMES[1]].apply_result.succeeded) == 1


class TestNoDeleteApiUsed:
    def test_fake_repository_has_no_delete_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parse_map = {DECK_NAMES[0]: _parsed(DECK_NAMES[0], [_card("カード")])}
        _patch_common(monkeypatch, [DECK_NAMES[0]], parse_map)
        writer = FakeWriter({DECK_NAMES[0]: _existing_deck(DECK_NAMES[0], "deck-0")})
        card_repo = FakeCardRepository()

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        import_article.execute_article_import(plan, card_repo)

        assert not hasattr(card_repo, "delete_page")
        assert not hasattr(card_repo, "delete_card")


class TestWriteArticleImportLog:
    def test_log_contains_expected_fields_and_no_secrets(self, tmp_path: Path) -> None:
        entry = import_article.DeckArticleEntry(
            deck_name=DECK_NAMES[0],
            status=import_article.STATUS_READY,
            deck_page_id="deck-0",
            deck_page_url="https://notion.so/deck-0",
            cards_plan=import_cards_module.ImportCardsPlan(
                parsed=_parsed(DECK_NAMES[0], [_card("カード1", 100)]),
                deck_page_id="deck-0",
                decisions=[],
            ),
        )
        plan = import_article.ArticleImportPlan(
            source_url=SOURCE_URL,
            all_deck_names=[DECK_NAMES[0]],
            excluded_deck_names=[],
            entries=[entry],
        )

        paths = import_article.write_article_import_log(
            plan, output_dir=tmp_path, applied=False, timestamp="20260101-000000"
        )

        assert paths.json_path.exists()
        content = paths.json_path.read_text(encoding="utf-8")
        # 注: SOURCE_URL自体に"secrets"を含むため("Secrets of Strixhaven"記事)、
        # 一般的な"secret"部分一致ではなく認証情報特有のキーのみを確認する。
        assert "api_key" not in content.lower()
        assert "authorization" not in content.lower()
        assert "bearer" not in content.lower()

        data = json.loads(content)
        assert data["delete_count"] == 0
        assert data["source_url"] == SOURCE_URL
        assert data["decks"][0]["deck_name"] == DECK_NAMES[0]
        assert data["decks"][0]["extracted_quantity"] == 100


class TestCardMatchOverrideFlowsThroughDeck:
    def test_override_resolved_card_is_applied_and_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """曖昧一致がオーバーライドで解決された場合、そのページだけが更新され、
        決定(CardDecision)にオーバーライド理由が記録されることを確認する
        (特殊仕様側の候補ページは一切触れられない)。
        """
        card = _card("Anguished Unmaking")
        parse_map = {DECK_NAMES[0]: _parsed(DECK_NAMES[0], [card])}
        _patch_common(monkeypatch, [DECK_NAMES[0]], parse_map)
        writer = FakeWriter({DECK_NAMES[0]: _existing_deck(DECK_NAMES[0], "deck-0")})

        resolved = _existing_card("p1-normal")
        card_repo = FakeCardRepository(
            matches={
                "Anguished Unmaking": CardMatch(
                    card=resolved,
                    ambiguous_candidates=[],
                    override_reason="ショーケース版を別レコードとして保持するため",
                )
            },
            deck_relation_ids={"p1-normal": []},
            owned={"p1-normal": True},
        )

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        applied = import_article.execute_article_import(plan, card_repo)

        entry = applied.entries[0]
        assert entry.status == import_article.STATUS_READY
        decision = entry.cards_plan.decisions[0]
        assert decision.override_used == "ショーケース版を別レコードとして保持するため"

        # p1-normal(通常版)以外への書き込みが一切ないこと
        assert len(card_repo.relation_updates) == 1
        assert card_repo.relation_updates[0][0] == "p1-normal"
        assert card_repo.created == []

    def test_deck_log_records_override_usage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        card = _card("Anguished Unmaking")
        parse_map = {DECK_NAMES[0]: _parsed(DECK_NAMES[0], [card])}
        _patch_common(monkeypatch, [DECK_NAMES[0]], parse_map)
        writer = FakeWriter({DECK_NAMES[0]: _existing_deck(DECK_NAMES[0], "deck-0")})

        resolved = _existing_card("p1-normal")
        card_repo = FakeCardRepository(
            matches={
                "Anguished Unmaking": CardMatch(
                    card=resolved, ambiguous_candidates=[], override_reason="ショーケース版を保持"
                )
            },
            deck_relation_ids={"p1-normal": []},
            owned={"p1-normal": True},
        )

        plan = import_article.build_article_import_plan(
            SOURCE_URL, writer, card_repo, allow_count_mismatch=True
        )
        applied = import_article.execute_article_import(plan, card_repo)

        paths = import_article.write_article_deck_logs(
            applied, output_dir=tmp_path, timestamp="20260101-000000"
        )
        assert len(paths) == 1
        data = json.loads(paths[0].read_text(encoding="utf-8"))

        assert data["overrides_used"] == [
            {"card": "Anguished Unmaking", "reason": "ショーケース版を保持"}
        ]
        assert data["delete_count"] == 0


class TestWriteArticleDeckLogs:
    def test_writes_one_file_per_deck_with_slugified_name(self, tmp_path: Path) -> None:
        entries = [
            import_article.DeckArticleEntry(
                deck_name=name,
                status=import_article.STATUS_READY,
                deck_page_id=f"deck-{i}",
                cards_plan=import_cards_module.ImportCardsPlan(
                    parsed=_parsed(name, [_card(f"{name}-c", 100)]),
                    deck_page_id=f"deck-{i}",
                    decisions=[],
                ),
            )
            for i, name in enumerate(DECK_NAMES)
        ]
        plan = import_article.ArticleImportPlan(
            source_url=SOURCE_URL,
            all_deck_names=DECK_NAMES,
            excluded_deck_names=[],
            entries=entries,
        )

        paths = import_article.write_article_deck_logs(
            plan, output_dir=tmp_path, timestamp="20260101-000000"
        )

        assert len(paths) == 5
        for path in paths:
            assert path.exists()
            assert path.name.startswith("article-deck-import-20260101-000000-")
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["delete_count"] == 0
            assert "api_key" not in json.dumps(data).lower()

    def test_no_delete_field_is_always_zero(self, tmp_path: Path) -> None:
        entry = import_article.DeckArticleEntry(
            deck_name="エラーデッキ", status=import_article.STATUS_ERROR, reason="解析失敗"
        )
        plan = import_article.ArticleImportPlan(
            source_url=SOURCE_URL,
            all_deck_names=["エラーデッキ"],
            excluded_deck_names=[],
            entries=[entry],
        )

        paths = import_article.write_article_deck_logs(
            plan, output_dir=tmp_path, timestamp="20260101-000000"
        )

        data = json.loads(paths[0].read_text(encoding="utf-8"))
        assert data["delete_count"] == 0
        assert data["status"] == import_article.STATUS_ERROR
