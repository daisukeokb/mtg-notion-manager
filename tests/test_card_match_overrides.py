from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_notion_manager.card_match_overrides import load_card_match_overrides
from mtg_notion_manager.exceptions import CardMatchOverrideError


def test_missing_file_returns_empty_overrides(tmp_path: Path) -> None:
    overrides = load_card_match_overrides(tmp_path / "does-not-exist.json")

    assert overrides.by_japanese_name == {}
    assert overrides.by_english_name == {}


def test_valid_file_is_loaded_and_normalized(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps(
            {
                "by_japanese_name": {
                    "  苦渋の破棄  ": {"canonical_page_id": "p1", "reason": "テスト"}
                },
                "by_english_name": {
                    "Anguished Unmaking": {"canonical_page_id": "p1", "reason": "テスト"}
                },
            }
        ),
        encoding="utf-8",
    )

    overrides = load_card_match_overrides(path)

    assert overrides.resolve(name_ja="苦渋の破棄", name_en=None) is not None
    assert overrides.resolve(name_ja=None, name_en="anguished   unmaking").canonical_page_id == "p1"


def test_resolve_prefers_english_over_japanese(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps(
            {
                "by_japanese_name": {"沼": {"canonical_page_id": "ja-page", "reason": "x"}},
                "by_english_name": {"Swamp": {"canonical_page_id": "en-page", "reason": "y"}},
            }
        ),
        encoding="utf-8",
    )

    overrides = load_card_match_overrides(path)

    result = overrides.resolve(name_ja="沼", name_en="Swamp")
    assert result is not None
    assert result.canonical_page_id == "en-page"


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CardMatchOverrideError):
        load_card_match_overrides(path)


def test_missing_canonical_page_id_raises(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps({"by_japanese_name": {"沼": {"reason": "page_idなし"}}}), encoding="utf-8"
    )

    with pytest.raises(CardMatchOverrideError):
        load_card_match_overrides(path)


def test_not_an_object_raises(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(CardMatchOverrideError):
        load_card_match_overrides(path)
