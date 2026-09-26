from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.error_contract import SCHEMA_VERSION, ErrorCategory, ErrorCode
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.services.dedupe_cards import (
    DedupeApplyResult,
    DedupePlan,
    DuplicateGroup,
    GroupApplyResult,
    GroupError,
    MergePlan,
    RepresentativeChoice,
)
from mtg_notion_manager.services.dedupe_schema import (
    SchemaMigrationExecutionError,
    SchemaMigrationResult,
    SchemaWriteCompletion,
)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

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
        #: apply_schema_migration()が実際に呼ばれた回数・引数を記録する
        #: (Phase 2N: --dry-run が --apply-schema のNotion書き込みを確実に
        #: 抑制することを、結果の観測ではなく呼び出し回数そのもので固定する)。
        self.apply_schema_migration_calls: list[list[str]] = []

    def missing_schema_properties(self) -> list[str]:
        return self._missing_schema

    def apply_schema_migration(self, property_names: list[str]) -> dict:
        self.apply_schema_migration_calls.append(list(property_names))
        return {}


def _patch_repo(
    monkeypatch: pytest.MonkeyPatch, missing_schema: list[str] | None = None
) -> FakeDedupeRepo:
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: FakeNotionClientCtx())
    fake_repo = FakeDedupeRepo(missing_schema)
    monkeypatch.setattr(cli, "DedupeRepository", lambda client, data_source_id: fake_repo)
    return fake_repo


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


def _regression_page(
    page_id: str, name: str, english_name: str | None = None
) -> dict:
    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name}]},
        "所持": {"type": "checkbox", "checkbox": False},
        "統合済み": {"type": "checkbox", "checkbox": False},
        "採用デッキ": {
            "type": "relation",
            "id": f"rel-{page_id}",
            "relation": [],
            "has_more": False,
        },
        "メモ": {"type": "rich_text", "rich_text": []},
    }
    if english_name is not None:
        properties["英語名"] = {"type": "rich_text", "rich_text": [{"plain_text": english_name}]}
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": "2024-01-01T00:00:00.000Z",
        "last_edited_time": "2024-01-01T00:00:00.000Z",
        "properties": properties,
    }


class PartialFailureNotionClientCtx:
    """dedupe-cards --apply の実際の実行(execute_dedupe_plan()のreal実装を通す)で、
    代表更新・重複ページ1件目のmarkは成功するが、2件目のmarkでNotionAPIErrorが
    発生するシナリオを模す。

    Phase 2Iのresult-fidelity修正(GroupApplyResult.duplicate_page_ids_marked)が
    実際にCLI表示へ反映されることの回帰ロック。CLI production code
    (cli.py/preview.py)は一切変更しない。
    """

    def __init__(self, pages: list[dict], fail_on_page_id: str) -> None:
        self._pages = pages
        self._fail_on_page_id = fail_on_page_id
        self.updated_pages: list[tuple[str, dict]] = []

    def __enter__(self) -> PartialFailureNotionClientCtx:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self._pages

    def get_data_source(self, data_source_id: str) -> dict:
        return {"properties": {"所持枚数": {"type": "number"}, "統合済み": {"type": "checkbox"}}}

    def update_page(self, page_id: str, properties: dict) -> dict:
        if page_id == self._fail_on_page_id:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました: {page_id}")
        self.updated_pages.append((page_id, properties))
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        return []


def test_partial_failure_shows_accurate_completed_mark_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 2Iのresult-fidelity修正の回帰ロック(§31)。

    代表更新 + 重複ページp2のmarkが実際に成功した後、p3のmarkでNotionAPIErrorが
    発生しても、「統合済み設定件数」列には人工的な0ではなく実際に成功した件数
    (1件)が表示される。これは新しいbehavior追加ではなく、Phase 2Iで既に
    成立した正しいobserved behaviorを固定するテストであり、CLI production code
    は一切変更していない。
    """
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    pages = [
        _regression_page("p1", "沼", english_name="Swamp"),
        _regression_page("p2", "沼"),
        _regression_page("p3", "沼"),
    ]
    client_ctx = PartialFailureNotionClientCtx(pages, fail_on_page_id="p3")
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: client_ctx)

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply"])

    assert result.exit_code == 1
    # 実際にNotionへ書き込みが成功したのはp1(代表)とp2(1件目の重複)だけ。
    updated_ids = {page_id for page_id, _ in client_ctx.updated_pages}
    assert updated_ids == {"p1", "p2"}
    # 「統合済み設定件数」列は実際の成功件数(1件)を表示する(人工的な0ではない)。
    plain_stdout = result.stdout.replace("\n", "")
    assert "1" in plain_stdout
    assert "失敗: 1件" in result.stdout


# --- Phase 2N: --dry-run / --apply-schema safety repair ------------------
#
# Phase 2Mのread-only auditで発見された既存の安全契約違反(--dry-run が
# --apply-schema のNotion書き込みを抑制しない)を修正する回帰テスト群。
# T1〜T9(Work Unit §22-30)に対応する。


def test_t1_apply_schema_only_calls_migration_and_skips_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply-schema"])

    assert result.exit_code == 0
    assert len(fake_repo.apply_schema_migration_calls) == 1
    assert execute_calls["count"] == 0


def test_t2_dry_run_apply_schema_suppresses_schema_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最重要regression: --dry-run --apply-schema はschema書き込みを一切行わない。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply-schema"]
    )

    assert result.exit_code == 0
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0
    assert "所持枚数" in result.stdout  # schema preview は維持される
    assert "沼" in result.stdout  # dedupe preview も維持される


def test_t3_dry_run_with_both_apply_flags_suppresses_all_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply", "--apply-schema"],
    )

    assert result.exit_code == 0
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0


def test_t4_apply_with_apply_schema_runs_schema_before_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )

    call_order: list[str] = []
    original_apply_schema_migration = fake_repo.apply_schema_migration

    def _tracking_apply_schema_migration(property_names: list[str]) -> dict:
        call_order.append("schema")
        return original_apply_schema_migration(property_names)

    fake_repo.apply_schema_migration = _tracking_apply_schema_migration  # type: ignore[method-assign]

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

    def _tracking_execute_dedupe_plan(plan: DedupePlan, repo: object) -> DedupeApplyResult:
        call_order.append("dedupe")
        return apply_result

    monkeypatch.setattr(cli, "execute_dedupe_plan", _tracking_execute_dedupe_plan)

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema"]
    )

    assert result.exit_code == 0
    assert len(fake_repo.apply_schema_migration_calls) == 1
    assert call_order == ["schema", "dedupe"]


def test_t5_apply_only_with_missing_schema_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(
            missing_schema=["所持枚数", "統合済み"]
        ),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply"])

    assert result.exit_code == 1
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0


def test_t6_dry_run_apply_schema_with_no_missing_schema_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=[])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply-schema"]
    )

    assert result.exit_code == 0
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0


def test_t7_dry_run_message_does_not_claim_schema_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: None)

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply-schema"]
    )

    assert result.exit_code == 0
    assert "スキーマに追加しました" not in result.stdout
    assert "Notionへの書き込みは行いません" in result.stdout


def test_t8_apply_schema_only_message_does_not_deny_schema_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: None)

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply-schema"])

    assert result.exit_code == 0
    assert "スキーマに追加しました" in result.stdout
    assert "Notionへの書き込みは行いません。" not in result.stdout
    assert "重複統合の書き込みは行いません" in result.stdout


@pytest.mark.parametrize("apply_flag", [False, True])
@pytest.mark.parametrize("apply_schema_flag", [False, True])
def test_t9_dry_run_dominates_all_apply_flag_combinations(
    monkeypatch: pytest.MonkeyPatch, apply_flag: bool, apply_schema_flag: bool
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    args = ["dedupe-cards", "--card-name", "沼", "--dry-run"]
    if apply_flag:
        args.append("--apply")
    if apply_schema_flag:
        args.append("--apply-schema")

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0


# --- Phase 2O: schema mutation result fidelity (CLI integration) --------
#
# services/dedupe_schema.pyの単体テスト(SUCCEEDED/KNOWN_FAILED/UNKNOWNの
# 分類そのもの)はtests/test_dedupe_schema.pyにある。ここでは
# cli.execute_schema_migration という統合ポイントを経由したCLI挙動
# (fail-closedの維持・schema→dedupeの順序・dry-run regressionの再固定)
# だけを検証する。


def test_t7_schema_failure_prevents_dedupe_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])

    def _raise_schema_failure(repo: object, property_names: list[str]) -> SchemaMigrationResult:
        raise SchemaMigrationExecutionError(
            SchemaMigrationResult(
                completion=SchemaWriteCompletion.KNOWN_FAILED,
                property_names=tuple(property_names),
            ),
            "Notion API呼び出しに失敗しました (400): bad request",
        )

    monkeypatch.setattr(cli, "execute_schema_migration", _raise_schema_failure)

    build_calls = {"count": 0}

    def _tracking_build_dedupe_plan(
        repo: object, card_name: str | None = None, representative_page_id: str | None = None
    ) -> DedupePlan:
        build_calls["count"] += 1
        return _sample_plan()

    monkeypatch.setattr(cli, "build_dedupe_plan", _tracking_build_dedupe_plan)

    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema"]
    )

    assert result.exit_code == 1
    # schema失敗時、build_dedupe_plan()自体が呼ばれる前に例外が伝播する
    # (schema phaseがbuild_dedupe_plan()より前に配置されているため)。
    assert build_calls["count"] == 0
    assert execute_calls["count"] == 0
    assert "bad request" in result.stdout


def test_t8_schema_success_then_dedupe_runs_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )

    call_order: list[str] = []
    schema_result = SchemaMigrationResult(
        completion=SchemaWriteCompletion.SUCCEEDED,
        property_names=("所持枚数", "統合済み"),
    )

    def _tracking_execute_schema_migration(
        repo: object, property_names: list[str]
    ) -> SchemaMigrationResult:
        call_order.append("schema")
        return schema_result

    monkeypatch.setattr(cli, "execute_schema_migration", _tracking_execute_schema_migration)

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

    def _tracking_execute_dedupe_plan(plan: DedupePlan, repo: object) -> DedupeApplyResult:
        call_order.append("dedupe")
        return apply_result

    monkeypatch.setattr(cli, "execute_dedupe_plan", _tracking_execute_dedupe_plan)

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema"]
    )

    assert result.exit_code == 0
    assert call_order == ["schema", "dedupe"]


def test_t9_apply_schema_only_generates_success_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )

    schema_calls = {"count": 0}

    def _tracking_execute_schema_migration(
        repo: object, property_names: list[str]
    ) -> SchemaMigrationResult:
        schema_calls["count"] += 1
        return SchemaMigrationResult(
            completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(property_names)
        )

    monkeypatch.setattr(cli, "execute_schema_migration", _tracking_execute_schema_migration)

    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(cli.app, ["dedupe-cards", "--card-name", "沼", "--apply-schema"])

    assert result.exit_code == 0
    assert schema_calls["count"] == 1
    assert execute_calls["count"] == 0


def test_t10_dry_run_never_calls_execute_schema_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 2Nのdry-run safety regressionを、新しい統合ポイント
    (cli.execute_schema_migration)側からも再固定する。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    schema_calls = {"count": 0}

    def _tracking_execute_schema_migration(
        repo: object, property_names: list[str]
    ) -> SchemaMigrationResult:
        schema_calls["count"] += 1
        return SchemaMigrationResult(
            completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(property_names)
        )

    monkeypatch.setattr(cli, "execute_schema_migration", _tracking_execute_schema_migration)

    result_a = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply-schema"]
    )
    result_b = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply", "--apply-schema"],
    )

    assert result_a.exit_code == 0
    assert result_b.exit_code == 0
    assert schema_calls["count"] == 0


# --- Phase 2P: --error-json + schema/dedupe aggregate MutationSummary ----
#
# repo.missing_schema_properties()等をdedupe-cards CLIが直接呼ぶ構造上、
# test_cli_error_contract.pyの_patch_notion()(NotionClient/CardRepository/
# NotionWriterのみpatch)とは噛み合わないため、既にこのファイルで確立
# されている_patch_repo()/FakeDedupeRepoをそのまま再利用する
# (この配置は意図的な判断であり、Final Reportで開示済み)。


def _assert_pure_json_error(stdout: str, *, command: str, category: str, code: str) -> dict:
    assert "\x1b" not in stdout, "stdoutにANSIエスケープが含まれてはならない"
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == command
    assert payload["error_category"] == category
    assert payload["error_code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    assert "mutation" not in payload
    return payload


def _assert_pure_json_v2_mutation_error(
    stdout: str, *, command: str, category: str, code: str
) -> dict:
    assert "\x1b" not in stdout, "stdoutにANSIエスケープが含まれてはならない"
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    assert payload["schema_version"] == 2
    assert payload["command"] == command
    assert payload["error_category"] == category
    assert payload["error_code"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    assert "mutation" in payload
    return payload


def _schema_failure(
    completion: str, *, message: str = "Notion API呼び出しに失敗しました (400): bad request"
) -> SchemaMigrationExecutionError:
    return SchemaMigrationExecutionError(
        SchemaMigrationResult(completion=completion, property_names=("所持枚数", "統合済み")),
        message,
    )


# T1 -----------------------------------------------------------------------


def test_p2p_t1_help_mentions_error_json() -> None:
    result = runner.invoke(cli.app, ["dedupe-cards", "--help"])

    assert result.exit_code == 0
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout).replace("\n", "")
    assert "--error-json" in plain


# T2/T7 ----------------------------------------------------------------------


def test_p2p_t2_success_matches_human_output_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
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
    args = ["dedupe-cards", "--card-name", "沼", "--apply"]

    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 0
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


def test_p2p_t7_schema_success_then_dedupe_success_ordering_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    call_order: list[str] = []

    def _tracking_execute_schema_migration(
        repo: object, property_names: list[str]
    ) -> SchemaMigrationResult:
        call_order.append("schema")
        return SchemaMigrationResult(
            completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(property_names)
        )

    monkeypatch.setattr(cli, "execute_schema_migration", _tracking_execute_schema_migration)
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

    def _tracking_execute_dedupe_plan(plan: DedupePlan, repo: object) -> DedupeApplyResult:
        call_order.append("dedupe")
        return apply_result

    monkeypatch.setattr(cli, "execute_dedupe_plan", _tracking_execute_dedupe_plan)

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema", "--error-json"],
    )

    assert result.exit_code == 0
    assert call_order == ["schema", "dedupe"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


# T3 -------------------------------------------------------------------------


def test_p2p_t3_config_error_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config() -> Config:
        from mtg_notion_manager.config import ConfigError

        raise ConfigError("APIキーが未設定です")

    monkeypatch.setattr(cli.Config, "load", staticmethod(_raise_config))

    result = runner.invoke(cli.app, ["dedupe-cards", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_p2p_t3_missing_card_data_source_id_is_pure_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config_without_card_db))

    result = runner.invoke(cli.app, ["dedupe-cards", "--error-json"])

    assert result.exit_code == 1
    _assert_pure_json_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.CONFIGURATION,
        code=ErrorCode.CONFIG_LOAD_FAILED,
    )


def test_p2p_t3_cli_usage_errors_stay_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """representative_page_id/card_name・apply/apply_all/yesの誤りはCLIの使い方の
    誤りでありError Contract対象外(既存precedent、§20/§21)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))

    result_a = runner.invoke(
        cli.app,
        ["dedupe-cards", "--representative-page-id", "p1", "--dry-run", "--error-json"],
    )
    result_b = runner.invoke(cli.app, ["dedupe-cards", "--apply", "--error-json"])

    for result in (result_a, result_b):
        assert result.exit_code == 1
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)
        assert "エラー" in result.stdout


def test_p2p_t3_missing_schema_blocks_apply_stays_human_but_preview_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--apply-schema省略時のschema不足はCLIの使い方の誤りとして対象外(§20/§21)。
    ただしerror_jsonバッファに溜まっていたpreview出力は、human modeと同じ内容
    としてそのまま可視化する(§13/§22)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(
            missing_schema=["所持枚数", "統合済み"]
        ),
    )
    args = ["dedupe-cards", "--card-name", "沼", "--apply"]

    human = runner.invoke(cli.app, args)
    structured = runner.invoke(cli.app, [*args, "--error-json"])

    assert human.exit_code == structured.exit_code == 1
    assert human.stdout == structured.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(structured.stdout)


# T4/T5/T6/T17 ----------------------------------------------------------------


def test_p2p_t4_schema_known_failure_error_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli, "execute_schema_migration", lambda repo, names: (_ for _ in ()).throw(
            _schema_failure(SchemaWriteCompletion.KNOWN_FAILED)
        )
    )
    build_calls = {"count": 0}

    def _tracking_build_dedupe_plan(
        repo: object, card_name: str | None = None, representative_page_id: str | None = None
    ) -> DedupePlan:
        build_calls["count"] += 1
        return _sample_plan()

    monkeypatch.setattr(cli, "build_dedupe_plan", _tracking_build_dedupe_plan)
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema", "--error-json"],
    )

    assert result.exit_code == 1
    assert build_calls["count"] == 0
    assert execute_calls["count"] == 0
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_FAILED"
    assert mutation["attempted"] == 1
    assert mutation["succeeded"] == 0
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"
    assert mutation["operations"] == [
        {"key": "所持枚数、統合済み", "action": "schema_update", "state": "failed"}
    ]


def test_p2p_t5_schema_unknown_error_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli, "execute_schema_migration", lambda repo, names: (_ for _ in ()).throw(
            _schema_failure(SchemaWriteCompletion.UNKNOWN)
        )
    )
    build_calls = {"count": 0}

    def _tracking_build_dedupe_plan(
        repo: object, card_name: str | None = None, representative_page_id: str | None = None
    ) -> DedupePlan:
        build_calls["count"] += 1
        return _sample_plan()

    monkeypatch.setattr(cli, "build_dedupe_plan", _tracking_build_dedupe_plan)
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema", "--error-json"],
    )

    assert result.exit_code == 1
    assert build_calls["count"] == 0
    assert execute_calls["count"] == 0
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["attempted"] == 1
    assert mutation["succeeded"] == 0
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 1
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"


def test_p2p_t6_schema_classification_is_message_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """messageが"timeout"を含んでいても、実際のcompletionがKNOWN_FAILEDなら
    mutation.failed(unknownではなく)へ分類される(str(exc)を一切見ない)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "execute_schema_migration",
        lambda repo, names: (_ for _ in ()).throw(
            _schema_failure(
                SchemaWriteCompletion.KNOWN_FAILED,
                message="a timeout-looking message but completion is actually KNOWN_FAILED",
            )
        ),
    )

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema", "--error-json"],
    )

    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    assert payload["mutation"]["failed"] == 1
    assert payload["mutation"]["unknown"] == 0


# T8/T9 ------------------------------------------------------------------------


def test_p2p_t8_schema_success_then_dedupe_known_failure_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    monkeypatch.setattr(
        cli,
        "execute_schema_migration",
        lambda repo, names: SchemaMigrationResult(
            completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(names)
        ),
    )
    apply_result = DedupeApplyResult(
        results=[
            GroupApplyResult(
                card_name="沼",
                representative_page_id="p1",
                representative_updated=True,
                duplicate_page_ids_marked=[],
                error="Notion API呼び出しに失敗しました: 沼",
                failed_operation="mark_merged",
                failed_completion="known_failed",
            )
        ]
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: apply_result)

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema", "--error-json"],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    # schema: succeeded=1。dedupe: 代表更新は成功(succeeded+=1)、markが既知失敗(failed+=1)。
    assert mutation["attempted"] == 3
    assert mutation["succeeded"] == 2
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0
    assert mutation["state"] == "PARTIAL_MUTATION"
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"


def test_p2p_t9_schema_success_then_dedupe_unknown_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    monkeypatch.setattr(
        cli,
        "execute_schema_migration",
        lambda repo, names: SchemaMigrationResult(
            completion=SchemaWriteCompletion.SUCCEEDED, property_names=tuple(names)
        ),
    )
    apply_result = DedupeApplyResult(
        results=[
            GroupApplyResult(
                card_name="沼",
                representative_page_id="p1",
                representative_updated=True,
                duplicate_page_ids_marked=[],
                error="Notion APIへの接続がタイムアウトしました: 沼",
                failed_operation="mark_merged",
                failed_completion="unknown",
            )
        ]
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: apply_result)

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--apply", "--apply-schema", "--error-json"],
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["attempted"] == 3
    assert mutation["succeeded"] == 2
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 1
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"


# T10 ---------------------------------------------------------------------


def test_p2p_t10_dedupe_only_aggregate_no_schema_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """schema mutationなし(--apply-schema省略)の場合、aggregateはdedupe-only
    の結果と一致する(schema分のcountが混入しない)。"""
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch)  # missing_schema=[] (既にスキーマは揃っている)
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
                duplicate_page_ids_marked=[],
                error="Notion API呼び出しに失敗しました: 沼",
                failed_operation="mark_merged",
                failed_completion="known_failed",
            )
        ]
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: apply_result)

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--apply", "--error-json"]
    )

    assert result.exit_code == 1
    payload = _assert_pure_json_v2_mutation_error(
        result.stdout,
        command="dedupe-cards",
        category=ErrorCategory.PARTIAL_MUTATION,
        code=ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE,
    )
    mutation = payload["mutation"]
    assert mutation["attempted"] == 2
    assert mutation["succeeded"] == 1
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0


# T12 JSON purity ------------------------------------------------------------


@pytest.mark.parametrize(
    "args_suffix",
    [
        ["--apply", "--apply-schema"],  # schema failure path (T4寄り)
    ],
)
def test_p2p_t12_json_purity_on_schema_failure(
    monkeypatch: pytest.MonkeyPatch, args_suffix: list[str]
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "execute_schema_migration",
        lambda repo, names: (_ for _ in ()).throw(
            _schema_failure(SchemaWriteCompletion.KNOWN_FAILED)
        ),
    )

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", *args_suffix, "--error-json"]
    )

    # stdout全体が有効なJSON1個のみであることそのものがpurityの証明
    # (人間向けpreview/進捗テキストが混入していればjson.loads自体が失敗する)。
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


def test_p2p_t12_json_purity_on_dedupe_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
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
                duplicate_page_ids_marked=[],
                error="Notion API呼び出しに失敗しました: 沼",
                failed_operation="mark_merged",
                failed_completion="known_failed",
            )
        ]
    )
    monkeypatch.setattr(cli, "execute_dedupe_plan", lambda plan, repo: apply_result)

    result = runner.invoke(
        cli.app, ["dedupe-cards", "--card-name", "沼", "--apply", "--error-json"]
    )

    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


# T14/T15 dry-run regression under --error-json --------------------------------


def test_p2p_t14_dry_run_schema_only_error_json_no_writes_no_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app,
        ["dedupe-cards", "--card-name", "沼", "--dry-run", "--apply-schema", "--error-json"],
    )

    assert result.exit_code == 0
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_p2p_t15_dry_run_both_flags_error_json_no_writes_no_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    fake_repo = _patch_repo(monkeypatch, missing_schema=["所持枚数", "統合済み"])
    monkeypatch.setattr(
        cli,
        "build_dedupe_plan",
        lambda repo, card_name=None, representative_page_id=None: _sample_plan(),
    )
    execute_calls = {"count": 0}
    monkeypatch.setattr(
        cli,
        "execute_dedupe_plan",
        lambda plan, repo: execute_calls.__setitem__("count", execute_calls["count"] + 1),
    )

    result = runner.invoke(
        cli.app,
        [
            "dedupe-cards",
            "--card-name",
            "沼",
            "--dry-run",
            "--apply",
            "--apply-schema",
            "--error-json",
        ],
    )

    assert result.exit_code == 0
    assert fake_repo.apply_schema_migration_calls == []
    assert execute_calls["count"] == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
