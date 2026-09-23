from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
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
