from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import IntentionalDuplicateConfigError
from mtg_notion_manager.intentional_duplicates import load_intentional_duplicates


def _write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _valid_group(**overrides: object) -> dict:
    group = {
        "card_name_en": "Anguished Unmaking",
        "card_name_ja": "苦渋の破棄",
        "page_ids": ["p1", "p2"],
        "reason": "通常版とショーケース版を別レコードとして保持する",
        "enabled": True,
    }
    group.update(overrides)
    return group


class TestLoadMissingFile:
    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        config = load_intentional_duplicates(tmp_path / "does-not-exist.json")

        assert config.groups == []


class TestNormalCases:
    def test_valid_group_is_loaded(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group()]})

        config = load_intentional_duplicates(path)

        assert len(config.groups) == 1
        group = config.groups[0]
        assert group.card_name_en == "Anguished Unmaking"
        assert group.card_name_ja == "苦渋の破棄"
        assert group.page_ids == frozenset({"p1", "p2"})
        assert group.reason == "通常版とショーケース版を別レコードとして保持する"
        assert group.enabled is True

    def test_find_matching_group_ignores_page_id_order(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=["p2", "p1"])]})
        config = load_intentional_duplicates(path)

        match = config.find_matching_group(
            frozenset({"p1", "p2"}), name_ja="苦渋の破棄", name_en="Anguished Unmaking"
        )

        assert match is not None

    def test_disabled_group_never_matches(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(enabled=False)]})
        config = load_intentional_duplicates(path)

        match = config.find_matching_group(
            frozenset({"p1", "p2"}), name_ja="苦渋の破棄", name_en="Anguished Unmaking"
        )

        assert match is None

    def test_reason_is_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(reason="テスト理由")]})
        config = load_intentional_duplicates(path)

        assert config.groups[0].reason == "テスト理由"


class TestNoPartialMatch:
    def test_extra_page_in_candidates_does_not_match(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=["p1", "p2"])]})
        config = load_intentional_duplicates(path)

        match = config.find_matching_group(
            frozenset({"p1", "p2", "p3"}), name_ja="苦渋の破棄", name_en="Anguished Unmaking"
        )

        assert match is None

    def test_missing_page_in_candidates_does_not_match(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=["p1", "p2", "p3"])]})
        config = load_intentional_duplicates(path)

        match = config.find_matching_group(
            frozenset({"p1", "p2"}), name_ja="苦渋の破棄", name_en="Anguished Unmaking"
        )

        assert match is None


class TestErrorCases:
    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        path.write_text("{not valid", encoding="utf-8")

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_missing_groups_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_groups_not_a_list_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": "not-a-list"})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        group = _valid_group()
        del group["reason"]
        _write(path, {"groups": [group]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_page_ids_with_one_entry_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=["p1"])]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_page_ids_empty_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=[])]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_page_ids_not_a_list_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids="p1,p2")]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_page_ids_with_empty_string_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=["p1", ""])]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_duplicate_page_id_within_group_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(page_ids=["p1", "p1"])]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_reason_empty_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(reason="")]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_enabled_not_boolean_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": [_valid_group(enabled="true")]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_same_page_id_in_multiple_groups_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(
            path,
            {
                "groups": [
                    _valid_group(
                        card_name_ja="カードA", card_name_en="Card A", page_ids=["p1", "p2"]
                    ),
                    _valid_group(
                        card_name_ja="カードB", card_name_en="Card B", page_ids=["p2", "p3"]
                    ),
                ]
            },
        )

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_conflicting_card_name_in_multiple_groups_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(
            path,
            {
                "groups": [
                    _valid_group(page_ids=["p1", "p2"]),
                    _valid_group(page_ids=["p3", "p4"]),
                ]
            },
        )

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)

    def test_groups_entry_not_an_object_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional.json"
        _write(path, {"groups": ["not-an-object"]})

        with pytest.raises(IntentionalDuplicateConfigError):
            load_intentional_duplicates(path)
