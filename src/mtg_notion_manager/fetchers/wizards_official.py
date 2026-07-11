from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from mtg_notion_manager.exceptions import MultipleDecksFoundError, ParseError
from mtg_notion_manager.fetchers.base import BaseFetcher
from mtg_notion_manager.models import RawDeckData

_LEADING_COUNT_RE = re.compile(r"^\d+\s+")
_TRAILING_CARD_ID_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


class WizardsOfficialFetcher(BaseFetcher):
    """magic.wizards.com のCommander Decklists記事用フェッチャー。

    ページは <deck-list set="XXX" deck-title="..."><main-deck>...</main-deck></deck-list>
    という機械可読タグでデッキリストを保持している。色はページ上部の
    <figcaption>デッキ名 (Red-Green)</figcaption> のような記載から取得する。
    """

    def matches(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("magic.wizards.com")

    def parse(self, html: str, source_url: str) -> RawDeckData:
        soup = BeautifulSoup(html, "lxml")

        deck_list_tags = soup.find_all("deck-list")
        if len(deck_list_tags) == 0:
            raise ParseError(f"デッキリストが見つかりませんでした: {source_url}")
        if len(deck_list_tags) > 1:
            deck_names = [tag.get("deck-title", "?") for tag in deck_list_tags]
            raise MultipleDecksFoundError(
                f"このページには複数のデッキが含まれています({', '.join(deck_names)})。"
                " 現バージョンは1ページ1デッキの記事のみ対応しています。"
            )

        deck_tag = deck_list_tags[0]
        name = (deck_tag.get("deck-title") or "").strip()
        set_code = (deck_tag.get("set") or "").strip()
        if not name or not set_code:
            raise ParseError(
                f"deck-list タグに deck-title または set 属性がありません: {source_url}"
            )

        main_deck = deck_tag.find("main-deck")
        if main_deck is None:
            raise ParseError(f"main-deck が見つかりませんでした: {source_url}")

        lines = [line.strip() for line in main_deck.get_text().splitlines() if line.strip()]
        if not lines:
            raise ParseError(f"デッキリストの中身が空です: {source_url}")

        commander = _TRAILING_CARD_ID_RE.sub("", _LEADING_COUNT_RE.sub("", lines[0])).strip()
        if not commander:
            raise ParseError(f"統率者名を抽出できませんでした: {source_url}")

        colors_raw = _extract_colors(soup, name, source_url)

        return RawDeckData(
            name=name,
            commander=commander,
            set_raw=set_code,
            colors_raw=colors_raw,
            source_url=source_url,
        )


def _extract_colors(soup: BeautifulSoup, deck_name: str, source_url: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(deck_name)}\s*\(([^)]+)\)")
    for figcaption in soup.find_all("figcaption"):
        text = figcaption.get_text(strip=True)
        match = pattern.match(text)
        if match:
            colors_text = match.group(1)
            return [token.strip() for token in re.split(r"[-/,]", colors_text) if token.strip()]

    raise ParseError(
        f"デッキ '{deck_name}' の色情報が見つかりませんでした: {source_url}"
    )
