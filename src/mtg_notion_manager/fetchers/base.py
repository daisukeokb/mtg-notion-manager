from abc import ABC, abstractmethod

import httpx

from mtg_notion_manager.exceptions import FetchError
from mtg_notion_manager.models import RawDeckData

USER_AGENT = "mtg-notion-manager/0.1 (+https://github.com/daisukeokb/mtg-notion-manager)"


class BaseFetcher(ABC):
    """URL判定とページ解析の共通インターフェース。"""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """このフェッチャーが対応するサイトのURLかどうか。"""

    @abstractmethod
    def parse(self, html: str, source_url: str) -> RawDeckData:
        """取得済みのHTMLからデッキ情報(マッピング前の生データ)を抽出する。"""

    def fetch(self, url: str) -> RawDeckData:
        html = download(url)
        return self.parse(html, url)


def download(url: str) -> str:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FetchError(f"ページの取得に失敗しました: {url} ({exc})") from exc
    return response.text
