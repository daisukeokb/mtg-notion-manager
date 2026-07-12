from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from mtg_notion_manager.exceptions import MultipleDecksFoundError, ParseError
from mtg_notion_manager.fetchers.base import BaseFetcher
from mtg_notion_manager.models import RawDeckData

_DECK_HEADING_RE = re.compile(r"^「(.+)」$")
_SET_TITLE_RE = re.compile(r"『(.+?)』")
_COLOR_CAPTION_RE = re.compile(r"^「(.+?)」\s*（([^）]+)）")


class MtgJpFetcher(BaseFetcher):
    """mtg-jp.com の統率者デッキ・デッキリスト記事用フェッチャー。

    デッキ見出しは <h4>「デッキ名」</h4>、デッキリスト本体は
    <table class="decklist">。色はページ上部の
    <strong>「デッキ名」（赤緑）</strong> のような記載から取得する。
    """

    def matches(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("mtg-jp.com")

    def list_deck_names(self, html: str, source_url: str) -> list[str]:
        """記事内の全デッキ名を返す(MultipleDecksFoundErrorは送出しない)。"""
        soup = BeautifulSoup(html, "lxml")
        headings = _find_deck_headings(soup)
        if not headings:
            raise ParseError(
                f"デッキ見出し(「デッキ名」形式のh4)が見つかりませんでした: {source_url}"
            )
        names = []
        for h4 in headings:
            match = _DECK_HEADING_RE.match(h4.get_text(strip=True))
            assert match is not None
            names.append(match.group(1))
        return names

    def parse(self, html: str, source_url: str, deck_name: str | None = None) -> RawDeckData:
        soup = BeautifulSoup(html, "lxml")

        deck_tables = soup.find_all("table", class_="decklist")
        if len(deck_tables) == 0:
            raise ParseError(f"デッキリストが見つかりませんでした: {source_url}")

        headings = _find_deck_headings(soup)
        if not headings:
            raise ParseError(
                f"デッキ見出し(「デッキ名」形式のh4)が見つかりませんでした: {source_url}"
            )

        heading_tag, name = _select_deck_heading(headings, deck_name, source_url)

        set_name = _extract_set_name(soup, source_url)
        colors_raw = _extract_colors(soup, name, source_url)
        commander = _extract_commander(heading_tag, source_url)

        return RawDeckData(
            name=name,
            commander=commander,
            set_raw=set_name,
            colors_raw=colors_raw,
            source_url=source_url,
        )


def _find_deck_headings(soup: BeautifulSoup) -> list[Tag]:
    return [h4 for h4 in soup.find_all("h4") if _DECK_HEADING_RE.match(h4.get_text(strip=True))]


def _select_deck_heading(
    headings: list[Tag], deck_name: str | None, source_url: str
) -> tuple[Tag, str]:
    named: list[tuple[Tag, str]] = []
    for h4 in headings:
        match = _DECK_HEADING_RE.match(h4.get_text(strip=True))
        assert match is not None  # _find_deck_headings で既にフィルタ済み
        named.append((h4, match.group(1)))

    if deck_name is not None:
        matched = [(tag, name) for tag, name in named if name == deck_name]
        if not matched:
            available = [name for _, name in named]
            raise ParseError(
                f"指定されたデッキ名 '{deck_name}' が見つかりません。"
                f" 利用可能なデッキ: {available} ({source_url})"
            )
        return matched[0]

    if len(named) > 1:
        available = [name for _, name in named]
        raise MultipleDecksFoundError(
            f"このページには複数({len(named)}個)のデッキリストが含まれています"
            f"({', '.join(available)})。"
            " --deck-name オプションで対象デッキ名を指定してください。"
        )

    return named[0]


def _extract_set_name(soup: BeautifulSoup, source_url: str) -> str:
    for h1 in soup.find_all("h1"):
        match = _SET_TITLE_RE.search(h1.get_text(strip=True))
        if match:
            return match.group(1)
    raise ParseError(f"発売セット名(『セット名』形式の見出し)が見つかりませんでした: {source_url}")


def _extract_colors(soup: BeautifulSoup, deck_name: str, source_url: str) -> list[str]:
    for strong in soup.find_all("strong"):
        text = strong.get_text(strip=True)
        match = _COLOR_CAPTION_RE.match(text)
        if match and match.group(1) == deck_name:
            colors_text = match.group(2)
            return list(colors_text)  # 「赤緑」→["赤", "緑"]

    raise ParseError(f"デッキ '{deck_name}' の色情報が見つかりませんでした: {source_url}")


def _extract_commander(heading_tag: Tag, source_url: str) -> str:
    card_link = heading_tag.find_next("a", class_="cardPopupLink")
    if card_link is None:
        raise ParseError(f"統率者名を抽出できませんでした: {source_url}")
    commander = card_link.get_text(strip=True)
    if not commander:
        raise ParseError(f"統率者名を抽出できませんでした: {source_url}")
    return commander
