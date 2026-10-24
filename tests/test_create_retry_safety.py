"""Phase 3F-R1: べき等でないページ作成(POST /pages)の5xx自動リトライ禁止の
CLI統合回帰テスト。

real NotionClient._request(pytest-httpxでHTTP層だけをmock)・NotionWriter/
CardRepository・execute_import/execute_import_cardsを通し、計画の構築(記事取得・
Notion読み取り)だけを差し替える。mockの応答はNotionサーバーの実際の挙動を証明する
ものではなく、「クライアントが何回POSTし、結果をどう報告するか」だけを固定する。
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.error_contract import ErrorCategory, ErrorCode
from mtg_notion_manager.models import CardDecision, DeckCard, DeckRecord, ParsedDeckList
from mtg_notion_manager.notion.client import API_BASE_URL
from mtg_notion_manager.services.import_cards import ImportCardsPlan
from mtg_notion_manager.services.import_deck import ImportPlan

runner = CliRunner()

DECK_URL = "https://mtg-jp.com/reading/publicity/0000001/"
DECK_PAGE_ID = "deck-page-id-for-test"
PAGES_URL = f"{API_BASE_URL}/pages"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mtg_notion_manager.notion.client.time.sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _post_count(httpx_mock: HTTPXMock) -> int:
    return sum(1 for r in httpx_mock.get_requests() if r.method == "POST" and r.url == PAGES_URL)


# --- deck import(import、v1のみ) ----------------------------------------------


def _patch_deck_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    record = DeckRecord(
        name="テストデッキ",
        commander="テスト統率者",
        set_name="ブルームバロウ",
        colors=["赤", "緑"],
        deck_list_url=DECK_URL,
    )
    monkeypatch.setattr(
        cli,
        "build_import_plan",
        lambda url, writer, deck_name=None: ImportPlan(record=record, existing=None),
    )


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_deck_import_503_sends_one_post_and_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _patch_deck_plan(monkeypatch)
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=503)
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "second-page"})

    result = runner.invoke(cli.app, ["import", DECK_URL, "--error-json"], input="y\n")

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 1  # 二重作成につながる自動リトライをしない
    assert "Notionに登録しました" not in result.stdout  # 曖昧な5xxを成功扱いしない
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    # Phase 3E-R1: 書き込み試行後の失敗はv2。分類は実際の原因のまま、503は完了状態不明。
    assert payload["schema_version"] == 2
    assert payload["command"] == "import"
    assert payload["error_category"] == ErrorCategory.PRODUCTION_API
    assert payload["error_code"] == ErrorCode.NOTION_API_ERROR
    mutation = payload["mutation"]
    assert mutation["attempted"] == 1
    assert mutation["unknown"] == 1
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"


# --- card import(import-cards、v2) ----------------------------------------------


def _patch_card_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    card = DeckCard(
        name_ja="テストカード", name_en=None, quantity=1, is_commander=False, source_url=DECK_URL
    )
    parsed = ParsedDeckList(
        deck_name="テストデッキ", commander_name="テスト統率者", cards=[card], source_url=DECK_URL
    )
    plan = ImportCardsPlan(
        parsed=parsed,
        deck_page_id=DECK_PAGE_ID,
        decisions=[CardDecision(card=card, action="create")],
    )
    monkeypatch.setattr(cli, "build_import_cards_plan", lambda *args, **kwargs: plan)


def _invoke_import_cards() -> object:
    return runner.invoke(
        cli.app,
        ["import-cards", DECK_URL, "--deck-page-id", DECK_PAGE_ID, "--apply", "--error-json"],
    )


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_card_import_503_sends_one_post_and_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _patch_card_plan(monkeypatch)
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=503)
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "second-page"})

    result = _invoke_import_cards()

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2
    assert payload["error_category"] == ErrorCategory.PARTIAL_MUTATION
    assert payload["error_code"] == ErrorCode.CARD_WRITE_PARTIAL_FAILURE
    mutation = payload["mutation"]
    # MutationSummaryは論理操作単位(HTTP試行回数ではない)。
    assert mutation["attempted"] == 1
    assert mutation["succeeded"] == 0
    assert mutation["failed"] == 0
    assert mutation["unknown"] == 1
    assert mutation["state"] == "MUTATION_STATE_UNKNOWN"
    assert mutation["recovery_action"] == "RECONCILE_BEFORE_RETRY"
    assert mutation["operations"] == [
        {"key": "テストカード", "action": "create", "state": "unknown"}
    ]


def test_card_import_400_keeps_known_failure_semantics(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _patch_card_plan(monkeypatch)
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=400, text="bad")

    result = _invoke_import_cards()

    assert result.exit_code == 1
    assert _post_count(httpx_mock) == 1
    mutation = json.loads(result.stdout)["mutation"]
    assert mutation["attempted"] == 1
    assert mutation["failed"] == 1
    assert mutation["unknown"] == 0
    assert mutation["state"] == "MUTATION_FAILED"
    assert mutation["recovery_action"] == "MANUAL_REVIEW_REQUIRED"
