from __future__ import annotations

from mtg_notion_manager.parsers.card_names import normalize_card_name


class TestNormalizeCardName:
    def test_identical_strings_produce_same_key(self) -> None:
        assert normalize_card_name("Sol Ring") == normalize_card_name("Sol Ring")

    def test_leading_trailing_whitespace_is_ignored(self) -> None:
        assert normalize_card_name("  Sol Ring  ") == normalize_card_name("Sol Ring")

    def test_repeated_internal_whitespace_is_collapsed(self) -> None:
        assert normalize_card_name("Sol   Ring") == normalize_card_name("Sol Ring")

    def test_full_width_space_is_normalized(self) -> None:
        assert normalize_card_name("Sol　Ring") == normalize_card_name("Sol Ring")

    def test_full_width_alphanumerics_are_normalized(self) -> None:
        assert normalize_card_name("ＳＯＬ ＲＩＮＧ").casefold() == normalize_card_name("SOL RING")

    def test_case_is_ignored(self) -> None:
        assert normalize_card_name("sol ring") == normalize_card_name("SOL RING")

    def test_double_faced_card_separator_spacing_is_normalized(self) -> None:
        assert normalize_card_name("A//B") == normalize_card_name("A // B")
        assert normalize_card_name("A /  / B") != normalize_card_name("A // B")

    def test_japanese_names_pass_through_unchanged_in_meaning(self) -> None:
        assert normalize_card_name("沼") == normalize_card_name("沼")
        assert normalize_card_name("沼") != normalize_card_name("山")
