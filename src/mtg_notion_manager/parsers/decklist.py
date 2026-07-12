"""デッキリスト本体(カード100枚)の抽出。

デッキ概要(名前・統率者名の1件・発売セット・色)は fetchers/ が担当し、
こちらはカード1枚ごとの名前・枚数の抽出という別責務を持つ。
デッキ見出し/deck-listタグの選択ロジックは fetchers/ の実装を再利用する
(同じHTML構造の解釈を重複させないため)。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from mtg_notion_manager.exceptions import (
    DeckCountMismatchError,
    ParseError,
    UnsupportedSourceError,
)
from mtg_notion_manager.fetchers.base import download
from mtg_notion_manager.fetchers.mtg_jp import (
    _extract_commander,
    _find_deck_headings,
    _select_deck_heading,
)
from mtg_notion_manager.fetchers.wizards_official import _attr, _select_deck_tag
from mtg_notion_manager.models import DeckCard, ParsedDeckList
from mtg_notion_manager.parsers.card_names import normalize_card_name

_WIZARDS_LINE_RE = re.compile(r"^(\d+)\s+(.+?)\s*(?:\[[^\]]*\])?\s*$")

COMMANDER_DECK_SIZE = 100


def parse_decklist(url: str, deck_name: str | None = None) -> ParsedDeckList:
    """URLからデッキ概要のURL判定を行い、対応するサイトのパーサーへ振り分ける。"""
    html = download(url)
    netloc = urlparse(url).netloc
    if netloc.endswith("mtg-jp.com"):
        return parse_mtg_jp_decklist(html, url, deck_name)
    if netloc.endswith("magic.wizards.com"):
        return parse_wizards_decklist(html, url, deck_name)
    raise UnsupportedSourceError(
        f"対応していないサイトです: {url}"
        " (magic.wizards.com または mtg-jp.com のURLを指定してください)"
    )


def parse_mtg_jp_decklist(
    html: str, source_url: str, deck_name: str | None = None
) -> ParsedDeckList:
    soup = BeautifulSoup(html, "lxml")

    headings = _find_deck_headings(soup)
    if not headings:
        raise ParseError(f"デッキ見出し(「デッキ名」形式のh4)が見つかりませんでした: {source_url}")
    heading_tag, name = _select_deck_heading(headings, deck_name, source_url)

    table = heading_tag.find_next("table", class_="decklist")
    if table is None:
        raise ParseError(f"デッキリストの表が見つかりませんでした: {source_url}")

    commander_name = _extract_commander(heading_tag, source_url)
    cards = _extract_mtg_jp_cards(table, commander_name, source_url)
    _ensure_commander_present(cards, commander_name, source_url)

    return ParsedDeckList(
        deck_name=name, commander_name=commander_name, cards=cards, source_url=source_url
    )


def parse_wizards_decklist(
    html: str, source_url: str, deck_name: str | None = None
) -> ParsedDeckList:
    soup = BeautifulSoup(html, "lxml")

    deck_list_tags = soup.find_all("deck-list")
    if not deck_list_tags:
        raise ParseError(f"デッキリストが見つかりませんでした: {source_url}")

    deck_tag = _select_deck_tag(deck_list_tags, deck_name, source_url)
    name = _attr(deck_tag, "deck-title").strip()

    main_deck = deck_tag.find("main-deck")
    if main_deck is None:
        raise ParseError(f"main-deck が見つかりませんでした: {source_url}")

    lines = [line.strip() for line in main_deck.get_text().splitlines() if line.strip()]
    if not lines:
        raise ParseError(f"デッキリストの中身が空です: {source_url}")

    entries: list[tuple[str | None, str | None, int, bool]] = []
    for index, line in enumerate(lines):
        quantity, card_name = _parse_wizards_line(line, source_url)
        entries.append((None, card_name, quantity, index == 0))

    commander_name = entries[0][1]
    assert commander_name is not None
    cards = _aggregate_cards(entries, source_url)
    _ensure_commander_present(cards, commander_name, source_url)

    return ParsedDeckList(
        deck_name=name, commander_name=commander_name, cards=cards, source_url=source_url
    )


def validate_deck_count(
    parsed: ParsedDeckList, expected: int = COMMANDER_DECK_SIZE, allow_mismatch: bool = False
) -> None:
    """デッキ合計枚数を検証する。既定では100枚以外を許可しない。"""
    total = parsed.total_quantity
    if total != expected and not allow_mismatch:
        raise DeckCountMismatchError(
            f"デッキ '{parsed.deck_name}' の合計枚数が{expected}枚と一致しません"
            f"(実際: {total}枚)。 --allow-count-mismatch を指定すると続行できます。"
            f" ({parsed.source_url})"
        )


def _extract_mtg_jp_cards(table: Tag, commander_name: str, source_url: str) -> list[DeckCard]:
    entries: list[tuple[str | None, str | None, int, bool]] = []
    for anchor in table.find_all("a", class_="cardPopupLink"):
        name_ja = anchor.get_text(strip=True)
        if not name_ja:
            continue
        quantity = _extract_preceding_quantity(anchor, source_url)
        entries.append((name_ja, None, quantity, name_ja == commander_name))
    return _aggregate_cards(entries, source_url)


def _extract_preceding_quantity(anchor: Tag, source_url: str) -> int:
    preceding = anchor.previous_sibling
    text = str(preceding) if preceding is not None else ""
    match = re.search(r"(\d+)\s*《?\s*$", text)
    if match is None:
        raise ParseError(
            f"カード '{anchor.get_text(strip=True)}' の枚数を抽出できませんでした: {source_url}"
        )
    return int(match.group(1))


def _parse_wizards_line(line: str, source_url: str) -> tuple[int, str]:
    match = _WIZARDS_LINE_RE.match(line)
    if match is None:
        raise ParseError(f"デッキリストの行を解析できませんでした: '{line}' ({source_url})")
    return int(match.group(1)), match.group(2).strip()


def _aggregate_cards(
    entries: list[tuple[str | None, str | None, int, bool]], source_url: str
) -> list[DeckCard]:
    """同名カード(基本土地など)が複数エントリに分かれている場合に枚数を合算する。"""
    aggregated: dict[str, DeckCard] = {}
    order: list[str] = []

    for name_ja, name_en, quantity, is_commander in entries:
        key = normalize_card_name(name_en or name_ja or "")
        if key in aggregated:
            existing = aggregated[key]
            aggregated[key] = DeckCard(
                name_ja=existing.name_ja or name_ja,
                name_en=existing.name_en or name_en,
                quantity=existing.quantity + quantity,
                is_commander=existing.is_commander or is_commander,
                source_url=source_url,
            )
        else:
            aggregated[key] = DeckCard(
                name_ja=name_ja,
                name_en=name_en,
                quantity=quantity,
                is_commander=is_commander,
                source_url=source_url,
            )
            order.append(key)

    return [aggregated[key] for key in order]


def _ensure_commander_present(cards: list[DeckCard], commander_name: str, source_url: str) -> None:
    if not any(card.is_commander for card in cards):
        raise ParseError(
            f"統率者 '{commander_name}' がカードリスト内に見つかりませんでした: {source_url}"
        )
