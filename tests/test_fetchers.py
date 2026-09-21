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

    def test_parse_falls_back_to_japanese_figcaption_by_position(self) -> None:
        # 2026年時点のマーベル記事等ではfigcaptionが日本語(「デッキ名」（色）形式)に
        # 変わっており、英語のdeck-title属性とはテキストが一致しない。
        # この場合、deck-listタグとfigcaptionの出現順が対応している前提で
        # 位置ベースにフォールバックする(回帰テスト)。
        html = _read_fixture("wizards_multi_deck_ja_figcaption.html")
        fetcher = WizardsOfficialFetcher()
        result = fetcher.parse(
            html,
            "https://magic.wizards.com/ja/news/announcements/bloomburrow-commander-decklists",
            deck_name="Animated Army",
        )

        assert result.name == "Animated Army"
        assert result.commander == "Bello, Bard of the Brambles"
        assert result.colors_raw == ["赤", "緑"]

    def test_parse_japanese_figcaption_second_deck_by_position(self) -> None:
        html = _read_fixture("wizards_multi_deck_ja_figcaption.html")
        fetcher = WizardsOfficialFetcher()
        result = fetcher.parse(
            html,
            "https://magic.wizards.com/ja/news/announcements/bloomburrow-commander-decklists",
            deck_name="Family Matters",
        )

        assert result.colors_raw == ["青", "赤", "白"]

    def test_list_deck_names_single_deck(self) -> None:
        html = _read_fixture("wizards_single_deck.html")
        fetcher = WizardsOfficialFetcher()

        names = fetcher.list_deck_names(
            html,
            "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists",
        )

        assert names == ["Animated Army"]


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

    def test_parse_inline_color_heading_format(self) -> None:
        # 2026年『久遠の終端』記事以降、見出しが「デッキ名」単独ではなく
        # デッキ名（色）の新形式に変わったケースの回帰テスト。
        html = _read_fixture("mtgjp_single_deck_inline_color_heading.html")
        fetcher = MtgJpFetcher()
        result = fetcher.parse(html, "https://mtg-jp.com/reading/publicity/0038874/")

        assert result.name == "惑星を形作る者"
        assert result.commander == "世界播種、ハースハル"
        assert result.set_raw == "久遠の終端"
        assert result.colors_raw == ["黒", "赤", "緑"]

    def test_list_deck_names_inline_color_heading_format(self) -> None:
        html = _read_fixture("mtgjp_single_deck_inline_color_heading.html")
        fetcher = MtgJpFetcher()

        names = fetcher.list_deck_names(html, "https://mtg-jp.com/reading/publicity/0038874/")

        assert names == ["惑星を形作る者"]

    def test_parse_bare_heading_format(self) -> None:
        # 2024年Fallout記事のように、見出しがデッキ名単独(装飾なし)で、
        # 色情報も<strong>キャプションではなくプレーンテキストの<div>にある
        # ケースの回帰テスト。デッキ見出しの認定はdecklistテーブルの
        # <caption>「デッキ名」との突き合わせによる。
        html = _read_fixture("mtgjp_single_deck_bare_heading.html")
        fetcher = MtgJpFetcher()
        result = fetcher.parse(html, "https://mtg-jp.com/reading/publicity/0037672/")

        assert result.name == "たくましき生存者たち"
        assert result.commander == "忠実な友、ドッグミート"
        assert result.set_raw == "Fallout"
        assert result.colors_raw == ["赤", "緑", "白"]

    def test_list_deck_names_bare_heading_format(self) -> None:
        html = _read_fixture("mtgjp_single_deck_bare_heading.html")
        fetcher = MtgJpFetcher()

        names = fetcher.list_deck_names(html, "https://mtg-jp.com/reading/publicity/0037672/")

        assert names == ["たくましき生存者たち"]


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
