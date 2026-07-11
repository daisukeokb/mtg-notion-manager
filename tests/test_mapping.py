import pytest

from mtg_notion_manager.exceptions import MappingError
from mtg_notion_manager.mapping import normalize_colors, normalize_set_name


class TestNormalizeSetName:
    def test_already_valid_name_passes_through(self) -> None:
        assert normalize_set_name("ブルームバロウ") == "ブルームバロウ"

    def test_wizards_set_code_is_mapped(self) -> None:
        assert normalize_set_name("BLB") == "ブルームバロウ"
        assert normalize_set_name("blb") == "ブルームバロウ"  # 大文字小文字を無視

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(MappingError):
            normalize_set_name("未知のセット")

    def test_unknown_code_raises(self) -> None:
        with pytest.raises(MappingError):
            normalize_set_name("ZZZ")


class TestNormalizeColors:
    def test_japanese_tokens_pass_through(self) -> None:
        assert normalize_colors(["赤", "緑"]) == ["赤", "緑"]

    def test_english_tokens_are_mapped(self) -> None:
        assert normalize_colors(["Red", "Green"]) == ["赤", "緑"]

    def test_mixed_tokens(self) -> None:
        assert normalize_colors(["赤", "Green"]) == ["赤", "緑"]

    def test_duplicates_are_removed_preserving_order(self) -> None:
        assert normalize_colors(["赤", "緑", "赤"]) == ["赤", "緑"]

    def test_unknown_token_raises(self) -> None:
        with pytest.raises(MappingError):
            normalize_colors(["赤", "ピンク"])
