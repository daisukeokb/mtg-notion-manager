from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.client import API_BASE_URL, NotionClient


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """リトライの待機を実際には行わずテストを高速化する。"""
    monkeypatch.setattr("mtg_notion_manager.notion.client.time.sleep", lambda seconds: None)


class TestRequestRetry:
    def test_success_returns_json(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", json={"name": "bot"})

        with NotionClient("secret_test") as client:
            result = client.get_current_user()

        assert result == {"name": "bot"}

    def test_429_retries_and_then_succeeds(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=f"{API_BASE_URL}/users/me", status_code=429, headers={"Retry-After": "0"}
        )
        httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", json={"name": "bot"})

        with NotionClient("secret_test") as client:
            result = client.get_current_user()

        assert result == {"name": "bot"}

    def test_500_retries_and_then_succeeds(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", status_code=500)
        httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", json={"name": "bot"})

        with NotionClient("secret_test") as client:
            result = client.get_current_user()

        assert result == {"name": "bot"}

    def test_exceeds_max_retries_raises(self, httpx_mock: HTTPXMock) -> None:
        # 初回 + MAX_RETRIES(5)回のリトライ = 合計6回試行して失敗する
        for _ in range(6):
            httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", status_code=503)

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError):
            client.get_current_user()

    def test_non_retryable_error_raises_immediately(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", status_code=404, text="not found")

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError):
            client.get_current_user()

        # 404はリトライ対象外なので1回しか呼ばれない
        assert len(httpx_mock.get_requests()) == 1

    def test_timeout_retries_then_succeeds(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
        httpx_mock.add_response(url=f"{API_BASE_URL}/users/me", json={"name": "bot"})

        with NotionClient("secret_test") as client:
            result = client.get_current_user()

        assert result == {"name": "bot"}

    def test_create_page_timeout_does_not_retry(self, httpx_mock: HTTPXMock) -> None:
        """create_page()はべき等ではないため、タイムアウト時に自動リトライして
        二重にページを作成してはならない(2026-09-08に実際にこの二重作成が
        発生したことを受けた回帰テスト)。
        """
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError):
            client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert len(httpx_mock.get_requests()) == 1

    def test_create_page_still_retries_on_429(self, httpx_mock: HTTPXMock) -> None:
        """429(rate_limited)はcreate_page()でも従来通りリトライしてよい
        (Notion公式SDKも429を全メソッドでリトライしている)。5xxはこれと異なり、
        create_page()では自動リトライしない(test_create_page_does_not_retry_ambiguous_5xx)。
        """
        httpx_mock.add_response(
            url=f"{API_BASE_URL}/pages", status_code=429, headers={"Retry-After": "0"}
        )
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages", json={"id": "page-1"})

        with NotionClient("secret_test") as client:
            result = client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert result == {"id": "page-1"}
        assert len(httpx_mock.get_requests()) == 2

    def test_create_page_success_sends_one_post(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages", json={"id": "page-1"})

        with NotionClient("secret_test") as client:
            result = client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert result == {"id": "page-1"}
        assert len(httpx_mock.get_requests()) == 1

    def test_create_page_400_raises_once(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages", status_code=400, text="bad")

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError) as info:
            client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert isinstance(info.value.__cause__, httpx.HTTPStatusError)
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    @pytest.mark.parametrize("status_code", [500, 502, 503, 504])
    def test_create_page_does_not_retry_ambiguous_5xx(
        self, httpx_mock: HTTPXMock, status_code: int
    ) -> None:
        """Phase 3F-R1: べき等でないcreate_page()は、サーバー側で実際には作成済みで
        ある可能性を排除できない5xxで自動リトライしてはならない(二重作成防止)。
        2回目の応答として成功を用意しても、POSTは1回しか送られない。
        """
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages", status_code=status_code)
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages", json={"id": "page-2"})

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError) as info:
            client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert isinstance(info.value.__cause__, httpx.HTTPStatusError)
        assert info.value.__cause__.response.status_code == status_code
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    def test_create_page_repeated_503_sends_only_one_post(self, httpx_mock: HTTPXMock) -> None:
        for _ in range(6):
            httpx_mock.add_response(url=f"{API_BASE_URL}/pages", status_code=503)

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError):
            client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.parametrize(
        "exception",
        [
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.ReadError("connection reset"),
        ],
        ids=["remote_protocol_error", "read_error"],
    )
    def test_create_page_transport_error_does_not_retry(
        self, httpx_mock: HTTPXMock, exception: Exception
    ) -> None:
        httpx_mock.add_exception(exception)

        with NotionClient("secret_test") as client, pytest.raises(NotionAPIError) as info:
            client.create_page("ds-1", {"名前": {"title": [{"text": {"content": "x"}}]}})

        assert info.value.__cause__ is exception
        assert len(httpx_mock.get_requests()) == 1

    def test_read_query_still_retries_503(self, httpx_mock: HTTPXMock) -> None:
        url = f"{API_BASE_URL}/data_sources/ds-1/query"
        httpx_mock.add_response(url=url, status_code=503)
        httpx_mock.add_response(url=url, json={"results": [], "has_more": False})

        with NotionClient("secret_test") as client:
            result = client.query_data_source_all("ds-1")

        assert result == []
        assert len(httpx_mock.get_requests()) == 2

    def test_get_still_retries_503(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages/p1", status_code=503)
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages/p1", json={"id": "p1"})

        with NotionClient("secret_test") as client:
            result = client.get_page("p1")

        assert result == {"id": "p1"}
        assert len(httpx_mock.get_requests()) == 2

    def test_update_page_still_retries_503(self, httpx_mock: HTTPXMock) -> None:
        """絶対値を設定するPATCHは、従来通り5xxで自動リトライする(Phase 3F-R1の対象外)。"""
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages/p1", status_code=503)
        httpx_mock.add_response(url=f"{API_BASE_URL}/pages/p1", json={"id": "p1"})

        with NotionClient("secret_test") as client:
            result = client.update_page("p1", {"所持": {"checkbox": True}})

        assert result == {"id": "p1"}
        assert len(httpx_mock.get_requests()) == 2


class TestQueryDataSourceAll:
    def test_paginates_until_has_more_is_false(self, httpx_mock: HTTPXMock) -> None:
        ds_id = "ds-1"
        httpx_mock.add_response(
            url=f"{API_BASE_URL}/data_sources/{ds_id}/query",
            json={
                "results": [{"id": "p1"}, {"id": "p2"}],
                "has_more": True,
                "next_cursor": "cursor-1",
            },
        )
        httpx_mock.add_response(
            url=f"{API_BASE_URL}/data_sources/{ds_id}/query",
            json={"results": [{"id": "p3"}], "has_more": False},
        )

        with NotionClient("secret_test") as client:
            results = client.query_data_source_all(ds_id)

        assert [r["id"] for r in results] == ["p1", "p2", "p3"]


class TestGetPagePropertyItem:
    def test_paginates_relation_items(self, httpx_mock: HTTPXMock) -> None:
        page_id = "page-1"
        property_id = "prop-1"
        httpx_mock.add_response(
            url=httpx.URL(
                f"{API_BASE_URL}/pages/{page_id}/properties/{property_id}"
            ).copy_merge_params({"page_size": "100"}),
            json={
                "results": [{"type": "relation", "relation": {"id": "deck-1"}}],
                "has_more": True,
                "next_cursor": "cursor-1",
            },
        )
        httpx_mock.add_response(
            url=httpx.URL(
                f"{API_BASE_URL}/pages/{page_id}/properties/{property_id}"
            ).copy_merge_params({"page_size": "100", "start_cursor": "cursor-1"}),
            json={
                "results": [{"type": "relation", "relation": {"id": "deck-2"}}],
                "has_more": False,
            },
        )

        with NotionClient("secret_test") as client:
            items = client.get_page_property_item(page_id, property_id)

        assert len(items) == 2
