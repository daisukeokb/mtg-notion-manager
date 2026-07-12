from __future__ import annotations

from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import (
    DeckCountMismatchError,
    MultipleDecksFoundError,
    ParseError,
)
from mtg_notion_manager.parsers import decklist

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class TestParseMtgJpDecklist:
    def test_extracts_100_cards(self) -> None:
        html = _read_fixture("mtgjp_full_decklist_100.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0035593/", deck_name="吸血鬼の血統"
        )

        assert parsed.deck_name == "吸血鬼の血統"
        assert parsed.total_quantity == 100

    def test_commander_is_included_and_flagged(self) -> None:
        html = _read_fixture("mtgjp_full_decklist_100.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0035593/", deck_name="吸血鬼の血統"
        )

        commander_cards = [c for c in parsed.cards if c.is_commander]
        assert len(commander_cards) == 1
        assert commander_cards[0].name_ja == "マウアーの太祖、ストレイファン"
        assert parsed.commander_name == "マウアーの太祖、ストレイファン"

    def test_basic_land_quantities_are_correct(self) -> None:
        html = _read_fixture("mtgjp_full_decklist_100.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0035593/", deck_name="吸血鬼の血統"
        )

        by_name = {c.name_ja: c.quantity for c in parsed.cards}
        assert by_name["沼"] == 14
        assert by_name["山"] == 11

    def test_category_headings_are_not_treated_as_cards(self) -> None:
        html = _read_fixture("mtgjp_full_decklist_100.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0035593/", deck_name="吸血鬼の血統"
        )

        names = {c.name_ja for c in parsed.cards}
        for forbidden in ("統率者", "土地", "クリーチャー", "呪文"):
            assert forbidden not in names

    def test_selects_deck_by_name_from_multi_deck_page(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0038046/", deck_name="家族が第一"
        )

        assert parsed.deck_name == "家族が第一"
        assert parsed.commander_name == "渓間の声、ジニア"

    def test_unknown_deck_name_raises(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        with pytest.raises(ParseError):
            decklist.parse_mtg_jp_decklist(
                html, "https://mtg-jp.com/reading/publicity/0038046/", deck_name="存在しないデッキ"
            )

    def test_missing_deck_name_on_multi_deck_page_raises(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        with pytest.raises(MultipleDecksFoundError):
            decklist.parse_mtg_jp_decklist(html, "https://mtg-jp.com/reading/publicity/0038046/")


class TestParseWizardsDecklist:
    def test_extracts_cards_with_english_names(self) -> None:
        html = _read_fixture("wizards_single_deck.html")
        parsed = decklist.parse_wizards_decklist(
            html, "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists"
        )

        assert parsed.commander_name == "Bello, Bard of the Brambles"
        by_name = {c.name_en: c.quantity for c in parsed.cards}
        assert by_name["Mountain"] == 8
        assert by_name["Forest"] == 10
        commander_cards = [c for c in parsed.cards if c.is_commander]
        assert len(commander_cards) == 1

    def test_selects_deck_by_name_from_multi_deck_page(self) -> None:
        html = _read_fixture("wizards_multi_deck.html")
        parsed = decklist.parse_wizards_decklist(
            html,
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
            deck_name="Family Matters",
        )

        assert parsed.deck_name == "Family Matters"
        assert parsed.commander_name == "Zinnia, Valley's Voice"


class TestValidateDeckCount:
    def test_passes_for_exactly_100(self) -> None:
        html = _read_fixture("mtgjp_full_decklist_100.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0035593/", deck_name="吸血鬼の血統"
        )
        decklist.validate_deck_count(parsed)  # should not raise

    def test_raises_when_under_100(self) -> None:
        html = _read_fixture("mtgjp_single_deck.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0038046/"
        )

        with pytest.raises(DeckCountMismatchError):
            decklist.validate_deck_count(parsed)

    def test_allow_mismatch_bypasses_error(self) -> None:
        html = _read_fixture("mtgjp_single_deck.html")
        parsed = decklist.parse_mtg_jp_decklist(
            html, "https://mtg-jp.com/reading/publicity/0038046/"
        )

        decklist.validate_deck_count(parsed, allow_mismatch=True)  # should not raise


class TestAggregateCards:
    def test_duplicate_names_are_summed(self) -> None:
        entries = [
            ("山", None, 5, False),
            ("沼", None, 2, False),
            ("山", None, 3, False),
        ]
        cards = decklist._aggregate_cards(entries, "https://example.com")

        by_name = {c.name_ja: c.quantity for c in cards}
        assert by_name["山"] == 8
        assert by_name["沼"] == 2
        # 順序は初出順を維持する
        assert [c.name_ja for c in cards] == ["山", "沼"]

    def test_commander_flag_is_preserved_on_merge(self) -> None:
        entries = [
            ("統率者カード", None, 1, True),
            ("統率者カード", None, 1, False),
        ]
        cards = decklist._aggregate_cards(entries, "https://example.com")

        assert len(cards) == 1
        assert cards[0].quantity == 2
        assert cards[0].is_commander is True
