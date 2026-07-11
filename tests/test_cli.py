from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config, ConfigError
from mtg_notion_manager.models import DeckRecord, ExistingDeck
from mtg_notion_manager.services.import_deck import ImportPlan

runner = CliRunner()
URL = "https://mtg-jp.com/reading/publicity/0038046/"


def _fake_config() -> Config:
    return Config(notion_api_key="secret_test", commander_data_source_id="ds-id")


def _sample_plan(existing: ExistingDeck | None = None, diff: list | None = None) -> ImportPlan:
    record = DeckRecord(
        name="動き出した兵隊",
        commander="茨の吟遊詩人、べロ",
        set_name="ブルームバロウ",
        colors=["赤", "緑"],
        deck_list_url=URL,
    )
    return ImportPlan(record=record, existing=existing, diff=diff or [])


def test_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "build_import_plan", lambda url, writer: _sample_plan())

    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_import", lambda plan, writer: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["import", URL, "--dry-run"])

    assert result.exit_code == 0
    assert executed["value"] is False
    assert "--dry-run" in result.stdout or "書き込み" in result.stdout


def test_duplicate_deck_is_skipped_without_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    existing = ExistingDeck(page_id="p1", page_url="https://notion.so/p1", properties={})
    monkeypatch.setattr(
        cli, "build_import_plan", lambda url, writer: _sample_plan(existing=existing)
    )

    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_import", lambda plan, writer: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["import", URL])

    assert result.exit_code == 0
    assert executed["value"] is False
    assert "重複" in result.stdout


def test_confirmed_import_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "build_import_plan", lambda url, writer: _sample_plan())

    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_import", lambda plan, writer: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["import", URL], input="y\n")

    assert result.exit_code == 0
    assert executed["value"] is True


def test_declined_confirmation_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "build_import_plan", lambda url, writer: _sample_plan())

    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_import", lambda plan, writer: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["import", URL], input="n\n")

    assert result.exit_code == 0
    assert executed["value"] is False


def test_config_error_exits_with_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_config_error() -> Config:
        raise ConfigError("NOTION_API_KEY が設定されていません")

    monkeypatch.setattr(cli.Config, "load", staticmethod(raise_config_error))

    result = runner.invoke(cli.app, ["import", URL, "--dry-run"])

    assert result.exit_code == 1
