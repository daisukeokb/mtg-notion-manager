"""Phase 3D-R1: dedupe-family(apply-dedupe-plan / apply-price-link-dedupe)の
mid-batch error mutation fidelity回帰テスト。

完了済みグループで書き込みを試みた後、後続グループの処理中(鮮度再監査・統合計画
作成のNotion読み取り)にMtgNotionManagerErrorで中断した場合でも、

- error_category/error_codeは実際の中断原因(読み取り失敗)のclassificationを維持し、
- mutationには中断前に完了したグループのwrite historyをError Contract v2で保持し、
- human出力の結果表・件数と既存形式の実行ログにも完了済みグループを残す

ことを固定する。real service(apply_dedupe_batch/apply_price_link_targets)・
DedupeRepository・build_dedupe_plan/execute_dedupe_planを通し、低レベル
NotionClientだけをfakeにする。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.error_contract import ErrorCategory, ErrorCode
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services.apply_dedupe_plan import (
    PartialDedupeApplyAbortedError,
    ReportGroup,
    apply_dedupe_batch,
)
from mtg_notion_manager.services.apply_price_link_dedupe import (
    PartialPriceLinkApplyAbortedError,
    PriceLinkTargetGroup,
    apply_price_link_targets,
)
from mtg_notion_manager.services.audit_duplicates import ExclusionList
from mtg_notion_manager.services.review_duplicate_conflicts import CATEGORY_PRICE_ONLY

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

runner = CliRunner()

_SCHEMA = {"properties": {"所持枚数": {"type": "number"}, "統合済み": {"type": "checkbox"}}}


def _fake_config() -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id="commander-ds-id",
        card_data_source_id="card-ds-id",
    )


def _timeout_error(message: str = "Notion APIへの接続がタイムアウトしました") -> NotionAPIError:
    error = NotionAPIError(message)
    error.__cause__ = httpx.TimeoutException("timed out")
    return error


def _http_error(status_code: int, message: str) -> NotionAPIError:
    request = httpx.Request("POST", "https://api.notion.com/v1/x")
    response = httpx.Response(status_code, request=request)
    error = NotionAPIError(message)
    error.__cause__ = httpx.HTTPStatusError("http error", request=request, response=response)
    return error


def _page(
    page_id: str,
    name: str,
    *,
    english_name: str | None = None,
    price: float | None = None,
    link: str | None = None,
) -> dict:
    offset = sum(ord(c) for c in page_id) % 50
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
    if price is not None:
        properties["販売価格"] = {"type": "number", "number": price}
    if link is not None:
        properties["販売リンク"] = {"type": "url", "url": link}
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": f"2024-01-01T00:{offset:02d}:00.000Z",
        "last_edited_time": f"2024-06-01T00:{offset:02d}:00.000Z",
        "properties": properties,
    }


class _MidBatchNotionClient:
    """グループ単位のNotion読み取り・書き込みを個別に失敗させられるfake低レベルclient。

    get_data_source()はbuild_dedupe_plan()内のmissing_schema_properties()から
    グループごとに1回呼ばれるため、schema_get_fail_on=Nで「N番目のグループの
    統合計画作成時の読み取り失敗」を再現できる。
    """

    def __init__(
        self,
        pages: list[dict],
        *,
        query_error: NotionAPIError | None = None,
        schema_get_fail_on: int | None = None,
        schema_get_error: NotionAPIError | None = None,
        relation_fail_page_id: str | None = None,
        update_errors: dict[str, NotionAPIError] | None = None,
    ) -> None:
        self.pages = pages
        self._query_error = query_error
        self._schema_get_fail_on = schema_get_fail_on
        self._schema_get_error = schema_get_error
        self._relation_fail_page_id = relation_fail_page_id
        self._update_errors = update_errors or {}
        self.query_calls = 0
        self.schema_get_calls = 0
        self.relation_calls = 0
        self.update_calls: list[str] = []
        if relation_fail_page_id is not None:
            prefix = relation_fail_page_id[:-1]
            for page in pages:
                if page["id"].startswith(prefix):
                    page["properties"]["採用デッキ"]["has_more"] = True

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        self.query_calls += 1
        if self._query_error is not None:
            raise self._query_error
        return self.pages

    def get_data_source(self, data_source_id: str) -> dict:
        self.schema_get_calls += 1
        if self.schema_get_calls == self._schema_get_fail_on:
            assert self._schema_get_error is not None
            raise self._schema_get_error
        return _SCHEMA

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        self.relation_calls += 1
        if page_id == self._relation_fail_page_id:
            raise _timeout_error("relation pagination timeout")
        return []

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.update_calls.append(page_id)
        error = self._update_errors.get(page_id)
        if error is not None:
            raise error
        for page in self.pages:
            if page["id"] == page_id:
                for name, value in properties.items():
                    page["properties"][name] = {
                        **page["properties"].get(name, {}),
                        **value,
                        "type": next(iter(value)),
                    }
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}


class _ClientCtx:
    def __init__(self, client: _MidBatchNotionClient) -> None:
        self._client = client

    def __enter__(self) -> _MidBatchNotionClient:
        return self._client

    def __exit__(self, *exc_info: object) -> None:
        return None


@dataclass(frozen=True)
class _CommandSpec:
    """両commandを同じシナリオで検証するための差分定義。

    group_order: CLIが実際に処理するグループ順(apply-dedupe-planは低リスク順ソートで
    レポート順のまま、apply-price-link-dedupe --scope canaryはカード名順)。
    representative/duplicate: 各グループで代表に選ばれるページIDの末尾
    (apply-dedupe-planは英語名ありの"1"、apply-price-link-dedupeは"2")。
    """

    name: str
    group_order: tuple[str, ...]
    representative: str
    duplicate: str
    log_glob: str

    def pages(self) -> list[dict]:
        pages: list[dict] = []
        for index, card in enumerate(("沼", "島", "森")):
            if self.name == "apply-dedupe-plan":
                pages += [_page(f"{card}1", card, english_name=f"E{card}"), _page(f"{card}2", card)]
            else:
                pages += [
                    _page(
                        f"{card}1", card, english_name=f"E{card}", price=100.0,
                        link="https://example.com/l",
                    ),
                    _page(
                        f"{card}2", card, english_name=f"E{card}", price=200.0 + index,
                        link="https://example.com/l",
                    ),
                ]
        return pages

    def rep(self, card: str) -> str:
        return f"{card}{self.representative}"

    def dup(self, card: str) -> str:
        return f"{card}{self.duplicate}"

    def args(self, tmp_path: Path, *, apply: bool = True) -> list[str]:
        cards = ("沼", "島", "森")
        if self.name == "apply-dedupe-plan":
            report = [
                {
                    "card_name": card,
                    "category": "auto",
                    "duplicate_count": 2,
                    "recommended_representative_id": f"{card}1",
                    "merged_deck_relation_count": 0,
                    "pages": [{"page_id": f"{card}1"}, {"page_id": f"{card}2"}],
                }
                for card in cards
            ]
            path = tmp_path / "audit.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            args = ["apply-dedupe-plan", "--audit-report", str(path)]
        else:
            report = [
                {
                    "card_name": card,
                    "review_category": CATEGORY_PRICE_ONLY,
                    "duplicate_count": 2,
                    "prices": [100.0, 200.0 + index],
                    "links": ["https://example.com/l"],
                    "merged_deck_relation_count": 0,
                    "pages": [{"page_id": f"{card}1"}, {"page_id": f"{card}2"}],
                }
                for index, card in enumerate(cards)
            ]
            path = tmp_path / "targets.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            args = ["apply-price-link-dedupe", "--targets-report", str(path), "--scope", "canary"]
        if apply:
            args.append("--apply")
        return [*args, "--output-dir", str(tmp_path / "out")]


_DEDUPE_PLAN = _CommandSpec(
    name="apply-dedupe-plan",
    group_order=("沼", "島", "森"),
    representative="1",
    duplicate="2",
    log_glob="dedupe-apply-*.json",
)
_PRICE_LINK = _CommandSpec(
    name="apply-price-link-dedupe",
    group_order=("島", "森", "沼"),
    representative="2",
    duplicate="1",
    log_glob="dedupe-price-apply-*.json",
)
_COMMANDS = pytest.mark.parametrize("spec", [_DEDUPE_PLAN, _PRICE_LINK], ids=lambda s: s.name)


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    spec: _CommandSpec,
    tmp_path: Path,
    *,
    error_json: bool = True,
    apply: bool = True,
    **client_kwargs: Any,
):
    monkeypatch.setattr(cli.Config, "load", staticmethod(_fake_config))
    monkeypatch.setattr(cli, "load_exclusions", lambda: ExclusionList())
    client = _MidBatchNotionClient(spec.pages(), **client_kwargs)
    monkeypatch.setattr(cli, "NotionClient", lambda api_key: _ClientCtx(client))
    args = spec.args(tmp_path, apply=apply)
    if error_json:
        args.append("--error-json")
    return runner.invoke(cli.app, args), client


def _apply_logs(spec: _CommandSpec, tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "out").glob(spec.log_glob))


def _assert_v1_notion_api_error(stdout: str, command: str) -> None:
    payload = json.loads(stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == command
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    assert "mutation" not in payload


def _assert_v2_notion_api_error(stdout: str, command: str) -> dict:
    assert "\x1b" not in stdout
    payload = json.loads(stdout)
    assert payload["schema_version"] == 2
    assert payload["command"] == command
    # carrierではなく実際の中断原因(読み取りのNotionAPIError)で分類される。
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    return payload["mutation"]


# --- T1/T2: mutation 0の読み取り失敗はv1のまま --------------------------------


@_COMMANDS
def test_r1_3d_t1_initial_query_failure_stays_v1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    result, client = _invoke(monkeypatch, spec, tmp_path, query_error=_timeout_error())

    assert result.exit_code == 1
    assert client.update_calls == []
    _assert_v1_notion_api_error(result.stdout, spec.name)
    assert _apply_logs(spec, tmp_path) == []


@_COMMANDS
def test_r1_3d_t2_first_group_schema_get_failure_stays_v1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    result, client = _invoke(
        monkeypatch, spec, tmp_path, schema_get_fail_on=1, schema_get_error=_timeout_error()
    )

    assert result.exit_code == 1
    assert client.update_calls == []
    _assert_v1_notion_api_error(result.stdout, spec.name)
    assert _apply_logs(spec, tmp_path) == []


# --- T3〜T6: 成功prefix + 後続グループの読み取り失敗 → v2 -------------------


def _assert_success_prefix_mutation(mutation: dict, attempted: int) -> None:
    assert mutation["attempted"] == attempted
    assert mutation["succeeded"] == attempted
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 0
    assert mutation["state"] == "MUTATION_SUCCEEDED"
    assert mutation["recovery_action"] == "NONE"
    assert "operations" not in mutation  # 成功operationは列挙しない既存Mutation Contract


@_COMMANDS
@pytest.mark.parametrize(
    "schema_get_error",
    [_timeout_error(), _http_error(500, "Notion API呼び出しに失敗しました (500)")],
    ids=["timeout", "http_500"],
)
def test_r1_3d_t3_t4_one_group_success_then_schema_get_failure_keeps_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spec: _CommandSpec,
    schema_get_error: NotionAPIError,
) -> None:
    """T3(timeout)/T4(HTTP 500): 1グループ目の代表更新+markが成功した後、
    2グループ目の統合計画作成時のschema GETが失敗する。"""
    first = spec.group_order[0]
    result, client = _invoke(
        monkeypatch, spec, tmp_path, schema_get_fail_on=2, schema_get_error=schema_get_error
    )

    assert result.exit_code == 1
    assert client.update_calls == [spec.rep(first), spec.dup(first)]
    mutation = _assert_v2_notion_api_error(result.stdout, spec.name)
    _assert_success_prefix_mutation(mutation, attempted=2)

    logs = _apply_logs(spec, tmp_path)
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding="utf-8"))
    assert log["applied"] is True
    assert log["summary"]["total"] == 1
    assert log["summary"]["applied"] == 1
    assert log["api_update_count"] == 2
    assert [g["card_name"] for g in log["groups"]] == [first]


@_COMMANDS
def test_r1_3d_t5_one_group_success_then_relation_pagination_failure_keeps_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """T5: 2グループ目の鮮度再監査中のrelation pagination読み取りが失敗する。"""
    first, second = spec.group_order[0], spec.group_order[1]
    result, client = _invoke(
        monkeypatch, spec, tmp_path, relation_fail_page_id=f"{second}1"
    )

    assert result.exit_code == 1
    assert client.relation_calls >= 1
    assert client.update_calls == [spec.rep(first), spec.dup(first)]
    mutation = _assert_v2_notion_api_error(result.stdout, spec.name)
    _assert_success_prefix_mutation(mutation, attempted=2)
    assert len(_apply_logs(spec, tmp_path)) == 1


@_COMMANDS
def test_r1_3d_t6_two_groups_success_then_third_group_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """T6: 2グループ(代表+mark=2 operation/グループ)成功後、3グループ目の読み取り失敗。"""
    result, client = _invoke(
        monkeypatch, spec, tmp_path, schema_get_fail_on=3, schema_get_error=_timeout_error()
    )

    assert result.exit_code == 1
    assert len(client.update_calls) == 4
    mutation = _assert_v2_notion_api_error(result.stdout, spec.name)
    _assert_success_prefix_mutation(mutation, attempted=4)
    log = json.loads(_apply_logs(spec, tmp_path)[0].read_text(encoding="utf-8"))
    assert log["summary"]["applied"] == 2
    assert log["api_update_count"] == 4


# --- failed / unknown prefix + 後続グループの読み取り失敗 ---------------------


@_COMMANDS
def test_r1_3d_failed_prefix_then_later_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """1グループ目のmarkがKNOWN_FAILED(構造化結果でbatch継続)、2グループ目成功、
    3グループ目の読み取り失敗。分類は読み取り失敗のまま、mutationはprefix全体を
    既存builderで集計した値になる。"""
    first = spec.group_order[0]
    result, client = _invoke(
        monkeypatch,
        spec,
        tmp_path,
        schema_get_fail_on=3,
        schema_get_error=_timeout_error(),
        update_errors={spec.dup(first): _http_error(400, "mark failed (400)")},
    )

    assert result.exit_code == 1
    assert len(client.update_calls) == 4
    mutation = _assert_v2_notion_api_error(result.stdout, spec.name)
    assert mutation["attempted"] == 4
    assert mutation["succeeded"] == 3
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0
    assert mutation["state"] == "PARTIAL_MUTATION"
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"
    assert mutation["operations"] == [
        {"key": first, "action": "mark_merged", "state": "failed"}
    ]


@_COMMANDS
def test_r1_3d_unknown_prefix_then_later_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """1グループ目の代表更新がUNKNOWN(timeout)、2グループ目成功、3グループ目の
    読み取り失敗。"""
    first = spec.group_order[0]
    result, client = _invoke(
        monkeypatch,
        spec,
        tmp_path,
        schema_get_fail_on=3,
        schema_get_error=_timeout_error(),
        update_errors={spec.rep(first): _timeout_error("representative timeout")},
    )

    assert result.exit_code == 1
    assert len(client.update_calls) == 3
    mutation = _assert_v2_notion_api_error(result.stdout, spec.name)
    assert mutation["attempted"] == 3
    assert mutation["succeeded"] == 2
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 1
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"
    assert mutation["operations"] == [
        {"key": first, "action": "representative_update", "state": "unknown"}
    ]


# --- human出力 / no-apply / 既存structured-result経路 -------------------------


@_COMMANDS
def test_r1_3d_human_mode_shows_completed_results_and_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """human modeでも完了済みグループの結果表・件数・実行ログと実際のエラーの両方が
    確認できる(既存の結果表示をそのまま再利用し、その後にエラーを表示する)。"""
    first = spec.group_order[0]
    result, _client = _invoke(
        monkeypatch,
        spec,
        tmp_path,
        error_json=False,
        schema_get_fail_on=2,
        schema_get_error=_timeout_error("Notion APIへの接続がタイムアウトしました"),
    )

    assert result.exit_code == 1
    plain = _ANSI_ESCAPE_RE.sub("", result.stdout)
    assert "適用結果" in plain
    assert first in plain.split("適用結果", 1)[1]
    assert "適用: 1件" in plain
    assert "失敗: 0件" in plain
    assert "実行ログ:" in plain
    assert "エラー: Notion APIへの接続がタイムアウトしました" in plain
    assert plain.index("適用結果") < plain.index("エラー:")
    assert "schema_version" not in plain
    assert len(_apply_logs(spec, tmp_path)) == 1


@_COMMANDS
def test_r1_3d_no_apply_midbatch_read_failure_stays_v1_without_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """--apply無しでは完了済みグループがあっても書き込みは0件のため、従来通り
    v1(mutationなし)のまま、結果表示・実行ログも追加しない。"""
    result, client = _invoke(
        monkeypatch,
        spec,
        tmp_path,
        apply=False,
        schema_get_fail_on=2,
        schema_get_error=_timeout_error(),
    )

    assert result.exit_code == 1
    assert client.update_calls == []
    _assert_v1_notion_api_error(result.stdout, spec.name)
    assert _apply_logs(spec, tmp_path) == []


@_COMMANDS
def test_r1_3d_write_failure_structured_path_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    """読み取り失敗が無い書き込み失敗は従来通り構造化結果経路
    (PARTIAL_MUTATION/DEDUPE_WRITE_PARTIAL_FAILURE)のまま。"""
    second = spec.group_order[1]
    result, client = _invoke(
        monkeypatch,
        spec,
        tmp_path,
        update_errors={spec.dup(second): _http_error(400, "mark failed (400)")},
    )

    assert result.exit_code == 1
    assert len(client.update_calls) == 6
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["error_category"] == ErrorCategory.PARTIAL_MUTATION
    assert payload["error_code"] == ErrorCode.DEDUPE_WRITE_PARTIAL_FAILURE
    assert payload["mutation"]["attempted"] == 6
    assert payload["mutation"]["failed"] == 1


@_COMMANDS
def test_r1_3d_all_success_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: _CommandSpec
) -> None:
    result, client = _invoke(monkeypatch, spec, tmp_path)

    assert result.exit_code == 0
    assert len(client.update_calls) == 6
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    assert len(_apply_logs(spec, tmp_path)) == 1


# --- service層: carrierの送出条件 ---------------------------------------------


def _dedupe_plan_groups() -> list[ReportGroup]:
    return [
        ReportGroup(
            card_name=card,
            duplicate_count=2,
            merged_deck_relation_count=0,
            recommended_representative_id=f"{card}1",
            page_ids=[f"{card}1", f"{card}2"],
        )
        for card in _DEDUPE_PLAN.group_order
    ]


def _price_link_targets() -> list[PriceLinkTargetGroup]:
    return [
        PriceLinkTargetGroup(
            card_name=card,
            review_category=CATEGORY_PRICE_ONLY,
            page_ids=[f"{card}1", f"{card}2"],
            prices=[100.0, 200.0 + ("沼", "島", "森").index(card)],
            links=["https://example.com/l"],
            merged_deck_relation_count=0,
        )
        for card in _PRICE_LINK.group_order
    ]


def test_r1_3d_service_carrier_keeps_cause_and_completed_outcomes() -> None:
    read_error = _timeout_error()
    dp_client = _MidBatchNotionClient(
        _DEDUPE_PLAN.pages(), schema_get_fail_on=2, schema_get_error=read_error
    )
    with pytest.raises(PartialDedupeApplyAbortedError) as dp_info:
        apply_dedupe_batch(
            DedupeRepository(dp_client, "card-ds-id"), _dedupe_plan_groups(), apply=True
        )
    assert dp_info.value.cause is read_error
    assert dp_info.value.__cause__ is read_error
    assert str(dp_info.value) == str(read_error)
    assert [o.card_name for o in dp_info.value.completed_outcomes] == ["沼"]

    pl_client = _MidBatchNotionClient(
        _PRICE_LINK.pages(), schema_get_fail_on=2, schema_get_error=read_error
    )
    with pytest.raises(PartialPriceLinkApplyAbortedError) as pl_info:
        apply_price_link_targets(
            DedupeRepository(pl_client, "card-ds-id"), _price_link_targets(), apply=True
        )
    assert pl_info.value.cause is read_error
    assert pl_info.value.__cause__ is read_error
    assert [o.card_name for o in pl_info.value.completed_outcomes] == ["島"]


def test_r1_3d_service_first_group_failure_propagates_original_exception() -> None:
    """完了済みグループが無い場合はcarrierを使わず元の例外をそのまま伝播する。"""
    read_error = _timeout_error()
    dp_client = _MidBatchNotionClient(
        _DEDUPE_PLAN.pages(), schema_get_fail_on=1, schema_get_error=read_error
    )
    with pytest.raises(NotionAPIError) as dp_info:
        apply_dedupe_batch(
            DedupeRepository(dp_client, "card-ds-id"), _dedupe_plan_groups(), apply=True
        )
    assert dp_info.value is read_error

    pl_client = _MidBatchNotionClient(
        _PRICE_LINK.pages(), schema_get_fail_on=1, schema_get_error=read_error
    )
    with pytest.raises(NotionAPIError) as pl_info:
        apply_price_link_targets(
            DedupeRepository(pl_client, "card-ds-id"), _price_link_targets(), apply=True
        )
    assert pl_info.value is read_error
