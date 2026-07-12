from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.services.dedupe_cards import (
    DedupeApplyResult,
    DedupePlan,
    DuplicateGroup,
    GroupApplyResult,
    GroupError,
    MergePlan,
    RepresentativeChoice,
)

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


def _page(page_id: str) -> dict:
    return {"id": page_id, "url": f"https://notion.so/{page_id}", "properties": {}}


def _sample_plan(missing_schema: list[str] | None = None) -> DedupePlan:
    group = DuplicateGroup(card_name="沼", pages=[_page("p1"), _page("p2")])
    representative = RepresentativeChoice(page=_page("p1"), reasons=["英語名あり"])
    merge_plan = MergePlan(
        group=group,
        representative=representative,
        merged_deck_relation_ids=["d1"],
        owned=True,
        quantity=2,
        english_name="Swamp",
        single_valued_attributes={},
        multi_valued_attributes={},
        duplicate_pages=[_page("p2")],
    )
    return DedupePlan(
        merge_plans=[merge_plan], group_errors=[], schema_missing_properties=missing_schema or []
    )


class FakeNotionClientCtx:
    def __enter__(self) -> FakeNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeDedupeRepo:
    def __init__(self, missing_schema: list[str] | None = None) -> None:
        self._missing_schema = missing_schema or []

    def missing_schema_properties(self) -> list[str]:
        return self._missing_schema

    def apply_schema_migration(self, property_names: list[str]) -> dict:
        return {}


def _patch_repo(monkeypatch: pytest.MonkeyPatch, missing_schema: list[str] | None = None) -> None:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    monkeypatch.setattr(
        cli, "DedupeRepository", lambda client, data_source_id: FakeDedupeRepo(missing_schema)
    )


def test_dry_run_shows_plan_and_does_not_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_dedupe_plan", lambda plan, repo: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--dry-run"])

    assert result.exit_code == 0
    assert executed["value"] is False
    assert "沼" in result.stdout


def test_without_apply_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_dedupe_plan", lambda plan, repo: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼"])

    assert result.exit_code == 0
    assert executed["value"] is False


def test_apply_with_card_name_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    apply_result = DedupeApplyResult(
        results=[
            GroupApplyResult(
                card_name="沼",
                representative_page_id="p1",
                representative_updated=True,
                duplicate_page_ids_marked=["p2"],
            )
        ]
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: apply_result)

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply"])

    assert result.exit_code == 0
    assert "成功: 1件" in result.stdout


def test_apply_without_card_name_requires_apply_all_and_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)

    result = runner.invoke(cli.app, ["dedupe-cards", "--apply"])

    assert result.exit_code == 1


def test_apply_all_with_yes_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    apply_result = DedupeApplyResult(
        results=[
            GroupApplyResult(
                card_name="沼", representative_page_id="p1", representative_updated=True
            )
        ]
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: apply_result)

    result = runner.invoke(cli.app, ["dedupe-cards", "--apply", "--apply-all", "--yes"])

    assert result.exit_code == 0


def test_missing_schema_blocks_apply_without_apply_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(
            missing_schema=["所持枚数", "統合済み"]
        ),
    )
    executed = {"value": False}
    monkeypatch.setattr(
        cli, "execute_dedupe_plan", lambda plan, repo: executed.__setitem__("value", True)
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply"])

    assert result.exit_code == 1
    assert executed["value"] is False


def test_representative_page_id_requires_card_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result = runner.invoke(cli.app, ["dedupe-cards", "--representative-page-id", "p1", "--dry-run"])

    assert result.exit_code == 1


def test_missing_card_data_source_id_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, ["dedupe-cards", "--dry-run"])

    assert result.exit_code == 1


def test_group_errors_are_shown_in_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)
    plan = DedupePlan(
        merge_plans=[],
        group_errors=[
            GroupError(
                card_name="秘儀の印鑑",
                pages=[_page("p1"), _page("p2")],
                error_type="conflict",
                message="英語名が競合しています",
            )
        ],
        schema_missing_properties=[],
    )
    monkeypatch.setattr(
        cli, "build_dedupe_plan", lambda repo, card_name=None, representative_page_id=None: plan
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "秘儀の印鑑", "--dry-run"])

    assert result.exit_code == 0
    assert "秘儀の印鑑" in result.stdout
