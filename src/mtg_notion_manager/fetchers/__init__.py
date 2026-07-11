from __future__ import annotations

from mtg_notion_manager.exceptions import UnsupportedSourceError
from mtg_notion_manager.fetchers.base import BaseFetcher
from mtg_notion_manager.fetchers.mtg_jp import MtgJpFetcher
from mtg_notion_manager.fetchers.wizards_official import WizardsOfficialFetcher

_FETCHERS: list[BaseFetcher] = [WizardsOfficialFetcher(), MtgJpFetcher()]


def get_fetcher(url: str) -> BaseFetcher:
    for fetcher in _FETCHERS:
        if fetcher.matches(url):
            return fetcher
    raise UnsupportedSourceError(
        f"対応していないサイトです: {url}"
        " (magic.wizards.com または mtg-jp.com のURLを指定してください)"
    )


__all__ = ["BaseFetcher", "WizardsOfficialFetcher", "MtgJpFetcher", "get_fetcher"]
