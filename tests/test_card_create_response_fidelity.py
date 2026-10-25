"""Phase 3G: カード新規作成(POST /pages)の2xx非JSON応答のresponse fidelity回帰テスト。

real NotionClient._request(pytest-httpxでHTTP層だけをmock)・CardRepository・
execute_import_cards / execute_article_importを通し、計画の構築(記事取得・Notion
読み取り)だけを差し替える。mockの応答はNotionサーバーの実際の挙動を証明するもの
ではなく、「クライアントが何回POSTし、結果をどう分類・報告するか」だけを固定する。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from mtg_notion_manager import cli
from mtg_notion_manager.config import Config
from mtg_notion_manager.error_contract import ErrorCategory, ErrorCode
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.import_cards_mutation_adapter import build_import_cards_mutation_summary
from mtg_notion_manager.models import CardDecision, DeckCard, ParsedDeckList, VerifiedNewCard
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.notion.client import API_BASE_URL, NotionClient
from mtg_notion_manager.services import import_cards
from mtg_notion_manager.services.card_resolution import resolve_new_card
from mtg_notion_manager.services.import_article import ArticleImportPlan, DeckArticleEntry
from mtg_notion_manager.services.import_cards import ImportCardsPlan

runner = CliRunner()

ARTICLE_URL = "https://mtg-jp.com/reading/publicity/0000003/"
DECK_PAGE_ID = "deck-page-id-for-test"
PAGES_URL = f"{API_BASE_URL}/pages"

#: 2xxだが本文をJSONとして解釈できない応答(status, 本文bytes, 期待するcauseの型)。
_NON_JSON_2XX = [
    (200, b"<html>ok</html>", json.JSONDecodeError),
    (201, b"<html>created</html>", json.JSONDecodeError),
    (200, b"", json.JSONDecodeError),
    (200, b"\x80not-utf8", UnicodeDecodeError),
]
_NON_JSON_IDS = ["200_html", "201_html", "200_empty", "200_invalid_utf8"]


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


def _card(name_ja: str) -> DeckCard:
    return DeckCard(
        name_ja=name_ja, name_en=None, quantity=1, is_commander=False, source_url=ARTICLE_URL
    )


def _plan(*names: str) -> ImportCardsPlan:
    cards = [_card(name) for name in names]
    parsed = ParsedDeckList(
        deck_name="テストデッキ", commander_name="テスト統率者", cards=cards, source_url=ARTICLE_URL
    )
    return ImportCardsPlan(
        parsed=parsed,
        deck_page_id=DECK_PAGE_ID,
        decisions=[CardDecision(card=card, action="create") for card in cards],
    )


def _post_count(httpx_mock: HTTPXMock) -> int:
    return sum(1 for r in httpx_mock.get_requests() if r.method == "POST" and r.url == PAGES_URL)


def _execute(plan: ImportCardsPlan) -> import_cards.ImportCardsResult:
    with NotionClient("secret_test") as client:
        return import_cards.execute_import_cards(plan, CardRepository(client, "card-ds-id"))


# --- A. CardRepository / execute_import_cards ---------------------------------------


@pytest.mark.parametrize(("status_code", "body", "cause_type"), _NON_JSON_2XX, ids=_NON_JSON_IDS)
def test_card_create_non_json_2xx_preserves_cause(
    httpx_mock: HTTPXMock, status_code: int, body: bytes, cause_type: type
) -> None:
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=status_code, content=body)
    # 本番と同じ経路(resolve_new_card)で、検証済みの新規カードを得る。
    verified = resolve_new_card(
        _card("テストカード"),
        article_url=ARTICLE_URL,
        deck_name="テストデッキ",
        confirmed_mapping=None,
    ).verified_card
    assert isinstance(verified, VerifiedNewCard)

    with NotionClient("secret_test") as client, pytest.raises(NotionAPIError) as info:
        CardRepository(client, "card-ds-id").create_card(verified, DECK_PAGE_ID)

    assert isinstance(info.value.__cause__, cause_type)  # 元の例外をcauseとして保持
    assert "カードの作成結果は不明です" in str(info.value)
    assert "自動リトライしません" in str(info.value)
    assert _post_count(httpx_mock) == 1  # 自動再試行なし


@pytest.mark.parametrize(("status_code", "body", "cause_type"), _NON_JSON_2XX, ids=_NON_JSON_IDS)
def test_card_create_non_json_2xx_is_unknown(
    httpx_mock: HTTPXMock, status_code: int, body: bytes, cause_type: type
) -> None:
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=status_code, content=body)

    result = _execute(_plan("テストカード"))

    r = result.results[0]
    assert r.action == "failed"
    assert r.failed_operation == import_cards.FailedWriteOperation.CREATE
    assert r.failed_completion == import_cards.WriteCompletion.UNKNOWN
    assert _post_count(httpx_mock) == 1
    summary = build_import_cards_mutation_summary(result.results)
    assert (summary.attempted, summary.succeeded, summary.failed, summary.unknown) == (1, 0, 0, 1)
    assert summary.state == "MUTATION_STATE_UNKNOWN"
    assert summary.recovery_action == "RECONCILE_BEFORE_RETRY"


def test_card_create_valid_json_2xx_unchanged(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=PAGES_URL, method="POST", json={"id": "card-1", "url": "https://notion.so/card-1"}
    )

    result = _execute(_plan("テストカード"))

    r = result.results[0]
    assert r.action == "created"
    assert r.page_id == "card-1"
    assert _post_count(httpx_mock) == 1


# --- E. 複数カード: 論理計数 ------------------------------------------------------


def test_success_then_unknown_then_success_counts_each_logical_create(
    httpx_mock: HTTPXMock,
) -> None:
    """既存batch contract(失敗・不明の後も後続カードを処理する)は変えず、成功・
    UNKNOWNを論理操作単位で数える。"""
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "card-a"})
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=200, content=b"not json")
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "card-c"})

    result = _execute(_plan("カードA", "カードB", "カードC"))

    assert [r.action for r in result.results] == ["created", "failed", "created"]
    assert _post_count(httpx_mock) == 3  # 各カード1回(カードBの再作成なし)
    summary = build_import_cards_mutation_summary(result.results)
    assert (summary.attempted, summary.succeeded, summary.failed, summary.unknown) == (3, 2, 0, 1)
    assert summary.attempted == summary.succeeded + summary.failed + summary.unknown
    assert summary.state == "MUTATION_STATE_UNKNOWN"


def test_unknown_then_abort_does_not_count_unattempted_card(httpx_mock: HTTPXMock) -> None:
    """カードA成功・カードB不明(2xx非JSON)の後、カードCで既存の中断条件
    (日本語名未確認、PartialImportAbortedError)に当たった場合、カードCは未試行で
    attemptedへ含めない。"""
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "card-a"})
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=200, content=b"not json")
    plan = _plan("カードA", "カードB")
    unverified = DeckCard(
        name_ja=None, name_en="Card C", quantity=1, is_commander=False, source_url=ARTICLE_URL
    )
    plan = ImportCardsPlan(
        parsed=plan.parsed,
        deck_page_id=plan.deck_page_id,
        decisions=[*plan.decisions, CardDecision(card=unverified, action="create")],
    )

    with pytest.raises(import_cards.PartialImportAbortedError) as info:
        _execute(plan)

    assert _post_count(httpx_mock) == 2  # カードCへは書き込まない
    summary = build_import_cards_mutation_summary(info.value.completed_results)
    assert (summary.attempted, summary.succeeded, summary.failed, summary.unknown) == (2, 1, 0, 1)


def test_exhausted_429_then_non_json_counts_logical_operations(httpx_mock: HTTPXMock) -> None:
    """429のリトライ(既存policy)を使い切ったカードAはKNOWN_FAILED(HTTP 6回でも論理1件)、
    カードBの2xx非JSONはUNKNOWN(HTTP 1回)。"""
    for _ in range(6):
        httpx_mock.add_response(
            url=PAGES_URL, method="POST", status_code=429, headers={"Retry-After": "0"}
        )
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=200, content=b"not json")

    result = _execute(_plan("カードA", "カードB"))

    assert [r.failed_completion for r in result.results] == [
        import_cards.WriteCompletion.KNOWN_FAILED,
        import_cards.WriteCompletion.UNKNOWN,
    ]
    assert _post_count(httpx_mock) == 7
    summary = build_import_cards_mutation_summary(result.results)
    assert (summary.attempted, summary.succeeded, summary.failed, summary.unknown) == (2, 0, 1, 1)


# --- B/C. CLI: import-cards(v2) / import-article(既存のv1契約) ---------------------


def _patch_card_plan(monkeypatch: pytest.MonkeyPatch, plan: ImportCardsPlan) -> None:
    monkeypatch.setattr(cli, "build_import_cards_plan", lambda *args, **kwargs: plan)


def _import_cards(*extra: str):
    return runner.invoke(
        cli.app, ["import-cards", ARTICLE_URL, "--deck-page-id", DECK_PAGE_ID, "--apply", *extra]
    )


def test_import_cards_non_json_error_json_is_v2_unknown(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _patch_card_plan(monkeypatch, _plan("テストカード"))
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=200, content=b"not json")

    result = _import_cards("--error-json")

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)  # 未捕捉なし
    assert _post_count(httpx_mock) == 1
    payload = json.loads(result.stdout)  # import-cardsはstdout全体が純粋なJSON
    assert payload["schema_version"] == 2
    # 正常returnしたbatchの部分失敗としての既存分類のまま。
    assert payload["error_category"] == ErrorCategory.PARTIAL_MUTATION
    assert payload["error_code"] == ErrorCode.CARD_WRITE_PARTIAL_FAILURE
    assert payload["mutation"] == {
        "state": "MUTATION_STATE_UNKNOWN",
        "attempted": 1,
        "succeeded": 0,
        "failed": 0,
        "unknown": 1,
        "recovery_action": "RECONCILE_BEFORE_RETRY",
        "operations": [{"key": "テストカード", "action": "create", "state": "unknown"}],
    }


def test_import_cards_non_json_human_mode(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _patch_card_plan(monkeypatch, _plan("テストカード"))
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=200, content=b"not json")

    result = _import_cards()

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _post_count(httpx_mock) == 1
    assert "schema_version" not in result.stdout
    flat = "".join(result.stdout.split())
    assert "カードの作成結果は不明です" in flat
    # human表示のfooterもRECONCILE_BEFORE_RETRYと同じ意味(確認前の再実行を促さない)。
    assert "完了状態が不明な書き込みが1件あります" in flat
    assert "同じコマンドを再実行してください" not in flat


@pytest.mark.parametrize(
    ("response_kwargs", "expect_unknown_guidance"),
    [
        ({"status_code": 503}, True),
        ({"status_code": 400, "text": "bad"}, False),
    ],
    ids=["unknown_503", "known_failed_400"],
)
def test_import_cards_human_footer_matches_completion(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    response_kwargs: dict,
    expect_unknown_guidance: bool,
) -> None:
    """完了状態が不明な失敗を含む場合だけ「確認してから再実行」の案内になり、
    既知の失敗だけの場合は既存の「同じコマンドを再実行」の案内のまま。"""
    _patch_card_plan(monkeypatch, _plan("テストカード"))
    httpx_mock.add_response(url=PAGES_URL, method="POST", **response_kwargs)

    result = _import_cards()

    assert result.exit_code == 1
    flat = "".join(result.stdout.split())
    assert ("完了状態が不明な書き込み" in flat) is expect_unknown_guidance
    assert ("同じコマンドを再実行してください" in flat) is not expect_unknown_guidance


def test_import_cards_success_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    _patch_card_plan(monkeypatch, _plan("テストカード"))
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "card-1"})

    result = _import_cards("--error-json")

    assert result.exit_code == 0
    assert _post_count(httpx_mock) == 1
    assert "schema_version" not in result.stdout


def _article_plan() -> ArticleImportPlan:
    def entry(deck_name: str, card_name: str) -> DeckArticleEntry:
        card = _card(card_name)
        parsed = ParsedDeckList(
            deck_name=deck_name, commander_name=card_name, cards=[card], source_url=ARTICLE_URL
        )
        cards_plan = ImportCardsPlan(
            parsed=parsed,
            deck_page_id=f"deck-{deck_name}",
            decisions=[CardDecision(card=card, action="create")],
        )
        return DeckArticleEntry(
            deck_name=deck_name,
            status="ready",
            deck_page_id=f"deck-{deck_name}",
            deck_page_url=f"https://notion.so/deck-{deck_name}",
            cards_plan=cards_plan,
        )

    entries = [entry("デッキA", "カードA"), entry("デッキB", "カードB")]
    return ArticleImportPlan(
        source_url=ARTICLE_URL,
        all_deck_names=[e.deck_name for e in entries],
        excluded_deck_names=[],
        entries=entries,
    )


@pytest.mark.parametrize("error_json", [True, False], ids=["error_json", "human"])
def test_import_article_non_json_follows_existing_contract(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, tmp_path: Path, error_json: bool
) -> None:
    """import-articleは既存のv1契約のまま: カード書き込みの失敗はdeck単位の結果として
    表示・実行ログへ残し、終了コード1。2xx非JSONでも未捕捉例外(INTERNAL/
    UNHANDLED_EXCEPTION)にならない。"""
    plan = _article_plan()
    monkeypatch.setattr(cli, "build_article_import_plan", lambda *args, **kwargs: plan)
    httpx_mock.add_response(url=PAGES_URL, method="POST", json={"id": "card-a"})
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=200, content=b"not json")

    args = ["import-article", ARTICLE_URL, "--apply", "--output-dir", str(tmp_path)]
    result = runner.invoke(cli.app, [*args, "--error-json"] if error_json else args)

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _post_count(httpx_mock) == 2
    assert ErrorCode.UNHANDLED_EXCEPTION not in result.stdout
    assert "schema_version" not in result.stdout
    logs = list(tmp_path.glob("import-article-*.json"))
    assert len(logs) == 1
    log_text = logs[0].read_text(encoding="utf-8")
    assert "カードの作成結果は不明です" in log_text


# --- D. 再試行の回帰(既存policy) ------------------------------------------------


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_card_create_ambiguous_5xx_still_single_post(
    httpx_mock: HTTPXMock, status_code: int
) -> None:
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=status_code)

    result = _execute(_plan("テストカード"))

    assert result.results[0].failed_completion == import_cards.WriteCompletion.UNKNOWN
    assert _post_count(httpx_mock) == 1


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ConnectError("connection refused"),
    ],
    ids=["read_timeout", "read_error", "remote_protocol_error", "connect_error"],
)
def test_card_create_transport_errors_unchanged(
    httpx_mock: HTTPXMock, exception: Exception
) -> None:
    httpx_mock.add_exception(exception, url=PAGES_URL, method="POST")

    result = _execute(_plan("テストカード"))

    r = result.results[0]
    assert r.failed_completion == import_cards.WriteCompletion.UNKNOWN
    assert _post_count(httpx_mock) == 1
    # 3G は2xx非JSONだけが対象: 通信エラーのメッセージは従来のNotionClientのまま。
    assert "カードの作成結果は不明です" not in (r.error or "")


def test_card_create_400_still_known_failed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=PAGES_URL, method="POST", status_code=400, text="bad")

    result = _execute(_plan("テストカード"))

    assert result.results[0].failed_completion == import_cards.WriteCompletion.KNOWN_FAILED
    assert _post_count(httpx_mock) == 1


def test_client_read_non_json_behavior_unchanged(httpx_mock: HTTPXMock) -> None:
    """3Gの修復はカード作成(CardRepository.create_card)だけ: NotionClientのread系の
    2xx非JSONの扱いは変えない。"""
    httpx_mock.add_response(url=f"{API_BASE_URL}/pages/p1", status_code=200, content=b"not json")

    with NotionClient("secret_test") as client, pytest.raises(json.JSONDecodeError):
        client.get_page("p1")
