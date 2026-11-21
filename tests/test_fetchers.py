from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import (
    MultipleDecksFoundError,
    ParseError,
    UnsupportedSourceError,
)
from mtg_notion_manager.fetchers import get_fetcher
from mtg_notion_manager.fetchers.mtg_jp import MtgJpFetcher
from mtg_notion_manager.fetchers.wizards_official import WizardsOfficialFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class TestWizardsOfficialFetcher:
    def test_matches_wizards_domain(self) -> None:
        fetcher = WizardsOfficialFetcher()
        assert fetcher.matches(
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists"
        )
        assert not fetcher.matches("https://mtg-jp.com/reading/publicity/0038046/")

    def test_parse_single_deck(self) -> None:
        html = _read_fixture("wizards_single_deck.html")
        fetcher = WizardsOfficialFetcher()
        result = fetcher.parse(
            html, "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists"
        )

        assert result.name == "Animated Army"
        assert result.commander == "Bello, Bard of the Brambles"
        assert result.set_raw == "BLB"
        assert result.colors_raw == ["Red", "Green"]

    def test_parse_multi_deck_raises(self) -> None:
        html = _read_fixture("wizards_multi_deck.html")
        fetcher = WizardsOfficialFetcher()
        with pytest.raises(MultipleDecksFoundError):
            fetcher.parse(
                html,
                "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
            )

    def test_parse_multi_deck_with_deck_name_selects_target(self) -> None:
        html = _read_fixture("wizards_multi_deck.html")
        fetcher = WizardsOfficialFetcher()
        result = fetcher.parse(
            html,
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
            deck_name="Family Matters",
        )

        assert result.name == "Family Matters"
        assert result.commander == "Zinnia, Valley's Voice"
        assert result.set_raw == "BLB"
        assert result.colors_raw == ["Blue", "Red", "White"]

    def test_parse_multi_deck_with_unknown_deck_name_raises(self) -> None:
        html = _read_fixture("wizards_multi_deck.html")
        fetcher = WizardsOfficialFetcher()
        with pytest.raises(ParseError):
            fetcher.parse(
                html,
                "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
                deck_name="存在しないデッキ",
            )

    def test_list_deck_names_returns_all_without_raising(self) -> None:
        html = _read_fixture("wizards_multi_deck.html")
        fetcher = WizardsOfficialFetcher()

        names = fetcher.list_deck_names(
            html,
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
        )

        assert names == ["Animated Army", "Family Matters"]

    def test_list_deck_names_single_deck(self) -> None:
        html = _read_fixture("wizards_single_deck.html")
        fetcher = WizardsOfficialFetcher()

        names = fetcher.list_deck_names(
            html,
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
        )

        assert names == ["Animated Army"]


class TestWizardsJapaneseArticleWithoutSetAttribute:
    """set属性・figcaptionのない日本語版Wizards記事(2026-11『スター・トレック』)。"""

    URL = "https://magic.wizards.com/ja/news/announcements/star-trek-commander-decklists"

    def test_set_from_title_and_colors_from_table_of_contents(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html")
        result = WizardsOfficialFetcher().parse(html, self.URL, deck_name="惑星連邦宇宙艦隊")

        assert result.name == "惑星連邦宇宙艦隊"
        assert result.commander == "Jean-Luc Picard, Luminary"
        assert result.set_raw == "マジック：ザ・ギャザリング | スター・トレック™"
        assert result.colors_raw == ["青", "赤", "白"]

    def test_second_deck_uses_its_own_colors(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html")
        result = WizardsOfficialFetcher().parse(html, self.URL, deck_name="我々はボーグ")

        assert result.commander == "The Borg Queen"
        assert result.colors_raw == ["白", "青", "黒"]

    def test_set_attribute_still_takes_precedence(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html").replace(
            'deck-title="我々はボーグ"', 'deck-title="我々はボーグ" set="TRK"'
        )
        result = WizardsOfficialFetcher().parse(html, self.URL, deck_name="我々はボーグ")

        assert result.set_raw == "TRK"

    def test_missing_title_and_set_attribute_raises(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html").replace("『", "").replace("』", "")
        with pytest.raises(ParseError):
            WizardsOfficialFetcher().parse(html, self.URL, deck_name="惑星連邦宇宙艦隊")

    def test_missing_colors_raises(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html").replace("（青赤白）", "")
        with pytest.raises(ParseError):
            WizardsOfficialFetcher().parse(html, self.URL, deck_name="惑星連邦宇宙艦隊")

    def test_conflicting_colors_raise(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html").replace(
            "<ul>", "<ul><li>惑星連邦宇宙艦隊（赤緑）</li>", 1
        )
        with pytest.raises(ParseError):
            WizardsOfficialFetcher().parse(html, self.URL, deck_name="惑星連邦宇宙艦隊")

    def test_non_color_text_in_parentheses_is_ignored(self) -> None:
        html = _read_fixture("wizards_ja_no_set_attr.html").replace(
            "（青赤白）", "（新規カード32枚）"
        )
        with pytest.raises(ParseError):
            WizardsOfficialFetcher().parse(html, self.URL, deck_name="惑星連邦宇宙艦隊")


class TestMtgJpFetcher:
    def test_matches_mtgjp_domain(self) -> None:
        fetcher = MtgJpFetcher()
        assert fetcher.matches("https://mtg-jp.com/reading/publicity/0038046/")
        assert not fetcher.matches(
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists"
        )

    def test_parse_single_deck(self) -> None:
        html = _read_fixture("mtgjp_single_deck.html")
        fetcher = MtgJpFetcher()
        result = fetcher.parse(html, "https://mtg-jp.com/reading/publicity/0038046/")

        assert result.name == "動き出した兵隊"
        assert result.commander == "茨の吟遊詩人、べロ"
        assert result.set_raw == "ブルームバロウ"
        assert result.colors_raw == ["赤", "緑"]

    def test_parse_multi_deck_raises(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        fetcher = MtgJpFetcher()
        with pytest.raises(MultipleDecksFoundError):
            fetcher.parse(html, "https://mtg-jp.com/reading/publicity/0038046/")

    def test_parse_multi_deck_with_deck_name_selects_target(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        fetcher = MtgJpFetcher()
        result = fetcher.parse(
            html,
            "https://mtg-jp.com/reading/publicity/0038046/",
            deck_name="家族が第一",
        )

        assert result.name == "家族が第一"
        assert result.commander == "渓間の声、ジニア"
        assert result.set_raw == "ブルームバロウ"
        assert result.colors_raw == ["青", "赤", "白"]

    def test_parse_multi_deck_with_unknown_deck_name_raises(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        fetcher = MtgJpFetcher()
        with pytest.raises(ParseError):
            fetcher.parse(
                html,
                "https://mtg-jp.com/reading/publicity/0038046/",
                deck_name="存在しないデッキ",
            )

    def test_list_deck_names_returns_all_without_raising(self) -> None:
        html = _read_fixture("mtgjp_multi_deck.html")
        fetcher = MtgJpFetcher()

        names = fetcher.list_deck_names(html, "https://mtg-jp.com/reading/publicity/0038046/")

        assert names == ["動き出した兵隊", "家族が第一"]

    def test_list_deck_names_single_deck(self) -> None:
        html = _read_fixture("mtgjp_single_deck.html")
        fetcher = MtgJpFetcher()

        names = fetcher.list_deck_names(html, "https://mtg-jp.com/reading/publicity/0038046/")

        assert names == ["動き出した兵隊"]


class TestGetFetcher:
    def test_routes_to_correct_fetcher(self) -> None:
        assert isinstance(
            get_fetcher("https://magic.wizards.com/en/news/announcements/x"),
            WizardsOfficialFetcher,
        )
        assert isinstance(
            get_fetcher("https://mtg-jp.com/reading/publicity/0038046/"),
            MtgJpFetcher,
        )

    def test_unsupported_source_raises(self) -> None:
        with pytest.raises(UnsupportedSourceError):
            get_fetcher("https://example.com/some-page")
