from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.exceptions import AmbiguousCardMatchError
from mtg_notion_manager.models import CardDecision, DeckCard, ExistingDeck, ParsedDeckList
from mtg_notion_manager.services.import_cards import (
    CardApplyResult,
    ImportCardsPlan,
    ImportCardsResult,
)

runner = CliRunner()
URL = "https://mtg-jp.com/reading/publicity/0035593/"
DECK_PAGE_ID = "39aa97c8-7142-81cb-af6e-d7a0446dea2c"


def _fake_config() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id="card-ds-id",
    )


def _fake_config_without_card_db() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id=None,
    )


def _card(name_ja: str) -> DeckCard:
    return DeckCard(name_ja=name_ja, name_en=None, quantity=1, is_commander=False, source_url=URL)


def _sample_plan(decisions: list[CardDecision] | None = None) -> ImportCardsPlan:
    if decisions is None:
        decisions = [
            CardDecision(card=_card("新カード"), action="create"),
            CardDecision(card=_card("既存カード"), action="relation_update"),
            CardDecision(card=_card("変更なしカード"), action="unchanged"),
        ]
    parsed = ParsedDeckList(
        deck_name="吸血鬼の血統",
        commander_name="マウアーの太祖、ストレイファン",
        cards=[d.card for d in decisions],
        source_url=URL,
    )
    return ImportCardsPlan(parsed=parsed, deck_page_id=DECK_PAGE_ID, decisions=decisions)


class FakeNotionClientCtx:
    def __enter__(self) -> FakeNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_notion_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(
        cli, "CardRepository", lambda client, data_source_id, overrides=None: object()
    )


def test_dry_run_shows_summary_and_does_not_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_import_cards_plan",
        lambda url, deck_page_id, repo, deck_name=None, allow_count_mismatch=False: _sample_plan(),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli,
        "execute_import_cards",
        lambda plan, repo, note="": executed.__setitem__("value", True),
    )

    result = runner.invoke(
        cli.app,
        ["import-cards", URL, "--deck-page-id", DECK_PAGE_ID, "--dry-run"],
    )

    assert result.exit_code == 0
    assert executed["value"] is False
    assert "吸血鬼の血統" in result.stdout
    assert "新規作成予定: 1" in result.stdout
    assert "リレーション追加予定: 1" in result.stdout
    assert "変更なし: 1" in result.stdout


def test_without_apply_flag_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_import_cards_plan",
        lambda url, deck_page_id, repo, deck_name=None, allow_count_mismatch=False: _sample_plan(),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli,
        "execute_import_cards",
        lambda plan, repo, note="": executed.__setitem__("value", True),
    )

    result = runner.invoke(cli.app, ["import-cards", URL, "--deck-page-id", DECK_PAGE_ID])

    assert result.exit_code == 0
    assert executed["value"] is False


def test_apply_executes_and_reports_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_import_cards_plan",
        lambda url, deck_page_id, repo, deck_name=None, allow_count_mismatch=False: _sample_plan(),
    )

    apply_result = ImportCardsResult(
        results=[
            CardApplyResult(card=_card("新カード"), action="created", page_id="new-id"),
            CardApplyResult(card=_card("既存カード"), action="relation_updated", page_id="p1"),
            CardApplyResult(card=_card("変更なしカード"), action="unchanged", page_id="p2"),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    result = runner.invoke(
        cli.app, ["import-cards", URL, "--deck-page-id", DECK_PAGE_ID, "--apply"]
    )

    assert result.exit_code == 0
    assert "成功: 3件" in result.stdout


def test_apply_with_failed_card_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_import_cards_plan",
        lambda url, deck_page_id, repo, deck_name=None, allow_count_mismatch=False: _sample_plan(),
    )
    apply_result = ImportCardsResult(
        results=[
            CardApplyResult(card=_card("失敗カード"), action="failed", error="接続エラー"),
        ]
    )
    monkeypatch.setattr(cli, "execute_import_cards", lambda plan, repo, note="": apply_result)

    result = runner.invoke(
        cli.app, ["import-cards", URL, "--deck-page-id", DECK_PAGE_ID, "--apply"]
    )

    assert result.exit_code == 1


def test_ambiguous_match_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)
    decisions = [CardDecision(card=_card("曖昧カード"), action="ambiguous", detail="2件の候補")]
    monkeypatch.setattr(
        cli,
        "build_import_cards_plan",
        lambda url, deck_page_id, repo, deck_name=None, allow_count_mismatch=False: _sample_plan(
            decisions
        ),
    )

    def _raise(*args: object, **kwargs: object) -> None:
        raise AmbiguousCardMatchError("曖昧一致のため中止しました")

    monkeypatch.setattr(cli, "execute_import_cards", _raise)

    result = runner.invoke(
        cli.app, ["import-cards", URL, "--deck-page-id", DECK_PAGE_ID, "--apply"]
    )

    assert result.exit_code == 1
    assert "曖昧" in result.stdout


def test_missing_card_data_source_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(
        cli.app, ["import-cards", URL, "--deck-page-id", DECK_PAGE_ID, "--dry-run"]
    )

    assert result.exit_code == 1


def test_missing_deck_name_and_deck_page_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(cli.app, ["import-cards", URL, "--dry-run"])

    assert result.exit_code == 1


def test_deck_lookup_by_name_when_page_id_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)

    existing_deck = ExistingDeck(
        page_id=DECK_PAGE_ID, page_url="https://notion.so/deck", properties={}
    )

    class FakeWriter:
        def __init__(self, client: object, data_source_id: str) -> None:
            pass

        def find_existing_deck(self, name: str) -> ExistingDeck | None:
            assert name == "吸血鬼の血統"
            return existing_deck

    monkeypatch.setattr(cli, "NotionWriter", FakeWriter)

    captured: dict[str, str] = {}

    def _fake_build_plan(
        url: str,
        deck_page_id: str,
        repo: object,
        deck_name: str | None = None,
        allow_count_mismatch: bool = False,
    ) -> ImportCardsPlan:
        captured["deck_page_id"] = deck_page_id
        return _sample_plan()

    monkeypatch.setattr(cli, "build_import_cards_plan", _fake_build_plan)

    result = runner.invoke(
        cli.app, ["import-cards", URL, "--deck-name", "吸血鬼の血統", "--dry-run"]
    )

    assert result.exit_code == 0
    assert captured["deck_page_id"] == DECK_PAGE_ID


def test_deck_not_found_by_name_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion_client(monkeypatch)

    class FakeWriter:
        def __init__(self, client: object, data_source_id: str) -> None:
            pass

        def find_existing_deck(self, name: str) -> ExistingDeck | None:
            return None

    monkeypatch.setattr(cli, "NotionWriter", FakeWriter)

    result = runner.invoke(
        cli.app, ["import-cards", URL, "--deck-name", "存在しないデッキ", "--dry-run"]
    )

    assert result.exit_code == 1
