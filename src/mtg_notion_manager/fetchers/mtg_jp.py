from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from mtg_notion_manager.exceptions import MultipleDecksFoundError, ParseError
from mtg_notion_manager.fetchers.base import BaseFetcher
from mtg_notion_manager.models import RawDeckData

_DECK_HEADING_KAGI_RE = re.compile(r"^「(.+)」$")
_DECK_HEADING_INLINE_COLOR_RE = re.compile(r"^(.+?)（([白青黒赤緑]+)）$")
_SET_TITLE_RE = re.compile(r"『(.+?)』")
_COLOR_CAPTION_RE = re.compile(r"^「(.+?)」\s*（([^）]+)）")
_PLAIN_COLOR_CAPTION_RE = re.compile(r"^(.+?)（([白青黒赤緑]+)）$")
_TABLE_CAPTION_NAME_RE = re.compile(r"^「(.+?)」")


class MtgJpFetcher(BaseFetcher):
    """mtg-jp.com の統率者デッキ・デッキリスト記事用フェッチャー。

    デッキ見出しは2種類の形式がある:
    - 旧形式: <h4>「デッキ名」</h4>(色は別途 <strong>「デッキ名」（赤緑）</strong> から取得)
    - 新形式: <h4>デッキ名（赤緑）</h4>(色を見出し自体から直接取得できる。
      2026年『久遠の終端』記事で確認)
    デッキリスト本体は <table class="decklist"> で形式は変わらない。
    """

    def matches(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("mtg-jp.com")

    def list_deck_names(self, html: str, source_url: str) -> list[str]:
        """記事内の全デッキ名を返す(MultipleDecksFoundErrorは送出しない)。"""
        soup = BeautifulSoup(html, "lxml")
        headings = _find_deck_headings(soup)
        if not headings:
            raise ParseError(
                f"デッキ見出し(「デッキ名」または「デッキ名（色）」形式のh4)が見つかりませんでした: {source_url}"
            )
        known_table_names = _collect_decklist_table_names(soup)
        names = []
        for h4 in headings:
            parsed = _parse_heading_text(h4.get_text(strip=True), known_table_names)
            assert parsed is not None
            names.append(parsed[0])
        return names

    def parse(self, html: str, source_url: str, deck_name: str | None = None) -> RawDeckData:
        soup = BeautifulSoup(html, "lxml")

        deck_tables = soup.find_all("table", class_="decklist")
        if len(deck_tables) == 0:
            raise ParseError(f"デッキリストが見つかりませんでした: {source_url}")

        headings = _find_deck_headings(soup)
        if not headings:
            raise ParseError(
                f"デッキ見出し(「デッキ名」または「デッキ名（色）」形式のh4)が見つかりませんでした: {source_url}"
            )

        known_table_names = _collect_decklist_table_names(soup)
        heading_tag, name, inline_colors = _select_deck_heading(
            headings, deck_name, source_url, known_table_names
        )

        set_name = _extract_set_name(soup, source_url)
        colors_raw = (
            list(inline_colors) if inline_colors else _extract_colors(soup, name, source_url)
        )
        commander = _extract_commander(heading_tag, source_url)

        return RawDeckData(
            name=name,
            commander=commander,
            set_raw=set_name,
            colors_raw=colors_raw,
            source_url=source_url,
        )


def _parse_heading_text(text: str, known_table_names: frozenset[str] = frozenset()) -> tuple[str, str | None] | None:
    """h4見出しテキストを (デッキ名, 見出し内の色文字列またはNone) に分解する。

    - 旧形式「デッキ名」は色情報を含まない(呼び出し側が別途取得する)。
    - 新形式デッキ名（赤緑）は色情報を見出し自体から直接取得できる。
    - 装飾のないデッキ名単独(2024年Fallout記事等)は、同じページ内の
      <table class="decklist"><caption>「デッキ名」...</caption> と完全一致する
      場合に限りデッキ見出しとして認める(無関係な見出しとの誤判定を防ぐため)。
    どれにもマッチしない場合は None を返す(デッキ見出しではない通常のh4)。
    """
    match = _DECK_HEADING_KAGI_RE.match(text)
    if match:
        return match.group(1), None
    match = _DECK_HEADING_INLINE_COLOR_RE.match(text)
    if match:
        return match.group(1), match.group(2)
    if text in known_table_names:
        return text, None
    return None


def _collect_decklist_table_names(soup: BeautifulSoup) -> frozenset[str]:
    names: set[str] = set()
    for table in soup.find_all("table", class_="decklist"):
        caption = table.find("caption")
        if caption is None:
            continue
        match = _TABLE_CAPTION_NAME_RE.match(caption.get_text(strip=True))
        if match:
            names.add(match.group(1))
    return frozenset(names)


def _find_deck_headings(soup: BeautifulSoup) -> list[Tag]:
    known_table_names = _collect_decklist_table_names(soup)
    return [
        h4
        for h4 in soup.find_all("h4")
        if _parse_heading_text(h4.get_text(strip=True), known_table_names)
    ]


def _select_deck_heading(
    headings: list[Tag],
    deck_name: str | None,
    source_url: str,
    known_table_names: frozenset[str] = frozenset(),
) -> tuple[Tag, str, str | None]:
    named: list[tuple[Tag, str, str | None]] = []
    for h4 in headings:
        parsed = _parse_heading_text(h4.get_text(strip=True), known_table_names)
        assert parsed is not None  # _find_deck_headings で既にフィルタ済み
        named.append((h4, parsed[0], parsed[1]))

    if deck_name is not None:
        matched = [(tag, name, colors) for tag, name, colors in named if name == deck_name]
        if not matched:
            available = [name for _, name, _ in named]
            raise ParseError(
                f"指定されたデッキ名 '{deck_name}' が見つかりません。"
                f" 利用可能なデッキ: {available} ({source_url})"
            )
        return matched[0]

    if len(named) > 1:
        available = [name for _, name, _ in named]
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

    # フォールバック: 「デッキ名」（色）形式のキャプションを持たない記事
    # (2024年Fallout記事等)では、色情報が独立した<div>の直下テキストに
    # プレーンテキストで「デッキ名（色）」として書かれていることがある。
    # <strong>で囲まれていないため、末端のテキストノードを直接走査する。
    for node in soup.find_all(string=True):
        text = node.strip()
        match = _PLAIN_COLOR_CAPTION_RE.match(text)
        if match and match.group(1) == deck_name:
            return list(match.group(2))

    raise ParseError(f"デッキ '{deck_name}' の色情報が見つかりませんでした: {source_url}")


def _extract_commander(heading_tag: Tag, source_url: str) -> str:
    card_link = heading_tag.find_next("a", class_="cardPopupLink")
    if card_link is None:
        raise ParseError(f"統率者名を抽出できませんでした: {source_url}")
    commander = card_link.get_text(strip=True)
    if not commander:
        raise ParseError(f"統率者名を抽出できませんでした: {source_url}")
    return commander
