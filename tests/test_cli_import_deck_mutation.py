"""Phase 3E-R1: import(デッキ新規作成)のError Contract v2 mutation接続の回帰テスト。

real NotionClient._request(pytest-httpxでHTTP層だけをmock)・build_import_plan・
NotionWriter・execute_importを通し、記事の取得(fetcher)だけを差し替える。mockの
応答はNotionサーバーの実際の挙動を証明するものではなく、「クライアントが何回POSTし、
結果をどう報告するか」だけを固定する。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.error_contract import ErrorCategory, ErrorCode
from mtg_notion_manager.exceptions import FetchError, NotionAPIError
from mtg_notion_manager.import_deck_mutation_adapter import build_deck_create_mutation_summary
from mtg_notion_manager.models import RawDeckData
from mtg_notion_manager.notion.client import API_BASE_URL
from mtg_notion_manager.services import import_deck
from mtg_notion_manager.services.import_deck import (
    DeckCreateCompletion,
    classify_deck_create_failure,
)

runner = CliRunner()

DECK_URL = "https://mtg-jp.com/reading/publicity/0000002/"
DECK_NAME = "テストデッキ"
PAGES_URL = f"{API_BASE_URL}/pages"
QUERY_URL = f"{API_BASE_URL}/data_sources/commander-ds-id/query"


class _FakeFetcher:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def fetch(self, url: str, deck_name: str | None = None) -> RawDeckData:
        if self._error is not None:
            raise self._error
        return RawDeckData(
            name=DECK_NAME,
            commander="テスト統率者",
            set_raw="ブルームバロウ",
            colors_raw=["赤", "緑"],
            source_url=url,
        )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mtg_notion_manager.notion.client.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        cli.Config,
        "load",
        staticmethod(
            lambda: Config(
                notion_api_key="secret_test",
                commander_data_source_id="commander-ds-id",
                card_data_source_id="card-ds-id",
            )
        ),
    )
    monkeypatch.setattr(import_deck, "get_fetcher", lambda url: _FakeFetcher())


def _no_existing_deck(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=QUERY_URL, method="POST", json={"results": []})


def _post_count(httpx_mock: HTTPXMock) -> int:
    return sum(1 for r in httpx_mock.get_requests() if r.method == "POST" and r.url == PAGES_URL)


def _invoke(*extra: str, stdin: str = "y\n"):
    return runner.invoke(cli.app, ["import", DECK_URL, *extra], input=stdin)


def _last_json(stdout: str) -> dict:
    # import固有: 確認前のプレビューがstdoutに出ているため、Error JSONは末尾の1行。
    return json.loads(stdout.strip().splitlines()[-1])


# --- 書き込み前の失敗: 従来通りv1(mutationなし) ---------------------------------


def test_pre_create_query_failure_stays_v1(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=QUERY_URL, method="POST", status_code=400, text="bad")

    result = _invoke("--error-json")

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 0
    payload = json.loads(result.stdout)  # 確認前のエラーはstdout全体が純粋なJSON
    assert payload["schema_version"] == 1
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    assert "mutation" not in payload


def test_pre_create_fetch_failure_stays_v1(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setattr(
        import_deck, "get_fetcher", lambda url: _FakeFetcher(FetchError("fetch failed"))
    )

    result = _invoke("--error-json")

    assert result.exit_code == 1
    assert httpx_mock.get_requests() == []
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["error_category"] == ErrorCategory.EXTERNAL_SOURCE
    assert "mutation" not in payload


# --- 書き込みなしの経路: 従来通り(exit 0・JSONなし) --------------------------


@pytest.mark.parametrize(
    ("extra", "stdin", "expected"),
    [
        (("--dry-run",), "", "--dry-run のためNotionへの書き込みは行いません"),
        ((), "n\n", "キャンセルしました"),
    ],
    ids=["dry_run", "declined"],
)
def test_no_write_paths_unchanged(
    httpx_mock: HTTPXMock, extra: tuple[str, ...], stdin: str, expected: str
) -> None:
    _no_existing_deck(httpx_mock)

    result = _invoke(*extra, "--error-json", stdin=stdin)

    assert result.exit_code == 0
    assert _post_count(httpx_mock) == 0
    assert expected in result.stdout
    assert "schema_version" not in result.stdout


def test_duplicate_deck_skip_unchanged(httpx_mock: HTTPXMock) -> None:
    existing = {"id": "existing", "url": "https://notion.so/existing", "properties": {}}
    httpx_mock.add_response(url=QUERY_URL, method="POST", json={"results": [existing]})

    result = _invoke("--error-json")

    assert result.exit_code == 0
    assert _post_count(httpx_mock) == 0
    assert "重複のため登録をスキップしました" in result.stdout
    assert "schema_version" not in result.stdout


def test_create_success_unchanged(httpx_mock: HTTPXMock) -> None:
    _no_existing_deck(httpx_mock)
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "deck-new"})

    result = _invoke("--error-json")

    assert result.exit_code == 0
    assert _post_count(httpx_mock) == 1
    assert "Notionに登録しました" in result.stdout
    assert "schema_version" not in result.stdout


# --- 書き込み試行後の失敗: v2(分類は実際の原因のまま) --------------------------


def test_create_400_is_known_failed_v2(httpx_mock: HTTPXMock) -> None:
    _no_existing_deck(httpx_mock)
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=400, text="validation")

    result = _invoke("--error-json")

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 1
    payload = _last_json(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["command"] == "import"
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    assert payload["mutation"] == {
        "state": "MUTATION_FAILED",
        "attempted": 1,
        "succeeded": 0,
        "failed": 1,
        "unknown": 0,
        "recovery_action": "MANUAL_REVIEW_REQUIRED",
        "operations": [{"key": DECK_NAME, "action": "create", "state": "failed"}],
    }


def test_create_exhausted_429_is_known_failed(httpx_mock: HTTPXMock) -> None:
    """429のリトライ(Phase 3F-R1で維持)を使い切った場合はKNOWN_FAILED。"""
    _no_existing_deck(httpx_mock)
    for _ in range(6):
        httpx_mock.add_response(
            url=PAGES_URL, method="POST", status_code=429, headers={"Retry-After": "0"}
        )

    result = _invoke("--error-json")

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 6  # HTTP試行回数(初回+5回)
    mutation = _last_json(result.stdout)["mutation"]
    assert mutation["attempted"] == 1  # 論理操作は1件
    assert mutation["failed"] == 1
    assert mutation["state"] == "MUTATION_FAILED"


def _assert_unknown_create(result, httpx_mock: HTTPXMock) -> dict:
    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 1
    assert "Notionに登録しました" not in result.stdout
    payload = _last_json(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    assert payload["mutation"] == {
        "state": "MUTATION_STATE_UNKNOWN",
        "attempted": 1,
        "succeeded": 0,
        "failed": 0,
        "unknown": 1,
        "recovery_action": "RECONCILE_BEFORE_RETRY",
        "operations": [{"key": DECK_NAME, "action": "create", "state": "unknown"}],
    }
    return payload


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_create_ambiguous_5xx_is_unknown_v2(httpx_mock: HTTPXMock, status_code: int) -> None:
    _no_existing_deck(httpx_mock)
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=status_code)

    result = _invoke("--error-json")

    _assert_unknown_create(result, httpx_mock)


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ConnectError("connection refused"),
    ],
    ids=["read_timeout", "remote_protocol_error", "connect_error"],
)
def test_create_transport_failure_is_unknown_v2(
    httpx_mock: HTTPXMock, exception: Exception
) -> None:
    _no_existing_deck(httpx_mock)
    httpx_mock.add_exception(exception, url=PAGES_URL, method="POST")

    result = _invoke("--error-json")

    _assert_unknown_create(result, httpx_mock)


def test_create_timeout_message_is_preserved(httpx_mock: HTTPXMock) -> None:
    """mutationを付与しても、timeout特有の安全上重要なmessageは変更されない。"""
    _no_existing_deck(httpx_mock)
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=PAGES_URL, method="POST")

    result = _invoke("--error-json")

    payload = _assert_unknown_create(result, httpx_mock)
    assert "サーバー側で実際には処理が完了していた可能性を排除できない" in payload["message"]


def test_create_failure_human_mode_unchanged(httpx_mock: HTTPXMock) -> None:
    _no_existing_deck(httpx_mock)
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=503)

    result = _invoke()

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 1
    assert "エラー: Notion API呼び出しに失敗しました (503)" in result.stdout
    assert "schema_version" not in result.stdout


# --- 分類・adapter単体 ------------------------------------------------------------


def _api_error(cause: BaseException) -> NotionAPIError:
    error = NotionAPIError("boom")
    error.__cause__ = cause
    return error


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", PAGES_URL)
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status_code, request=request)
    )


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (_status_error(400), DeckCreateCompletion.KNOWN_FAILED),
        (_status_error(404), DeckCreateCompletion.KNOWN_FAILED),
        (_status_error(429), DeckCreateCompletion.KNOWN_FAILED),
        (_status_error(500), DeckCreateCompletion.UNKNOWN),
        (_status_error(502), DeckCreateCompletion.UNKNOWN),
        (_status_error(503), DeckCreateCompletion.UNKNOWN),
        (_status_error(504), DeckCreateCompletion.UNKNOWN),
        (httpx.ReadTimeout("t"), DeckCreateCompletion.UNKNOWN),
        (httpx.ReadError("r"), DeckCreateCompletion.UNKNOWN),
    ],
)
def test_classify_deck_create_failure(cause: BaseException, expected: str) -> None:
    assert classify_deck_create_failure(_api_error(cause)) == expected


def test_classify_deck_create_failure_without_cause_is_unknown() -> None:
    assert classify_deck_create_failure(NotionAPIError("boom")) == DeckCreateCompletion.UNKNOWN


def test_classify_deck_create_failure_ignores_message() -> None:
    error = NotionAPIError("Notion APIへの接続がタイムアウトしました")
    error.__cause__ = _status_error(400)
    assert classify_deck_create_failure(error) == DeckCreateCompletion.KNOWN_FAILED


def test_adapter_rejects_unknown_completion_value() -> None:
    with pytest.raises(ValueError, match="Unknown DeckCreateCompletion"):
        build_deck_create_mutation_summary(DECK_NAME, "succeeded")
