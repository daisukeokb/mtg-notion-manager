from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.services.apply_price_link_dedupe import ApplyLogPaths, GroupApplyOutcome

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


def _target_item(
    card_name: str,
    page_ids: list[str],
    category: str = "price_only",
    prices: list[float] | None = None,
) -> dict:
    return {
        "card_name": card_name,
        "review_category": category,
        "duplicate_count": len(page_ids),
        "prices": prices or [100, 200],
        "links": [],
        "merged_deck_relation_count": 0,
        "pages": [{"page_id": pid} for pid in page_ids],
    }


def _write_targets_report(tmp_path: Path) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            [
                # 沼: 2ページ・価格差のみ → カナリア選定対象。
                _target_item("沼", ["p1", "p2"]),
                # 秘薬: 3ページ構成にしてカナリア(2ページ限定)から除外し、
                # remainingスコープのテストで確実に対象として残るようにする。
                _target_item("秘薬", ["p5", "p6", "p7"]),
                _target_item("血染めのぬかるみ", ["p3", "p4"], category="manual_representative"),
            ]
        ),
        encoding="utf-8",
    )
    return path


def _patch_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())


def test_rejects_unknown_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    report_path = _write_targets_report(tmp_path)

    result = runner.invoke(
        cli.app,
        ["apply-price-link-dedupe", "--targets-report", str(report_path), "--scope", "bogus"],
    )

    assert result.exit_code == 1


def test_manual_scope_requires_representative_page_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    report_path = _write_targets_report(tmp_path)

    result = runner.invoke(
        cli.app,
        ["apply-price-link-dedupe", "--targets-report", str(report_path), "--scope", "manual"],
    )

    assert result.exit_code == 1


def test_missing_report_file_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(
        cli.app,
        ["apply-price-link-dedupe", "--targets-report", str(tmp_path / "missing.json")],
    )

    assert result.exit_code == 1


def test_dry_run_lists_remaining_scope_and_does_not_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_targets_report(tmp_path)

    outcome = GroupApplyOutcome(
        card_name="秘薬",
        status="planned",
        representative_page_id="p5",
        merged_page_ids=["p6", "p7"],
    )
    monkeypatch.setattr(
        cli, "apply_price_link_targets", lambda repo, targets, apply, exclusions=None: [outcome]
    )
    monkeypatch.setattr(
        cli,
        "write_price_link_apply_log",
        lambda outcomes, targets_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "remaining",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "秘薬" in result.stdout
    assert "沼" not in result.stdout  # 沼はカナリア対象のためremainingには含まれない
    assert "血染めのぬかるみ" not in result.stdout  # remainingスコープはprice_onlyのみ


def test_manual_scope_targets_only_manual_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_targets_report(tmp_path)

    captured: dict[str, object] = {}

    def fake_apply(repo: object, targets: list, apply: bool, exclusions: object = None) -> list:
        captured["targets"] = targets
        return [
            GroupApplyOutcome(
                card_name=t.card_name,
                status="applied",
                representative_page_id=t.representative_page_id,
            )
            for t in targets
        ]

    monkeypatch.setattr(cli, "apply_price_link_targets", fake_apply)
    monkeypatch.setattr(
        cli,
        "write_price_link_apply_log",
        lambda outcomes, targets_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "manual",
            "--manual-representative-page-id",
            "p3",
            "--apply",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    targets = captured["targets"]
    assert len(targets) == 1
    assert targets[0].card_name == "血染めのぬかるみ"
    assert targets[0].representative_page_id == "p3"


def test_apply_writes_log_and_reports_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_targets_report(tmp_path)

    outcome = GroupApplyOutcome(
        card_name="沼", status="applied", representative_page_id="p1", merged_page_ids=["p2"]
    )
    captured_apply_flag: dict[str, bool] = {}

    def fake_apply(repo: object, targets: list, apply: bool, exclusions: object = None) -> list:
        captured_apply_flag["apply"] = apply
        return [outcome]

    monkeypatch.setattr(cli, "apply_price_link_targets", fake_apply)
    monkeypatch.setattr(
        cli,
        "write_price_link_apply_log",
        lambda outcomes, targets_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "canary",
            "--apply",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured_apply_flag["apply"] is True
    assert "適用: 1件" in result.stdout


def test_dry_run_flag_overrides_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_notion(monkeypatch)
    report_path = _write_targets_report(tmp_path)

    captured_apply_flag: dict[str, bool] = {}

    def fake_apply(repo: object, targets: list, apply: bool, exclusions: object = None) -> list:
        captured_apply_flag["apply"] = apply
        return []

    monkeypatch.setattr(cli, "apply_price_link_targets", fake_apply)
    monkeypatch.setattr(
        cli,
        "write_price_link_apply_log",
        lambda outcomes, targets_report_path, output_dir, applied, timestamp=None: ApplyLogPaths(
            json_path=Path(output_dir) / "log.json"
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "apply-price-link-dedupe",
            "--targets-report",
            str(report_path),
            "--scope",
            "remaining",
            "--apply",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured_apply_flag["apply"] is False


def test_missing_card_data_source_id_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_config_without_card_db() -> Config:
        return Config(
            notion_api_key="secret_test",
            commander_data_source_id="commander-ds-id",
            card_data_source_id=None,
        )

    monkeypatch.setattr(cli.Config, "load", staticmethod(fake_config_without_card_db))
    report_path = _write_targets_report(tmp_path)

    result = runner.invoke(
        cli.app, ["apply-price-link-dedupe", "--targets-report", str(report_path)]
    )

    assert result.exit_code == 1
