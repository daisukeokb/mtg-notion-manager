from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.models import DeckCard, ParsedDeckList
from mtg_notion_manager.services.import_article import (
    ArticleImportLogPaths,
    ArticleImportPlan,
    DeckArticleEntry,
)
from mtg_notion_manager.services.import_cards import (
    CardApplyResult,
    ImportCardsPlan,
    ImportCardsResult,
)

runner = CliRunner()
URL = "https://magic.wizards.com/ja/news/announcements/secrets-of-strixhaven-commander-decklists"


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


class FakeNotionClientCtx:
    def __enter__(self) -> FakeNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "CardRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "NotionWriter", lambda client, data_source_id: object())


def _card(name_en: str) -> DeckCard:
    return DeckCard(name_ja=None, name_en=name_en, quantity=1, is_commander=False, source_url=URL)


def _cards_plan(deck_name: str) -> ImportCardsPlan:
    card = _card(f"{deck_name}-card")
    parsed = ParsedDeckList(
        deck_name=deck_name, commander_name=card.display_name, cards=[card], source_url=URL
    )
    return ImportCardsPlan(parsed=parsed, deck_page_id=f"deck-{deck_name}", decisions=[])


def _ready_entry(deck_name: str) -> DeckArticleEntry:
    return DeckArticleEntry(
        deck_name=deck_name,
        status="ready",
        deck_page_id=f"deck-{deck_name}",
        deck_page_url=f"https://notion.so/deck-{deck_name}",
        cards_plan=_cards_plan(deck_name),
    )


def _sample_plan(entries: list[DeckArticleEntry] | None = None) -> ArticleImportPlan:
    if entries is None:
        entries = [_ready_entry("デッキA"), _ready_entry("デッキB")]
    return ArticleImportPlan(
        source_url=URL,
        all_deck_names=[e.deck_name for e in entries],
        excluded_deck_names=[],
        entries=entries,
    )


def _patch_write_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "write_article_import_log",
        lambda plan, output_dir, applied, timestamp=None: ArticleImportLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )


def test_dry_run_shows_summary_and_does_not_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_article_import_plan",
        lambda url, writer, card_repo, exclude_deck_names=None, allow_count_mismatch=False: (
            _sample_plan()
        ),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli,
        "execute_article_import",
        lambda plan, repo, note="": executed.__setitem__("value", True),
    )

    result = runner.invoke(cli.app, ["import-article", URL, "--dry-run"])

    assert result.exit_code == 0
    assert executed["value"] is False
    assert "検出デッキ数: 2" in result.stdout
    assert "処理可能: 2" in result.stdout


def test_without_apply_flag_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_article_import_plan",
        lambda url, writer, card_repo, exclude_deck_names=None, allow_count_mismatch=False: (
            _sample_plan()
        ),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli,
        "execute_article_import",
        lambda plan, repo, note="": executed.__setitem__("value", True),
    )

    result = runner.invoke(cli.app, ["import-article", URL])

    assert result.exit_code == 0
    assert executed["value"] is False


def test_apply_executes_and_reports_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_article_import_plan",
        lambda url, writer, card_repo, exclude_deck_names=None, allow_count_mismatch=False: (
            _sample_plan()
        ),
    )

    applied_entry = DeckArticleEntry(
        deck_name="デッキA",
        status="ready",
        deck_page_id="deck-デッキA",
        cards_plan=_cards_plan("デッキA"),
        apply_result=ImportCardsResult(
            results=[
                CardApplyResult(card=_card("デッキA-card"), action="created", page_id="new-id")
            ]
        ),
    )

    def _fake_execute(plan: ArticleImportPlan, repo: object, note: str = "") -> ArticleImportPlan:
        return ArticleImportPlan(
            source_url=plan.source_url,
            all_deck_names=plan.all_deck_names,
            excluded_deck_names=plan.excluded_deck_names,
            entries=[applied_entry],
        )

    monkeypatch.setattr(cli, "execute_article_import", _fake_execute)

    result = runner.invoke(cli.app, ["import-article", URL, "--apply"])

    assert result.exit_code == 0
    assert "デッキA" in result.stdout


def test_exclude_deck_option_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)

    captured: dict[str, object] = {}

    def _fake_build_plan(
        url: str,
        writer: object,
        card_repo: object,
        exclude_deck_names: list[str] | None = None,
        allow_count_mismatch: bool = False,
    ) -> ArticleImportPlan:
        captured["exclude_deck_names"] = exclude_deck_names
        return _sample_plan([_ready_entry("デッキA")])

    monkeypatch.setattr(cli, "build_article_import_plan", _fake_build_plan)

    result = runner.invoke(
        cli.app, ["import-article", URL, "--exclude-deck", "デッキB", "--dry-run"]
    )

    assert result.exit_code == 0
    assert captured["exclude_deck_names"] == ["デッキB"]


def test_error_status_exits_nonzero_after_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    error_entry = DeckArticleEntry(deck_name="デッキC", status="error", reason="解析エラー")
    monkeypatch.setattr(
        cli,
        "build_article_import_plan",
        lambda url, writer, card_repo, exclude_deck_names=None, allow_count_mismatch=False: (
            _sample_plan([error_entry])
        ),
    )
    monkeypatch.setattr(cli, "execute_article_import", lambda plan, repo, note="": plan)

    result = runner.invoke(cli.app, ["import-article", URL, "--apply"])

    assert result.exit_code == 1


def test_needs_review_only_does_not_fail_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_log(monkeypatch)
    review_entry = DeckArticleEntry(deck_name="デッキD", status="needs_review", reason="曖昧一致")
    monkeypatch.setattr(
        cli,
        "build_article_import_plan",
        lambda url, writer, card_repo, exclude_deck_names=None, allow_count_mismatch=False: (
            _sample_plan([review_entry])
        ),
    )
    monkeypatch.setattr(cli, "execute_article_import", lambda plan, repo, note="": plan)

    result = runner.invoke(cli.app, ["import-article", URL, "--apply"])

    assert result.exit_code == 0


def test_missing_card_data_source_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, ["import-article", URL, "--dry-run"])

    assert result.exit_code == 1
