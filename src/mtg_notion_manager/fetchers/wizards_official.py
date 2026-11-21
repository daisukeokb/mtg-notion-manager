from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from mtg_notion_manager.exceptions import MultipleDecksFoundError, ParseError
from mtg_notion_manager.fetchers.base import BaseFetcher
from mtg_notion_manager.models import RawDeckData

_LEADING_COUNT_RE = re.compile(r"^\d+\s+")
_SET_TITLE_RE = re.compile(r"『(.+?)』")
# 日本語版記事の目次「デッキ名（青赤白）」の色部分。全角括弧のみを対象とする。
_JA_COLOR_TEXT_RE = re.compile(r"^(?:無色|[白青黒赤緑]+)$")
_TRAILING_CARD_ID_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def _attr(tag: Tag, name: str) -> str:
    """タグの属性値を文字列として取得する(bs4は複数値属性をlistで返しうるため正規化する)。"""
    value = tag.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return " ".join(value)


class WizardsOfficialFetcher(BaseFetcher):
    """magic.wizards.com のCommander Decklists記事用フェッチャー。

    ページは <deck-list set="XXX" deck-title="..."><main-deck>...</main-deck></deck-list>
    という機械可読タグでデッキリストを保持している。色はページ上部の
    <figcaption>デッキ名 (Red-Green)</figcaption> のような記載から取得する。

    2026年11月の『スター・トレック』記事のように、deck-list タグに set 属性がなく
    figcaption も存在しない日本語版記事がある。その場合に限り、発売セットは
    記事タイトル(<h1> または <title>)の『』内、色は目次等の
    「デッキ名（青赤白）」という記載から取得する(set 属性があれば従来どおりそちらを優先)。
    """

    def matches(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("magic.wizards.com")

    def list_deck_names(self, html: str, source_url: str) -> list[str]:
        """記事内の全デッキ名を返す(MultipleDecksFoundErrorは送出しない)。"""
        soup = BeautifulSoup(html, "lxml")
        deck_list_tags = soup.find_all("deck-list")
        if len(deck_list_tags) == 0:
            raise ParseError(f"デッキリストが見つかりませんでした: {source_url}")
        return [_attr(tag, "deck-title").strip() for tag in deck_list_tags]

    def parse(self, html: str, source_url: str, deck_name: str | None = None) -> RawDeckData:
        soup = BeautifulSoup(html, "lxml")

        deck_list_tags = soup.find_all("deck-list")
        if len(deck_list_tags) == 0:
            raise ParseError(f"デッキリストが見つかりませんでした: {source_url}")

        deck_tag = _select_deck_tag(deck_list_tags, deck_name, source_url)
        name = _attr(deck_tag, "deck-title").strip()
        if not name:
            raise ParseError(f"deck-list タグに deck-title 属性がありません: {source_url}")
        set_code = _attr(deck_tag, "set").strip() or _extract_set_title(soup, source_url)

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


def _select_deck_tag(deck_list_tags: list[Tag], deck_name: str | None, source_url: str) -> Tag:
    if deck_name is not None:
        matched = [tag for tag in deck_list_tags if _attr(tag, "deck-title") == deck_name]
        if not matched:
            available = [_attr(tag, "deck-title") or "?" for tag in deck_list_tags]
            raise ParseError(
                f"指定されたデッキ名 '{deck_name}' が見つかりません。"
                f" 利用可能なデッキ: {available} ({source_url})"
            )
        return matched[0]

    if len(deck_list_tags) > 1:
        deck_names = [_attr(tag, "deck-title") or "?" for tag in deck_list_tags]
        raise MultipleDecksFoundError(
            f"このページには複数のデッキが含まれています({', '.join(deck_names)})。"
            " --deck-name オプションで対象デッキ名を指定してください。"
        )

    return deck_list_tags[0]


def _extract_set_title(soup: BeautifulSoup, source_url: str) -> str:
    """set 属性がない場合に、記事タイトルの『』内をセット名の生値として返す。"""
    candidates = [h1.get_text(strip=True) for h1 in soup.find_all("h1")]
    if soup.title is not None:
        candidates.append(soup.title.get_text(strip=True))
    for text in candidates:
        match = _SET_TITLE_RE.search(text)
        if match:
            return match.group(1).strip()
    raise ParseError(
        "deck-list タグに set 属性がなく、記事タイトルから『セット名』も取得できませんでした:"
        f" {source_url}"
    )


def _extract_colors(soup: BeautifulSoup, deck_name: str, source_url: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(deck_name)}\s*\(([^)]+)\)")
    for figcaption in soup.find_all("figcaption"):
        text = figcaption.get_text(strip=True)
        match = pattern.match(text)
        if match:
            colors_text = match.group(1)
            return [token.strip() for token in re.split(r"[-/,]", colors_text) if token.strip()]

    # 日本語版記事: 「デッキ名（青赤白）」(全角括弧、色は日本語の1文字表記の連結)。
    ja_pattern = re.compile(rf"^\s*{re.escape(deck_name)}\s*（([^）]+)）\s*$")
    found: set[tuple[str, ...]] = set()
    for element in soup.find_all(["a", "li", "strong", "p", "h2", "h3"]):
        match = ja_pattern.match(element.get_text(strip=True))
        if match and _JA_COLOR_TEXT_RE.match(match.group(1)):
            colors_text = match.group(1)
            found.add((colors_text,) if colors_text == "無色" else tuple(colors_text))
    if len(found) == 1:
        return list(next(iter(found)))
    if len(found) > 1:
        raise ParseError(
            f"デッキ '{deck_name}' の色情報が複数箇所で食い違っています({sorted(found)}):"
            f" {source_url}"
        )

    raise ParseError(f"デッキ '{deck_name}' の色情報が見つかりませんでした: {source_url}")
