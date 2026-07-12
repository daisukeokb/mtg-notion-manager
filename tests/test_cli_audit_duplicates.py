from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.services.audit_duplicates import AuditReportPaths, GroupAudit

runner = CliRunner()


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


def _sample_audits() -> list[GroupAudit]:
    return [
        GroupAudit(
            card_name="沼",
            pages=[{"id": "p1"}, {"id": "p2"}],
            category="auto",
            recommended_representative_id="p1",
            representative_reasons=["英語名あり"],
            conflicts=[],
            special_version_flags=[],
            price_link_differs=False,
            merged_deck_relation_count=1,
            estimated_quantity=2,
            risks=[],
            recommended_action="dedupe-cards --card-name で自動統合可能",
        )
    ]


def test_audit_writes_reports_and_does_not_call_notion_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())
    monkeypatch.setattr(cli, "load_intentional_duplicates", lambda: object())

    captured: dict[str, object] = {}

    def fake_audit(
        repo: object,
        card_name: str | None = None,
        exclusions: object = None,
        intentional_duplicates: object = None,
    ) -> list:
        captured["card_name"] = card_name
        return _sample_audits()

    monkeypatch.setattr(cli, "audit_duplicate_groups", fake_audit)

    write_calls: list[tuple] = []

    def fake_write(
        audits: list, output_dir: Path, timestamp: str | None = None
    ) -> AuditReportPaths:
        write_calls.append((audits, output_dir))
        return AuditReportPaths(
            json_path=output_dir / "a.json",
            csv_path=output_dir / "a.csv",
            markdown_path=output_dir / "a.md",
        )

    monkeypatch.setattr(cli, "write_audit_reports", fake_write)

    result = runner.invoke(cli.app, ["audit-duplicates", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert len(write_calls) == 1
    assert "自動統合可能: 1" in result.stdout


def test_audit_with_card_name_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())
    monkeypatch.setattr(cli, "load_intentional_duplicates", lambda: object())

    captured: dict[str, object] = {}

    def fake_audit(
        repo: object,
        card_name: str | None = None,
        exclusions: object = None,
        intentional_duplicates: object = None,
    ) -> list:
        captured["card_name"] = card_name
        return []

    monkeypatch.setattr(cli, "audit_duplicate_groups", fake_audit)
    monkeypatch.setattr(
        cli,
        "write_audit_reports",
        lambda audits, output_dir, timestamp=None: AuditReportPaths(
            json_path=output_dir / "a.json",
            csv_path=output_dir / "a.csv",
            markdown_path=output_dir / "a.md",
        ),
    )

    result = runner.invoke(
        cli.app,
        ["audit-duplicates", "--card-name", "血染めのぬかるみ", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert captured["card_name"] == "血染めのぬかるみ"


def test_missing_card_data_source_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, ["audit-duplicates"])

    assert result.exit_code == 1


def test_audit_command_never_touches_notion_client_write_methods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLIレベルでも、Notionクライアントの更新系メソッドが呼ばれる余地がないことを確認する。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    class StrictFakeClient:
        def __enter__(self) -> StrictFakeClient:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def update_page(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("update_page must not be called by audit-duplicates")

        def update_data_source_schema(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("update_data_source_schema must not be called")

    monkeypatch.setattr(cli, "NotionClient", lambda api_key: StrictFakeClient())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())
    monkeypatch.setattr(cli, "load_intentional_duplicates", lambda: object())
    def fake_audit_empty(
        repo: object,
        card_name: object = None,
        exclusions: object = None,
        intentional_duplicates: object = None,
    ) -> list:
        return []

    monkeypatch.setattr(cli, "audit_duplicate_groups", fake_audit_empty)
    monkeypatch.setattr(
        cli,
        "write_audit_reports",
        lambda audits, output_dir, timestamp=None: AuditReportPaths(
            json_path=output_dir / "a.json",
            csv_path=output_dir / "a.csv",
            markdown_path=output_dir / "a.md",
        ),
    )

    result = runner.invoke(cli.app, ["audit-duplicates", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0


def test_intentional_duplicate_count_and_detail_are_shown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: object())
    monkeypatch.setattr(cli, "load_exclusions", lambda: object())
    monkeypatch.setattr(cli, "load_intentional_duplicates", lambda: object())

    intentional_audit = GroupAudit(
        card_name="苦渋の破棄",
        pages=[
            {
                "id": "78a2b136-bef4-487a-9b46-ec08bdf8d4cb",
                "properties": {
                    "英語名": {"rich_text": [{"plain_text": "Anguished Unmaking"}]},
                },
            },
            {"id": "28ef458e-b1f4-4226-98d8-cc6c3c144d2a", "properties": {}},
        ],
        category="intentional_duplicates",
        recommended_representative_id=None,
        representative_reasons=[],
        conflicts=[],
        special_version_flags=[],
        price_link_differs=False,
        merged_deck_relation_count=0,
        estimated_quantity=2,
        risks=[],
        recommended_action="意図的に別レコードとして保持されています",
        intentional_duplicate_reason="通常版とショーケース版を別レコードとして保持する",
    )

    monkeypatch.setattr(
        cli,
        "audit_duplicate_groups",
        lambda repo, card_name=None, exclusions=None, intentional_duplicates=None: [
            intentional_audit
        ],
    )
    monkeypatch.setattr(
        cli,
        "write_audit_reports",
        lambda audits, output_dir, timestamp=None: AuditReportPaths(
            json_path=output_dir / "a.json",
            csv_path=output_dir / "a.csv",
            markdown_path=output_dir / "a.md",
        ),
    )

    result = runner.invoke(cli.app, ["audit-duplicates", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "意図的に保持する重複: 1グループ" in result.stdout
    assert "要確認: 0" in result.stdout
    assert "苦渋の破棄 / Anguished Unmaking" in result.stdout
    assert "ページ数: 2" in result.stdout
    assert "理由: 通常版とショーケース版を別レコードとして保持する" in result.stdout
    assert "状態: intentional_duplicate" in result.stdout
