from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from mtg_notion_manager.exceptions import FetchError
from mtg_notion_manager.fetchers.base import download

URL = "https://magic.wizards.com/ja/news/announcements/example"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """リトライの待機を実際には行わずテストを高速化する。"""
    monkeypatch.setattr("mtg_notion_manager.fetchers.base.time.sleep", lambda seconds: None)


class TestDownloadRetry:
    def test_success_returns_text(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=URL, text="<html>ok</html>")

        result = download(URL)

        assert result == "<html>ok</html>"

    def test_429_retries_and_then_succeeds(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "0"})
        httpx_mock.add_response(url=URL, text="<html>ok</html>")

        result = download(URL)

        assert result == "<html>ok</html>"

    def test_500_retries_and_then_succeeds(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=URL, status_code=500)
        httpx_mock.add_response(url=URL, text="<html>ok</html>")

        result = download(URL)

        assert result == "<html>ok</html>"

    def test_timeout_retries_then_succeeds(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
        httpx_mock.add_response(url=URL, text="<html>ok</html>")

        result = download(URL)

        assert result == "<html>ok</html>"

    def test_exceeds_max_retries_raises_fetch_error(self, httpx_mock: HTTPXMock) -> None:
        for _ in range(6):
            httpx_mock.add_response(url=URL, status_code=503)

        with pytest.raises(FetchError):
            download(URL)

    def test_non_retryable_error_raises_immediately(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=URL, status_code=404, text="not found")

        with pytest.raises(FetchError):
            download(URL)

        assert len(httpx_mock.get_requests()) == 1

    def test_retry_after_header_is_respected(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waited: list[float] = []
        monkeypatch.setattr(
            "mtg_notion_manager.fetchers.base.time.sleep", lambda seconds: waited.append(seconds)
        )
        httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "3"})
        httpx_mock.add_response(url=URL, text="<html>ok</html>")

        download(URL)

        assert waited == [3.0]
