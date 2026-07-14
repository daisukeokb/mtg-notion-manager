from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.services import title_update_dry_run as planner

runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain_help_text(stdout: str) -> str:
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


def _patch_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "install_http_write_guard", lambda client: [])
    monkeypatch.setattr(cli, "ReadOnlyNotionClient", lambda client: object())


def _entry(**overrides: object) -> dict:
    base = {
        "page_id": "page-1",
        "expected_current_title": "Elusive Otter",
        "confirmed_new_title": "神出鬼没のカワウソ",
        "expected_english_name": "Elusive Otter",
        "source_deck_ids": ["deck-1"],
        "verification_status": "human_confirmed",
        "verification_actor": "user",
        "verification_note": "Japanese card title explicitly confirmed by the user",
    }
    base.update(overrides)
    return base


def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    data = {
        "schema_version": 1,
        "purpose": "plan_existing_card_title_updates",
        "source_audit_report": None,
        "entries": entries,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _eligible_report(entries: list[dict]) -> planner.TitleUpdateDryRunReport:
    plan_entries = [
        planner.TitleUpdatePlanEntry(
            page_id=e["page_id"],
            current_title=e["expected_current_title"],
            expected_current_title=e["expected_current_title"],
            confirmed_new_title=e["confirmed_new_title"],
            current_english_name=e["expected_english_name"],
            expected_english_name=e["expected_english_name"],
            verification_status="human_confirmed",
            verification_actor="user",
            verification_note="note",
            current_title_matches=True,
            english_name_matches=True,
            is_archived_or_trashed=False,
            same_title_check=planner.SameTitleCheck("x", "no_existing_same_title", []),
            relation_snapshot=planner.RelationSnapshot([], 0, True, True, []),
            eligible_for_future_update=True,
            blocking_reasons=[],
        )
        for e in entries
    ]
    return planner.TitleUpdateDryRunReport(
        audit_timestamp="2026-07-14T00:00:00",
        manifest_path="manifest.json",
        expected_target_count=len(entries),
        entries=plan_entries,
        method_call_log=["get_page"],
        http_call_log=[],
    )


def _blocked_report(entries: list[dict]) -> planner.TitleUpdateDryRunReport:
    report = _eligible_report(entries)
    first = report.entries[0]
    blocked_first = planner.TitleUpdatePlanEntry(
        **{**first.__dict__, "eligible_for_future_update": False, "blocking_reasons": ["x"]}
    )
    return planner.TitleUpdateDryRunReport(
        audit_timestamp=report.audit_timestamp,
        manifest_path=report.manifest_path,
        expected_target_count=report.expected_target_count,
        entries=[blocked_first, *report.entries[1:]],
        method_call_log=report.method_call_log,
        http_call_log=[],
    )


class TestManifestOptionRequired:
    def test_missing_manifest_option_fails(self) -> None:
        result = runner.invoke(cli.app, ["plan-title-updates", "--expected-count", "1"])
        assert result.exit_code != 0

    def test_missing_expected_count_option_fails(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, [_entry()])
        result = runner.invoke(
            cli.app, ["plan-title-updates", "--manifest", str(manifest_path)]
        )
        assert result.exit_code != 0


class TestManifestValidationErrors:
    def test_count_mismatch_fails_without_notion_call(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        called = {"value": False}
        monkeypatch.setattr(
            cli, "NotionClient", lambda api_key: called.__setitem__("value", True)
        )
        manifest_path = _write_manifest(tmp_path, [_entry()])

        result = runner.invoke(
            cli.app,
            ["plan-title-updates", "--manifest", str(manifest_path), "--expected-count", "7"],
        )

        assert result.exit_code == 1
        assert called["value"] is False  # Notionへ接続する前に失敗している

    def test_missing_file_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

        result = runner.invoke(
            cli.app,
            ["plan-title-updates", "--manifest", "does-not-exist.json", "--expected-count", "1"],
        )

        assert result.exit_code == 1

    def test_invalid_json_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        bad_path = tmp_path / "bad.json"
        bad_path.write_text("not json", encoding="utf-8")

        result = runner.invoke(
            cli.app,
            ["plan-title-updates", "--manifest", str(bad_path), "--expected-count", "1"],
        )

        assert result.exit_code == 1

    def test_missing_card_data_source_id_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))
        manifest_path = _write_manifest(tmp_path, [_entry()])

        result = runner.invoke(
            cli.app,
            ["plan-title-updates", "--manifest", str(manifest_path), "--expected-count", "1"],
        )

        assert result.exit_code == 1


class TestDryRunOutcomes:
    def test_all_eligible_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        entries = [_entry()]
        manifest_path = _write_manifest(tmp_path, entries)
        monkeypatch.setattr(
            cli,
            "build_title_update_dry_run_plan",
            lambda *a, **k: _eligible_report(entries),
        )

        result = runner.invoke(
            cli.app,
            [
                "plan-title-updates",
                "--manifest",
                str(manifest_path),
                "--expected-count",
                "1",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )

        assert result.exit_code == 0
        assert "適用可能: 1" in result.stdout

    def test_any_blocked_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        entries = [_entry(page_id="p1"), _entry(page_id="p2")]
        manifest_path = _write_manifest(tmp_path, entries)
        monkeypatch.setattr(
            cli,
            "build_title_update_dry_run_plan",
            lambda *a, **k: _blocked_report(entries),
        )

        result = runner.invoke(
            cli.app,
            [
                "plan-title-updates",
                "--manifest",
                str(manifest_path),
                "--expected-count",
                "2",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )

        assert result.exit_code == 1

    def test_json_report_is_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        entries = [_entry()]
        manifest_path = _write_manifest(tmp_path, entries)
        monkeypatch.setattr(
            cli,
            "build_title_update_dry_run_plan",
            lambda *a, **k: _eligible_report(entries),
        )
        out_dir = tmp_path / "out"

        result = runner.invoke(
            cli.app,
            [
                "plan-title-updates",
                "--manifest",
                str(manifest_path),
                "--expected-count",
                "1",
                "--output-dir",
                str(out_dir),
            ],
        )

        assert result.exit_code == 0
        json_files = list(out_dir.glob("dry-run-card-title-updates-*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["notion_write_operations"] == 0
        assert data["notion_write_attempts"] == 0

    def test_markdown_report_is_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
        _patch_notion(monkeypatch)
        entries = [_entry()]
        manifest_path = _write_manifest(tmp_path, entries)
        monkeypatch.setattr(
            cli,
            "build_title_update_dry_run_plan",
            lambda *a, **k: _eligible_report(entries),
        )
        out_dir = tmp_path / "out"

        result = runner.invoke(
            cli.app,
            [
                "plan-title-updates",
                "--manifest",
                str(manifest_path),
                "--expected-count",
                "1",
                "--output-dir",
                str(out_dir),
            ],
        )

        assert result.exit_code == 0
        md_files = list(out_dir.glob("dry-run-card-title-updates-*.md"))
        assert len(md_files) == 1


class TestHelpIsReadOnly:
    def test_help_states_read_only(self) -> None:
        result = runner.invoke(cli.app, ["plan-title-updates", "--help"])

        assert result.exit_code == 0
        plain = _plain_help_text(result.stdout)
        assert "読み取り専用" in plain
        assert "--manifest" in plain
        assert "--expected-count" in plain

    def test_apply_option_does_not_exist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--applyオプション自体が定義されていないことを、実際の起動失敗で確認する
        (ヘルプ文言の部分一致では「--applyは存在しないと説明する文」と区別できないため)。
        """
        monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

        result = runner.invoke(
            cli.app,
            ["plan-title-updates", "--manifest", "x.json", "--expected-count", "1", "--apply"],
        )

        assert result.exit_code != 0
        assert "no such option" in result.stdout.lower() or "unexpected" in result.stdout.lower()


class TestEntrypointReadOnlyGuard:
    def test_cli_command_body_has_no_write_capable_references(self) -> None:
        """plan-title-updatesコマンド本体(エントリーポイント)のAST内に、
        書き込み系の識別子が一切含まれないことを確認する。"""
        source = Path("src/mtg_notion_manager/cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_node = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "plan_title_updates_command"
        )
        names_in_function = {
            node.id for node in ast.walk(func_node) if isinstance(node, ast.Name)
        }
        attrs_in_function = {
            node.attr for node in ast.walk(func_node) if isinstance(node, ast.Attribute)
        }
        forbidden = {
            "update_page",
            "create_page",
            "update_data_source_schema",
            "DedupeRepository",
            "CardRepository",
            "NotionWriter",
            "execute_import_cards",
            "execute_article_import",
            "execute_dedupe_plan",
            "apply_dedupe_batch",
            "apply_price_link_targets",
        }
        assert not (names_in_function & forbidden)
        assert not (attrs_in_function & forbidden)
