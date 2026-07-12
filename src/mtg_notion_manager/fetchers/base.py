from __future__ import annotations

import time
from abc import ABC, abstractmethod

import httpx

from mtg_notion_manager.exceptions import FetchError
from mtg_notion_manager.models import RawDeckData

USER_AGENT = "mtg-notion-manager/0.1 (+https://github.com/daisukeokb/mtg-notion-manager)"

MAX_RETRIES = 5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class BaseFetcher(ABC):
    """URL判定とページ解析の共通インターフェース。"""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """このフェッチャーが対応するサイトのURLかどうか。"""

    @abstractmethod
    def parse(self, html: str, source_url: str, deck_name: str | None = None) -> RawDeckData:
        """取得済みのHTMLからデッキ情報(マッピング前の生データ)を抽出する。

        1ページに複数デッキが含まれる場合、deck_name で対象を指定する。
        指定がなく複数デッキが見つかった場合は MultipleDecksFoundError を送出する。
        """

    @abstractmethod
    def list_deck_names(self, html: str, source_url: str) -> list[str]:
        """取得済みのHTMLに含まれる全デッキ名を返す(MultipleDecksFoundErrorは送出しない)。"""

    def fetch(self, url: str, deck_name: str | None = None) -> RawDeckData:
        html = download(url)
        return self.parse(html, url, deck_name)


def download(url: str) -> str:
    """ページ本体を取得する。

    429/5xx・タイムアウトは指数バックオフ(Retry-Afterがあれば優先)で
    最大 MAX_RETRIES 回まで自動リトライする(notion/client.pyと同じ方針)。
    """
    attempt = 0
    while True:
        try:
            response = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=15.0,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                time.sleep(_retry_wait_seconds(exc.response, attempt))
                attempt += 1
                continue
            raise FetchError(f"ページの取得に失敗しました: {url} ({exc})") from exc
        except httpx.TimeoutException as exc:
            if attempt < MAX_RETRIES:
                time.sleep(_backoff_seconds(attempt))
                attempt += 1
                continue
            raise FetchError(f"ページの取得がタイムアウトしました: {url} ({exc})") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"ページの取得に失敗しました: {url} ({exc})") from exc
        else:
            return response.text


def _retry_wait_seconds(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return _backoff_seconds(attempt)


def _backoff_seconds(attempt: int) -> float:
    return min(2.0**attempt, 30.0)
