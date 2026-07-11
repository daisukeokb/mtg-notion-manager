from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import MultipleDecksFoundError, UnsupportedSourceError
from mtg_notion_manager.fetchers import get_fetcher
from mtg_notion_manager.fetchers.mtg_jp import MtgJpFetcher
from mtg_notion_manager.fetchers.wizards_official import WizardsOfficialFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class TestWizardsOfficialFetcher:
    def test_matches_wizards_domain(self) -> None:
        fetcher = WizardsOfficialFetcher()
        assert fetcher.matches("https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists")
        assert not fetcher.matches("https://mtg-jp.com/reading/publicity/0038046/")

    def test_parse_single_deck(self) -> None:
        html = _read_fixture("wizards_single_deck.html")
        fetcher = WizardsOfficialFetcher()
        result = fetcher.parse(html, "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists")

        assert result.name == "Animated Army"
        assert result.commander == "Bello, Bard of the Brambles"
        assert result.set_raw == "BLB"
        assert result.colors_raw == ["Red", "Green"]

    def test_parse_multi_deck_raises(self) -> None:
        html = _read_fixture("wizards_multi_deck.html")
        fetcher = WizardsOfficialFetcher()
        with pytest.raises(MultipleDecksFoundError):
            fetcher.parse(html, "https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists")


class TestMtgJpFetcher:
    def test_matches_mtgjp_domain(self) -> None:
        fetcher = MtgJpFetcher()
        assert fetcher.matches("https://mtg-jp.com/reading/publicity/0038046/")
        assert not fetcher.matches("https://magic.wizards.com/en/news/announcements/bloomburrow-commander-decklists")

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
