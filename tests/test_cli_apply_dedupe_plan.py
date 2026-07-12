from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.services.apply_dedupe_plan import ApplyLogPaths, GroupApplyOutcome

runner = CliRunner()


def _fake_config() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id="card-ds-id",
    )


class FakeNotionClientCtx:
    def __enter__(self) -> FakeNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _report_item(card_name: str, page_ids: list[str], category: str = "auto") -> dict:
    return {
        "card_name": card_name,
        "category": category,
        "duplicate_count": len(page_ids),
        "recommended_representative_id": page_ids[0],
        "merged_deck_relation_count": 0,
        "pages": [{"page_id": pid} for pid in page_ids],
    }


def _write_report(tmp_path: Path) -> Path:
    path = tmp_path / "audit.json"
    path.write_text(
        json.dumps(
            [
                _report_item("沼", ["p1", "p2"]),
                _report_item("秘儀の印鑑", ["p3", "p4"], category="needs_review"),
            ]
        ),
        encoding="utf-8",
    )
    return path


def _patch_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())


def test_rejects_non_auto_classification(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    report_path = _write_report(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "apply-dedupe-plan",
            "--audit-report",
            str(report_path),
            "--classification",
            "needs_review",
        ],
    )

    assert result.exit_code == 1


def test_missing_report_file_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(
        cli.app,
        ["apply-dedupe-plan", "--audit-report", str(tmp_path / "missing.json")],
    )

    assert result.exit_code == 1


def test_dry_run_lists_targets_and_does_not_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_report(tmp_path)

    outcome = GroupApplyOutcome(
        card_name="沼", status="planned", representative_page_id="p2", merged_page_ids=["p1"]
    )
    monkeypatch.setattr(
        cli, "apply_dedupe_batch", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    monkeypatch.setattr(
        cli,
        "write_apply_log",
        lambda outcomes, audit_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-dedupe-plan",
            "--audit-report",
            str(report_path),
            "--limit",
            "10",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "沼" in result.stdout
    assert "秘儀の印鑑" not in result.stdout  # needs_reviewは対象外


def test_apply_writes_log_and_reports_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_report(tmp_path)

    outcome = GroupApplyOutcome(
        card_name="沼", status="applied", representative_page_id="p2", merged_page_ids=["p1"]
    )
    captured_apply_flag: dict[str, bool] = {}

    def fake_batch(repo: object, targets: list, apply: bool, exclusions: object = None) -> list:
        captured_apply_flag["apply"] = apply
        return [outcome]

    monkeypatch.setattr(cli, "apply_dedupe_batch", fake_batch)
    monkeypatch.setattr(
        cli,
        "write_apply_log",
        lambda outcomes, audit_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-dedupe-plan",
            "--audit-report",
            str(report_path),
            "--apply",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured_apply_flag["apply"] is True
    assert "適用: 1件" in result.stdout


def test_dry_run_flag_overrides_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--apply と --dry-run が両方指定された場合、dry-runが優先される。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_report(tmp_path)

    captured_apply_flag: dict[str, bool] = {}

    def fake_batch(repo: object, targets: list, apply: bool, exclusions: object = None) -> list:
        captured_apply_flag["apply"] = apply
        return []

    monkeypatch.setattr(cli, "apply_dedupe_batch", fake_batch)
    monkeypatch.setattr(
        cli,
        "write_apply_log",
        lambda outcomes, audit_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-dedupe-plan",
            "--audit-report",
            str(report_path),
            "--apply",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured_apply_flag["apply"] is False
