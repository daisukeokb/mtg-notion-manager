from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.services.verify_import import (
    ArticleVerifyReport,
    DeckVerifyEntry,
    VerifyReportPaths,
)

runner = CliRunner()
URL = "https://magic.wizards.com/ja/news/announcements/secrets-of-strixhaven-commander-decklists"

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain_help_text(stdout: str) -> str:
    """rich/typerの--help出力から、環境依存のANSIコード・改行折り返しを除去する。

    --helpの折り返し幅・色付けは実行環境(TTY有無・COLUMNS・CI)に応じてrichが
    動的に決めるため、生のstdoutへ部分文字列一致させるテストは環境依存で壊れる。
    """
    return _ANSI_ESCAPE_RE.sub("", stdout).replace("\n", "")


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


def _patch_notion(monkeypatch: pytest.MonkeyPatch, client: object | None = None) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: client or FakeNotionClientCtx())
    monkeypatch.setattr(
        cli, "CardRepository", lambda client, data_source_id, overrides=None: object()
    )
    monkeypatch.setattr(cli, "NotionWriter", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_card_match_overrides", lambda: object())


def _verified_entry(deck_name: str = "デッキA") -> DeckVerifyEntry:
    return DeckVerifyEntry(
        deck_name=deck_name,
        verification_status="verified",
        verification_errors=[],
        deck_page_id="deck-1",
        deck_page_url="https://notion.so/deck-1",
        extracted_card_count=100,
        unique_card_count=87,
        existing_card_count=87,
        new_card_count=0,
        ambiguous_match_count=0,
        error_count=0,
        overrides_used=[],
        expected_relation_page_ids=["p1", "p2"],
        actual_relation_page_ids=["p1", "p2"],
        missing_relation_page_ids=[],
        unexpected_relation_page_ids=[],
    )


def _mismatch_entry(deck_name: str = "デッキB") -> DeckVerifyEntry:
    return DeckVerifyEntry(
        deck_name=deck_name,
        verification_status="mismatch",
        verification_errors=["新規カードが1件あります(カードDB未登録の可能性)"],
        deck_page_id="deck-2",
        deck_page_url="https://notion.so/deck-2",
        extracted_card_count=100,
        unique_card_count=85,
        existing_card_count=84,
        new_card_count=1,
        ambiguous_match_count=0,
        error_count=0,
        overrides_used=[],
        expected_relation_page_ids=["p3"],
        actual_relation_page_ids=["p3"],
        missing_relation_page_ids=[],
        unexpected_relation_page_ids=[],
    )


def _report(
    entries: list[DeckVerifyEntry], all_deck_names: list[str] | None = None
) -> ArticleVerifyReport:
    return ArticleVerifyReport(
        source_url=URL,
        all_deck_names=all_deck_names or [e.deck_name for e in entries],
        entries=entries,
    )


def _patch_write_report(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def fake_write(
        report: object, output_dir: Path, timestamp: str | None = None
    ) -> VerifyReportPaths:
        calls.append((report, output_dir))
        return VerifyReportPaths(json_path=output_dir / "verify-import-x.json")

    monkeypatch.setattr(cli, "write_verify_report", fake_write)
    return calls


class TestSuccessExitCode:
    def test_all_verified_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        write_calls = _patch_write_report(monkeypatch)
        monkeypatch.setattr(
            cli,
            "build_verify_import_plan",
            lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _report(
                [_verified_entry()]
            ),
        )

        result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

        assert result.exit_code == 0
        assert len(write_calls) == 1
        assert "成功数: 1" in result.stdout
        assert "失敗数: 0" in result.stdout


class TestMismatchExitCode:
    def test_any_mismatch_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        _patch_write_report(monkeypatch)
        monkeypatch.setattr(
            cli,
            "build_verify_import_plan",
            lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _report(
                [_verified_entry(), _mismatch_entry()]
            ),
        )

        result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

        assert result.exit_code == 1
        assert "失敗数: 1" in result.stdout
        assert "新規カード数: 1" in result.stdout

    def test_detail_flag_shows_verification_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        _patch_write_report(monkeypatch)
        monkeypatch.setattr(
            cli,
            "build_verify_import_plan",
            lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _report(
                [_mismatch_entry()]
            ),
        )

        result = runner.invoke(
            cli.app, ["verify-import", URL, "--detail", "--output-dir", str(tmp_path)]
        )

        assert result.exit_code == 1
        assert "新規カードが1件あります" in result.stdout


class TestExecutionErrorExitCode:
    def test_config_error_returns_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise_config_error() -> Config:
            from mtg_notion_manager.config import ConfigError

            raise ConfigError("NOTION_API_KEY が設定されていません")

        monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config_error))

        result = runner.invoke(cli.app, ["verify-import", URL])

        assert result.exit_code == 2

    def test_missing_card_data_source_id_returns_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

        result = runner.invoke(cli.app, ["verify-import", URL])

        assert result.exit_code == 2

    def test_notion_read_failure_returns_two(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)

        def fake_build(
            url: str,
            client: object,
            writer: object,
            card_repo: object,
            include_deck_names=None,
            deck_page_map_path=None,
            confirmed_card_map_path=None,
        ):
            raise NotionAPIError("Notion API呼び出しに失敗しました (500): boom")

        monkeypatch.setattr(cli, "build_verify_import_plan", fake_build)

        result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

        assert result.exit_code == 2

    def test_execution_error_is_not_treated_as_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一時的なNotion読取失敗は登録状態の差分(exit 1)と混同されない(exit 2)。"""
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)

        def fake_build(
            url: str,
            client: object,
            writer: object,
            card_repo: object,
            include_deck_names=None,
            deck_page_map_path=None,
            confirmed_card_map_path=None,
        ):
            raise NotionAPIError("timeout")

        monkeypatch.setattr(cli, "build_verify_import_plan", fake_build)

        result = runner.invoke(cli.app, ["verify-import", URL])

        assert result.exit_code == 2
        assert result.exit_code != 1


class TestIncludeDeckOption:
    def test_include_deck_is_passed_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        _patch_write_report(monkeypatch)
        captured: dict[str, object] = {}

        def fake_build(
            url: str,
            client: object,
            writer: object,
            card_repo: object,
            include_deck_names=None,
            deck_page_map_path=None,
            confirmed_card_map_path=None,
        ):
            captured["include_deck_names"] = include_deck_names
            return _report([_verified_entry("プリズマリの技巧")])

        monkeypatch.setattr(cli, "build_verify_import_plan", fake_build)

        result = runner.invoke(
            cli.app,
            [
                "verify-import",
                URL,
                "--include-deck",
                "プリズマリの技巧",
                "--output-dir",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0
        assert captured["include_deck_names"] == ["プリズマリの技巧"]

    def test_multiple_include_deck_options(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        _patch_write_report(monkeypatch)
        captured: dict[str, object] = {}

        def fake_build(
            url: str,
            client: object,
            writer: object,
            card_repo: object,
            include_deck_names=None,
            deck_page_map_path=None,
            confirmed_card_map_path=None,
        ):
            captured["include_deck_names"] = include_deck_names
            return _report([_verified_entry("A"), _verified_entry("B")])

        monkeypatch.setattr(cli, "build_verify_import_plan", fake_build)

        result = runner.invoke(
            cli.app,
            [
                "verify-import",
                URL,
                "--include-deck",
                "A",
                "--include-deck",
                "B",
                "--output-dir",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0
        assert captured["include_deck_names"] == ["A", "B"]


class TestNoApplyOption:
    def test_apply_flag_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)

        result = runner.invoke(cli.app, ["verify-import", URL, "--apply"])

        assert result.exit_code != 0
        assert "No such option" in result.stdout or "no such option" in result.stdout.lower()


class TestReportWriting:
    def test_report_path_is_printed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        _patch_write_report(monkeypatch)
        monkeypatch.setattr(
            cli,
            "build_verify_import_plan",
            lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _report(
                [_verified_entry()]
            ),
        )

        result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

        assert result.exit_code == 0
        assert "verify-import-x.json" in result.stdout


class TestReadOnlyGuarantee:
    def test_command_never_touches_notion_client_write_methods(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class StrictFakeClient:
            def __enter__(self) -> StrictFakeClient:
                return self

            def __exit__(self, *exc_info: object) -> None:
                return None

            def update_page(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("update_page must not be called by verify-import")

            def create_page(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("create_page must not be called by verify-import")

        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch, client=StrictFakeClient())
        _patch_write_report(monkeypatch)
        monkeypatch.setattr(
            cli,
            "build_verify_import_plan",
            lambda url, client, writer, card_repo, include_deck_names=None, **kwargs: _report(
                [_verified_entry()]
            ),
        )

        result = runner.invoke(cli.app, ["verify-import", URL, "--output-dir", str(tmp_path)])

        assert result.exit_code == 0


def test_help_mentions_deck_page_map() -> None:
    result = runner.invoke(cli.app, ["verify-import", "--help"])

    assert result.exit_code == 0
    assert "--deck-page-map" in _plain_help_text(result.stdout)


def test_help_mentions_confirmed_card_map() -> None:
    result = runner.invoke(cli.app, ["verify-import", "--help"])

    assert result.exit_code == 0
    assert "--confirmed-card-map" in _plain_help_text(result.stdout)


def test_plain_help_text_strips_ansi_and_wrapping() -> None:
    """CI環境でrichが折り返し・色付けした--help出力でも検出できることを確認する
    (GitHub Actions run 29240645584 で実際に発生した失敗の再現)。
    """
    wrapped = "\x1b[1m--deck\x1b[0m\n\x1b[2m-page-map\x1b[0m"
    assert "--deck-page-map" in _plain_help_text(wrapped)


def test_deck_page_map_path_is_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    _patch_write_report(monkeypatch)

    captured: dict[str, object] = {}

    def fake_build(
        url: str,
        client: object,
        writer: object,
        card_repo: object,
        include_deck_names=None,
        deck_page_map_path=None,
        confirmed_card_map_path=None,
    ):
        captured["deck_page_map_path"] = deck_page_map_path
        return _report([_verified_entry()])

    monkeypatch.setattr(cli, "build_verify_import_plan", fake_build)

    result = runner.invoke(
        cli.app,
        [
            "verify-import",
            URL,
            "--deck-page-map",
            "config/deck_page_mapping.example.json",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert str(captured["deck_page_map_path"]) == "config/deck_page_mapping.example.json"


def test_invalid_deck_page_map_exits_with_execution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)

    from mtg_notion_manager.exceptions import DeckPageMappingConfigError

    def fake_build(*args: object, **kwargs: object):
        raise DeckPageMappingConfigError("設定が不正です")

    monkeypatch.setattr(cli, "build_verify_import_plan", fake_build)

    result = runner.invoke(cli.app, ["verify-import", URL, "--deck-page-map", "bad.json"])

    assert result.exit_code == 2
