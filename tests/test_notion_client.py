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
