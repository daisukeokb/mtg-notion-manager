from __future__ import annotations

import httpx
import pytest

from mtg_notion_manager.exceptions import AmbiguousCardMatchError, NotionAPIError
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


def _unverifiable_card(name_en: str) -> DeckCard:
    """日本語名が取得できない(英語記事由来・confirmed_mapping未指定)カード。

    _apply_one()内でresolve_new_card(confirmed_mapping=None)が呼ばれ、
    verified_cardがNoneのままUnverifiedNewCardErrorが送出される。
    """
    return DeckCard(
        name_ja=None, name_en=name_en, quantity=1, is_commander=False, source_url=SOURCE_URL
    )


class FailingCreateCardRepository(FakeCardRepository):
    """指定したカード名でcreate_card()呼び出し時にNotionAPIErrorを送出する。

    それ以外の挙動はFakeCardRepositoryと同じ(継続動作の既存挙動を検証するため)。
    """

    def __init__(self, fail_on_name_ja: set[str], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.fail_on_name_ja = fail_on_name_ja

    def create_card(self, card: DeckCard, deck_page_id: str, note: str = "") -> dict:
        if card.name_ja in self.fail_on_name_ja:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました (500): {card.name_ja}")
        return super().create_card(card, deck_page_id, note=note)


def _http_status_error(status_code: int = 400) -> httpx.HTTPStatusError:
    """実際のNotionClientが送出するNotionAPIErrorのcauseを模す(定義済みのHTTP
    エラー応答 = サーバーが明示的に拒否した、という構造的signal)。"""
    request = httpx.Request("POST", "https://api.notion.com/v1/pages")
    response = httpx.Response(status_code, request=request, text="notion error body")
    return httpx.HTTPStatusError("HTTP error", request=request, response=response)


def _timeout_exception() -> httpx.TimeoutException:
    """実際のNotionClientが送出するNotionAPIErrorのcauseを模す(応答が一切
    得られない = 完了状態が不明、という構造的signal)。"""
    return httpx.TimeoutException("timed out")


class FailingCreateWithCauseCardRepository(FakeCardRepository):
    """指定したカード名でcreate_card()呼び出し時に、指定したcauseを持つ
    NotionAPIErrorを送出する(実際のNotionClient._request()の
    `raise NotionAPIError(...) from exc` 連鎖をそのまま模したfake)。

    create_card()の呼び出し回数も記録し、「1回だけ試行される」ことを検証できる
    ようにする。
    """

    def __init__(self, fail_on_name_ja: dict[str, BaseException], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.fail_on_name_ja = fail_on_name_ja
        self.create_attempts: list[str | None] = []

    def create_card(self, card: DeckCard, deck_page_id: str, note: str = "") -> dict:
        self.create_attempts.append(card.name_ja)
        cause = self.fail_on_name_ja.get(card.name_ja or "")
        if cause is not None:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました: {card.name_ja}") from cause
        return super().create_card(card, deck_page_id, note=note)


class FailingRelationUpdateWithCauseCardRepository(FakeCardRepository):
    """指定したpage_idでapply_relation_update()呼び出し時に、指定したcauseを持つ
    NotionAPIErrorを送出する(update_page()経由の失敗を模す)。"""

    def __init__(self, fail_on_page_id: dict[str, BaseException], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.fail_on_page_id = fail_on_page_id

    def apply_relation_update(
        self, existing: ExistingCard, deck_page_id: str, current_deck_ids: list[str]
    ) -> dict:
        cause = self.fail_on_page_id.get(existing.page_id)
        if cause is not None:
            raise NotionAPIError(
                f"Notion API呼び出しに失敗しました: {existing.page_id}"
            ) from cause
        return super().apply_relation_update(existing, deck_page_id, current_deck_ids)


class TestPartialResultPreservationOnAbort:
    """UnverifiedNewCardErrorによる中断時、それより前のCardApplyResultが
    PartialImportAbortedError.completed_resultsとして回収可能であることを検証する。

    中断semantics自体(同じ中断位置・以降のカード未処理・書き込み回数/順序不変)を
    変更していないことも併せて確認する。
    """

    def test_first_card_abort_produces_empty_completed_results(self) -> None:
        aborting_card = _unverifiable_card("Unresolvable Card")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([aborting_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=aborting_card, action="create")],
        )
        repo = FakeCardRepository()

        with pytest.raises(import_cards.PartialImportAbortedError) as exc_info:
            import_cards.execute_import_cards(plan, repo)

        assert exc_info.value.completed_results == ()
        assert repo.created == []  # 中断したカード自身への書き込みも試行されない
        assert repo.relation_updates == []

    def test_successful_prefix_is_preserved_and_ordered(self) -> None:
        card_a = _card("カードA")
        card_b = _card("カードB")
        aborting_card = _unverifiable_card("Unresolvable Card")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card_a, card_b, aborting_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=card_a, action="create"),
                CardDecision(card=card_b, action="create"),
                CardDecision(card=aborting_card, action="create"),
            ],
        )
        repo = FakeCardRepository()

        with pytest.raises(import_cards.PartialImportAbortedError) as exc_info:
            import_cards.execute_import_cards(plan, repo)

        completed = exc_info.value.completed_results
        assert len(completed) == 2
        assert completed[0].card is card_a
        assert completed[0].action == "created"
        assert completed[1].card is card_b
        assert completed[1].action == "created"
        # 中断したカード自身へは書き込みが試行されない(A・Bの2件だけがcreate_card対象)。
        assert len(repo.created) == 2

    def test_mixed_completed_outcomes_including_notion_api_error(self) -> None:
        """A: 成功 / B: NotionAPIErrorでfailed / C: 成功 / D: 中断 / E: 未処理、の順序。"""
        card_a = _card("カードA")
        card_b = _card("カードB")
        card_c = _card("カードC")
        aborting_card = _unverifiable_card("Unresolvable Card")
        card_e = _card("カードE")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card_a, card_b, card_c, aborting_card, card_e]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=card_a, action="create"),
                CardDecision(card=card_b, action="create"),
                CardDecision(card=card_c, action="create"),
                CardDecision(card=aborting_card, action="create"),
                CardDecision(card=card_e, action="create"),
            ],
        )
        repo = FailingCreateCardRepository(fail_on_name_ja={"カードB"})

        with pytest.raises(import_cards.PartialImportAbortedError) as exc_info:
            import_cards.execute_import_cards(plan, repo)

        completed = exc_info.value.completed_results
        assert [r.action for r in completed] == ["created", "failed", "created"]
        assert completed[0].card is card_a
        assert completed[1].card is card_b
        assert completed[1].error is not None  # NotionAPIError由来のfailed結果も保持される
        assert completed[2].card is card_c
        # 中断カード(D)・後続カード(E)のいずれもcreate_card対象に含まれない。
        assert [c.name_ja for c, _, _ in repo.created] == ["カードA", "カードC"]

    def test_no_notion_write_attempted_for_the_aborting_card(self) -> None:
        """resolve_new_card()の失敗はcard_repo.create_card()到達前に検知されるため、
        中断したカード自身へのNotion書き込みは一切試行されない。"""
        aborting_card = _unverifiable_card("Unresolvable Card")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([aborting_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=aborting_card, action="create")],
        )
        repo = FakeCardRepository()

        with pytest.raises(import_cards.PartialImportAbortedError):
            import_cards.execute_import_cards(plan, repo)

        assert repo.created == []

    def test_later_cards_are_not_processed_at_all(self) -> None:
        """中断より後のdecisionはcreate_card/get_deck_relation_ids/apply_relation_update
        のいずれも一切呼ばれない(単に書き込み件数だけでなく、処理自体が発生しないことを確認)。
        """
        aborting_card = _unverifiable_card("Unresolvable Card")
        later_create = _card("後続カード(create)")
        later_existing = _existing("later-p1")
        later_update_card = _card("後続カード(relation_update)")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([aborting_card, later_create, later_update_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=aborting_card, action="create"),
                CardDecision(card=later_create, action="create"),
                CardDecision(
                    card=later_update_card, action="relation_update", existing=later_existing
                ),
            ],
        )
        repo = FakeCardRepository()

        with pytest.raises(import_cards.PartialImportAbortedError):
            import_cards.execute_import_cards(plan, repo)

        assert repo.created == []
        assert repo.relation_updates == []

    def test_original_cause_is_preserved_via_exception_chaining(self) -> None:
        aborting_card = _unverifiable_card("Unresolvable Card")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([aborting_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=aborting_card, action="create")],
        )
        repo = FakeCardRepository()

        with pytest.raises(import_cards.PartialImportAbortedError) as exc_info:
            import_cards.execute_import_cards(plan, repo)

        cause = exc_info.value.__cause__
        assert cause is not None
        assert type(cause) is import_cards.UnverifiedNewCardError
        assert str(cause) == str(exc_info.value)  # carrierのstr()は元の例外と同一

    def test_carrier_is_still_an_unverified_new_card_error(self) -> None:
        """既存の互換性gate: isinstance(exc, UnverifiedNewCardError) を維持する
        (このモジュールではUnverifiedNewCardErrorへのexcept依存は現状存在しないが、
        将来のcatch境界のためにhierarchy互換性を保つ)。
        """
        aborting_card = _unverifiable_card("Unresolvable Card")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([aborting_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=aborting_card, action="create")],
        )
        repo = FakeCardRepository()

        try:
            import_cards.execute_import_cards(plan, repo)
            pytest.fail("PartialImportAbortedError was not raised")
        except import_cards.UnverifiedNewCardError as exc:
            assert isinstance(exc, import_cards.PartialImportAbortedError)

    def test_existing_notion_api_error_continue_on_error_is_unchanged(self) -> None:
        """本Work Unitより前から存在するNotionAPIError継続動作の回帰確認
        (このケース単体では新carrier例外は一切関与しない)。"""
        card_a = _card("カードA")
        card_b = _card("カードB")
        card_c = _card("カードC")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card_a, card_b, card_c]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=card_a, action="create"),
                CardDecision(card=card_b, action="create"),
                CardDecision(card=card_c, action="create"),
            ],
        )
        repo = FailingCreateCardRepository(fail_on_name_ja={"カードB"})

        result = import_cards.execute_import_cards(plan, repo)

        assert [r.action for r in result.results] == ["created", "failed", "created"]
        assert len(repo.created) == 2  # AとCのみ(Bは例外送出のため未記録)


class TestFailedWriteMetadata:
    """CardApplyResult(action="failed")のfailed_operation/failed_completionが、
    NotionAPIError.__cause__の型だけから(メッセージ文字列を一切見ずに)構造的に
    決まることを検証する。"""

    def test_known_failed_create_via_http_status_error(self) -> None:
        card = _card("カードA")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="create")],
        )
        repo = FailingCreateWithCauseCardRepository(
            fail_on_name_ja={"カードA": _http_status_error()}
        )

        result = import_cards.execute_import_cards(plan, repo)

        r = result.results[0]
        assert r.action == "failed"
        assert r.failed_operation == import_cards.FailedWriteOperation.CREATE
        assert r.failed_completion == import_cards.WriteCompletion.KNOWN_FAILED
        assert r.error == "Notion API呼び出しに失敗しました: カードA"  # human文字列は不変
        assert repo.create_attempts == ["カードA"]  # 1回だけ試行される

    def test_unknown_completion_create_via_timeout(self) -> None:
        card = _card("カードA")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="create")],
        )
        repo = FailingCreateWithCauseCardRepository(
            fail_on_name_ja={"カードA": _timeout_exception()}
        )

        result = import_cards.execute_import_cards(plan, repo)

        r = result.results[0]
        assert r.action == "failed"
        assert r.failed_operation == import_cards.FailedWriteOperation.CREATE
        assert r.failed_completion == import_cards.WriteCompletion.UNKNOWN
        assert repo.create_attempts == ["カードA"]  # 非べき等操作は1回だけ試行される

    def test_relation_update_known_failed_via_http_status_error(self) -> None:
        card = _card("既存カード")
        existing = _existing("p1")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="relation_update", existing=existing)],
        )
        repo = FailingRelationUpdateWithCauseCardRepository(
            fail_on_page_id={"p1": _http_status_error()}
        )

        result = import_cards.execute_import_cards(plan, repo)

        r = result.results[0]
        assert r.failed_operation == import_cards.FailedWriteOperation.RELATION_UPDATE
        assert r.failed_completion == import_cards.WriteCompletion.KNOWN_FAILED

    def test_relation_update_unknown_completion_via_timeout(self) -> None:
        """update_page()はタイムアウト時に自動リトライされる(idempotent)が、
        リトライを使い切った後の完了状態は依然として不明であるため、
        KNOWN_FAILEDではなくUNKNOWNのままとする。"""
        card = _card("既存カード")
        existing = _existing("p1")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="relation_update", existing=existing)],
        )
        repo = FailingRelationUpdateWithCauseCardRepository(
            fail_on_page_id={"p1": _timeout_exception()}
        )

        result = import_cards.execute_import_cards(plan, repo)

        r = result.results[0]
        assert r.failed_operation == import_cards.FailedWriteOperation.RELATION_UPDATE
        assert r.failed_completion == import_cards.WriteCompletion.UNKNOWN

    def test_successful_create_has_no_failure_metadata(self) -> None:
        card = _card("新カード")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="create")],
        )
        repo = FakeCardRepository()

        result = import_cards.execute_import_cards(plan, repo)

        r = result.results[0]
        assert r.action == "created"
        assert r.failed_operation is None
        assert r.failed_completion is None

    def test_unchanged_has_no_failure_metadata(self) -> None:
        card = _card("既存カード")
        existing = _existing("p1")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="unchanged", existing=existing)],
        )
        repo = FakeCardRepository()

        result = import_cards.execute_import_cards(plan, repo)

        r = result.results[0]
        assert r.failed_operation is None
        assert r.failed_completion is None

    def test_partial_abort_preserves_structured_failure_metadata(self) -> None:
        """A: 成功 / B: NotionAPIError(known_failed)でfailed / C: 中断、の順序。
        completed_resultsにBのstructured metadataがそのまま残ることを確認する。"""
        card_a = _card("カードA")
        card_b = _card("カードB")
        aborting_card = _unverifiable_card("Unresolvable Card")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card_a, card_b, aborting_card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=card_a, action="create"),
                CardDecision(card=card_b, action="create"),
                CardDecision(card=aborting_card, action="create"),
            ],
        )
        repo = FailingCreateWithCauseCardRepository(
            fail_on_name_ja={"カードB": _http_status_error()}
        )

        with pytest.raises(import_cards.PartialImportAbortedError) as exc_info:
            import_cards.execute_import_cards(plan, repo)

        completed = exc_info.value.completed_results
        assert [r.action for r in completed] == ["created", "failed"]
        assert completed[0].card is card_a
        assert completed[0].failed_operation is None
        assert completed[0].failed_completion is None
        assert completed[1].card is card_b
        assert completed[1].failed_operation == import_cards.FailedWriteOperation.CREATE
        assert completed[1].failed_completion == import_cards.WriteCompletion.KNOWN_FAILED

    def test_classification_is_independent_of_message_text(self) -> None:
        """メッセージ文言が何であっても、__cause__の型だけで分類されることを証明する
        (メッセージに「タイムアウト」という語を含めても、causeがHTTPStatusErrorなら
        KNOWN_FAILEDのまま――文字列解析への依存が無いことの直接的な証拠)。"""
        card = _card("カードA")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[CardDecision(card=card, action="create")],
        )

        class MisleadingTextRepository(FakeCardRepository):
            def create_card(
                self, card: DeckCard, deck_page_id: str, note: str = ""
            ) -> dict:
                raise NotionAPIError(
                    "タイムアウトしましたが実際には成功している可能性があります"
                ) from _http_status_error()

        result = import_cards.execute_import_cards(plan, MisleadingTextRepository())

        assert result.results[0].failed_completion == import_cards.WriteCompletion.KNOWN_FAILED

    def test_existing_notion_api_error_regression_still_passes_with_new_fields(self) -> None:
        """既存のFailingCreateCardRepository(causeなしのbare NotionAPIError)は
        引き続きaction="failed"へ変換され、continue-on-error動作は不変(新metadata
        フィールドの追加後もこの既存回帰は無変更で成立する)。"""
        card_a = _card("カードA")
        card_b = _card("カードB")
        plan = import_cards.ImportCardsPlan(
            parsed=_parsed([card_a, card_b]),
            deck_page_id=DECK_PAGE_ID,
            decisions=[
                CardDecision(card=card_a, action="create"),
                CardDecision(card=card_b, action="create"),
            ],
        )
        repo = FailingCreateCardRepository(fail_on_name_ja={"カードB"})

        result = import_cards.execute_import_cards(plan, repo)

        assert [r.action for r in result.results] == ["created", "failed"]
        # cause無しのNotionAPIErrorはUNKNOWN側へ倒す(安全側のデフォルト)。
        assert result.results[1].failed_completion == import_cards.WriteCompletion.UNKNOWN
        assert result.results[1].failed_operation == import_cards.FailedWriteOperation.CREATE


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
        decision = import_cards._decide(card, DECK_PAGE_ID, repo, SOURCE_URL, "吸血鬼の血統", None)
        assert decision.action == "relation_update"

        # 適用後、リポジトリの状態が更新されたとみなす(実際のNotion側の状態変化を模擬)
        repo.deck_relation_ids["p1"] = [DECK_PAGE_ID]

        # 2回目: 既にリレーション済みなのでunchangedと判定される
        decision_again = import_cards._decide(
            card, DECK_PAGE_ID, repo, SOURCE_URL, "吸血鬼の血統", None
        )
        assert decision_again.action == "unchanged"
