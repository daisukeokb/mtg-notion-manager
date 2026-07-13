from __future__ import annotations

from pathlib import Path

import pytest

from mtg_notion_manager.exceptions import DeckPageMappingConfigError
from mtg_notion_manager.models import ExistingDeck
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.services.deck_page_mapping import (
    RESOLUTION_EXACT_NAME_MATCH,
    RESOLUTION_EXPLICIT_MAPPING,
    load_deck_page_mapping,
    normalize_article_url,
    normalize_page_id,
    resolve_deck_page,
)

ARTICLE_URL = "https://magic.wizards.com/ja/news/announcements/lorwyn-eclipsed-commander-decklists"
COMMANDER_DS_ID = "39aa97c8-7142-80a1-85c2-000b7f998d48"

DECK_A_PAGE_ID = "39aa97c8-7142-813d-9e6c-e3b7b2bb8873"
DECK_A_NAME_JA = "エレメンタルの舞踊"
DECK_A_NAME_EN = "Dance of the Elements"

DECK_B_PAGE_ID = "39aa97c8-7142-81a9-8444-e3ab7a9b7a63"
DECK_B_NAME_JA = "枯朽の呪い"
DECK_B_NAME_EN = "Blight Curse"


class FakeNotionClient:
    def __init__(self, pages: dict[str, dict] | None = None) -> None:
        self.pages = pages or {}
        self.query_results: dict[str, list[dict]] = {}
        self.get_page_calls: list[str] = []

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
        return self.query_results.get(title, [])

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls.append(page_id)
        if page_id not in self.pages:
            from mtg_notion_manager.exceptions import NotionAPIError

            raise NotionAPIError(f"page not found: {page_id}")
        return self.pages[page_id]


def _deck_page(page_id: str, name: str, data_source_id: str = COMMANDER_DS_ID) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": {"名前": {"type": "title", "title": [{"plain_text": name}]}},
    }


def _mapping_config_dict(
    article_url: str = ARTICLE_URL,
    decks: list[dict] | None = None,
    schema_version: int = 1,
) -> dict:
    return {
        "schema_version": schema_version,
        "article_url": article_url,
        "decks": decks
        if decks is not None
        else [
            {
                "article_deck_name": DECK_A_NAME_EN,
                "page_id": DECK_A_PAGE_ID,
                "expected_page_name": DECK_A_NAME_JA,
            },
            {
                "article_deck_name": DECK_B_NAME_EN,
                "page_id": DECK_B_PAGE_ID,
                "expected_page_name": DECK_B_NAME_JA,
            },
        ],
    }


def _write_mapping(tmp_path: Path, data: dict) -> Path:
    import json

    path = tmp_path / "deck_page_mapping.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestReproduceLorwynMismatch:
    """ローウィンの昏明の実際の症状を最小再現する。

    マッピングを一切使わない場合、英語のdeck-title("Dance of the Elements")は
    日本語のNotionページ名("エレメンタルの舞踊")と完全一致しないため解決できない
    (これは不具合ではなく、修正前の設計どおりの動作)。
    """

    def test_without_mapping_english_article_name_does_not_resolve(self) -> None:
        pages = {DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_NAME_JA)}
        client = FakeNotionClient(pages=pages)
        writer = NotionWriter(client, COMMANDER_DS_ID)
        # Notion側は日本語名でしか登録されていないため、英語名では見つからない
        client.query_results[DECK_A_NAME_JA] = [_deck_page(DECK_A_PAGE_ID, DECK_A_NAME_JA)]

        resolution = resolve_deck_page(DECK_A_NAME_EN, writer, mapping=None)

        assert resolution.resolved is False
        assert resolution.existing_deck is None

    def test_with_explicit_mapping_english_article_name_resolves_to_japanese_page(
        self, tmp_path: Path
    ) -> None:
        pages = {DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_NAME_JA)}
        client = FakeNotionClient(pages=pages)
        writer = NotionWriter(client, COMMANDER_DS_ID)
        mapping_path = _write_mapping(tmp_path, _mapping_config_dict())
        mapping = load_deck_page_mapping(
            mapping_path, ARTICLE_URL, [DECK_A_NAME_EN, DECK_B_NAME_EN]
        )

        resolution = resolve_deck_page(DECK_A_NAME_EN, writer, mapping=mapping)

        assert resolution.resolved is True
        assert resolution.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert resolution.existing_deck is not None
        assert resolution.existing_deck.page_id == DECK_A_PAGE_ID


class TestResolveDeckPageWithoutMapping:
    def test_exact_name_match_still_works(self) -> None:
        client = FakeNotionClient()
        name = "エレメンタルの舞踊"
        client.query_results[name] = [_deck_page(DECK_A_PAGE_ID, name)]
        writer = NotionWriter(client, COMMANDER_DS_ID)

        resolution = resolve_deck_page("エレメンタルの舞踊", writer, mapping=None)

        assert resolution.resolved is True
        assert resolution.resolution_method == RESOLUTION_EXACT_NAME_MATCH
        assert resolution.existing_deck is not None
        assert resolution.existing_deck.page_id == DECK_A_PAGE_ID

    def test_no_match_is_unresolved(self) -> None:
        client = FakeNotionClient()
        writer = NotionWriter(client, COMMANDER_DS_ID)

        resolution = resolve_deck_page("存在しないデッキ", writer, mapping=None)

        assert resolution.resolved is False
        assert resolution.resolution_method is None
        assert resolution.existing_deck is None
        assert resolution.error is None

    def test_multiple_matches_is_unresolved_with_error(self) -> None:
        client = FakeNotionClient()
        client.query_results["重複デッキ"] = [
            _deck_page("page-1", "重複デッキ"),
            _deck_page("page-2", "重複デッキ"),
        ]
        writer = NotionWriter(client, COMMANDER_DS_ID)

        resolution = resolve_deck_page("重複デッキ", writer, mapping=None)

        assert resolution.resolved is False
        assert resolution.existing_deck is None
        assert resolution.error is not None
        assert "2件" in resolution.error


class TestResolveDeckPageWithMapping:
    def test_mapping_takes_priority_over_exact_match(self, tmp_path: Path) -> None:
        """マッピングと同名の完全一致候補が両方存在する場合、マッピングを優先する。"""
        other_page_id = "other-page-id-0000000000000000"
        pages = {DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_NAME_JA)}
        client = FakeNotionClient(pages=pages)
        # "Dance of the Elements"という名前のページが偶然Notion上に別途存在していても、
        # マッピングが優先されるべき
        client.query_results[DECK_A_NAME_EN] = [_deck_page(other_page_id, DECK_A_NAME_EN)]
        writer = NotionWriter(client, COMMANDER_DS_ID)
        mapping = load_deck_page_mapping(
            _write_mapping(tmp_path, _mapping_config_dict()),
            ARTICLE_URL,
            [DECK_A_NAME_EN, DECK_B_NAME_EN],
        )

        resolution = resolve_deck_page(DECK_A_NAME_EN, writer, mapping=mapping)

        assert resolution.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert resolution.existing_deck is not None
        assert resolution.existing_deck.page_id == DECK_A_PAGE_ID  # マッピング側が優先

    def test_invalid_mapping_target_does_not_fall_back_to_exact_match(self, tmp_path: Path) -> None:
        """マッピング先ページの名前が期待と異なる場合、完全一致へフォールバックせず失敗する。"""
        client = FakeNotionClient(
            pages={DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, "違うページ名")}
        )
        # 完全一致なら見つかる状態を用意しておく(それでもフォールバックしてはいけない)
        client.query_results[DECK_A_NAME_EN] = [_deck_page("fallback-page-id", DECK_A_NAME_EN)]
        writer = NotionWriter(client, COMMANDER_DS_ID)
        mapping = load_deck_page_mapping(
            _write_mapping(tmp_path, _mapping_config_dict()),
            ARTICLE_URL,
            [DECK_A_NAME_EN, DECK_B_NAME_EN],
        )

        resolution = resolve_deck_page(DECK_A_NAME_EN, writer, mapping=mapping)

        assert resolution.resolved is False
        assert resolution.resolution_method == RESOLUTION_EXPLICIT_MAPPING
        assert resolution.existing_deck is None
        assert resolution.error is not None

    def test_page_outside_commander_db_fails(self, tmp_path: Path) -> None:
        pages = {
            DECK_A_PAGE_ID: _deck_page(DECK_A_PAGE_ID, DECK_A_NAME_JA, data_source_id="other-ds")
        }
        client = FakeNotionClient(pages=pages)
        writer = NotionWriter(client, COMMANDER_DS_ID)
        mapping = load_deck_page_mapping(
            _write_mapping(tmp_path, _mapping_config_dict()),
            ARTICLE_URL,
            [DECK_A_NAME_EN, DECK_B_NAME_EN],
        )

        resolution = resolve_deck_page(DECK_A_NAME_EN, writer, mapping=mapping)

        assert resolution.resolved is False
        assert resolution.error is not None

    def test_page_not_found_fails(self, tmp_path: Path) -> None:
        client = FakeNotionClient(pages={})
        writer = NotionWriter(client, COMMANDER_DS_ID)
        mapping = load_deck_page_mapping(
            _write_mapping(tmp_path, _mapping_config_dict()),
            ARTICLE_URL,
            [DECK_A_NAME_EN, DECK_B_NAME_EN],
        )

        resolution = resolve_deck_page(DECK_A_NAME_EN, writer, mapping=mapping)

        assert resolution.resolved is False
        assert resolution.error is not None

    def test_deck_without_mapping_entry_falls_back_to_exact_match(self, tmp_path: Path) -> None:
        client = FakeNotionClient()
        client.query_results["別デッキ"] = [_deck_page("other-id", "別デッキ")]
        writer = NotionWriter(client, COMMANDER_DS_ID)
        mapping = load_deck_page_mapping(
            _write_mapping(tmp_path, _mapping_config_dict()),
            ARTICLE_URL,
            [DECK_A_NAME_EN, DECK_B_NAME_EN, "別デッキ"],
        )

        resolution = resolve_deck_page("別デッキ", writer, mapping=mapping)

        assert resolution.resolved is True
        assert resolution.resolution_method == RESOLUTION_EXACT_NAME_MATCH


class TestLoadDeckPageMappingValidation:
    def test_missing_file_fails(self, tmp_path: Path) -> None:
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(tmp_path / "does-not-exist.json", ARTICLE_URL, [DECK_A_NAME_EN])

    def test_invalid_json_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN])

    def test_missing_required_key_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(decks=[{"article_deck_name": DECK_A_NAME_EN}])
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN])

    def test_empty_article_deck_name_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(
            decks=[
                {"article_deck_name": "", "page_id": DECK_A_PAGE_ID, "expected_page_name": "x"}
            ]
        )
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN])

    def test_empty_expected_page_name_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(
            decks=[
                {
                    "article_deck_name": DECK_A_NAME_EN,
                    "page_id": DECK_A_PAGE_ID,
                    "expected_page_name": "",
                }
            ]
        )
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN])

    def test_invalid_page_id_format_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(
            decks=[
                {
                    "article_deck_name": DECK_A_NAME_EN,
                    "page_id": "not-a-valid-uuid",
                    "expected_page_name": DECK_A_NAME_JA,
                }
            ]
        )
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN])

    def test_unsupported_schema_version_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(schema_version=999)
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN, DECK_B_NAME_EN])

    def test_article_url_mismatch_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(article_url="https://example.com/other-article")
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN, DECK_B_NAME_EN])

    def test_duplicate_page_id_across_decks_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(
            decks=[
                {
                    "article_deck_name": DECK_A_NAME_EN,
                    "page_id": DECK_A_PAGE_ID,
                    "expected_page_name": DECK_A_NAME_JA,
                },
                {
                    "article_deck_name": DECK_B_NAME_EN,
                    "page_id": DECK_A_PAGE_ID,
                    "expected_page_name": DECK_B_NAME_JA,
                },
            ]
        )
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN, DECK_B_NAME_EN])

    def test_deck_name_not_in_article_fails(self, tmp_path: Path) -> None:
        data = _mapping_config_dict(
            decks=[
                {
                    "article_deck_name": "存在しないデッキ名",
                    "page_id": DECK_A_PAGE_ID,
                    "expected_page_name": DECK_A_NAME_JA,
                }
            ]
        )
        path = _write_mapping(tmp_path, data)
        with pytest.raises(DeckPageMappingConfigError):
            load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN, DECK_B_NAME_EN])

    def test_mapping_for_non_selected_deck_is_allowed(self, tmp_path: Path) -> None:
        """記事全体には存在するが--include-deckで選択されていないデッキのマッピングはエラーにしない。"""
        path = _write_mapping(tmp_path, _mapping_config_dict())
        # all_deck_namesには両方存在させる(選択有無に関わらず記事全体のリストを渡す)
        mapping = load_deck_page_mapping(path, ARTICLE_URL, [DECK_A_NAME_EN, DECK_B_NAME_EN])
        assert mapping.resolve(DECK_A_NAME_EN) is not None
        assert mapping.resolve(DECK_B_NAME_EN) is not None


class TestNormalizeArticleUrl:
    def test_trailing_slash_is_ignored(self) -> None:
        assert normalize_article_url(ARTICLE_URL + "/") == normalize_article_url(ARTICLE_URL)

    def test_query_string_is_ignored(self) -> None:
        assert normalize_article_url(ARTICLE_URL + "?utm_source=x") == normalize_article_url(
            ARTICLE_URL
        )

    def test_different_paths_are_not_equal(self) -> None:
        assert normalize_article_url(ARTICLE_URL) != normalize_article_url(
            "https://magic.wizards.com/ja/news/announcements/other-article"
        )


class TestNormalizePageId:
    def test_dashed_and_compact_forms_are_equal(self) -> None:
        dashed = "39aa97c8-7142-813d-9e6c-e3b7b2bb8873"
        compact = "39aa97c871428 13d9e6ce3b7b2bb8873".replace(" ", "")
        assert normalize_page_id(dashed) == normalize_page_id(compact)

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(DeckPageMappingConfigError):
            normalize_page_id("not-a-uuid")


class TestExistingDeckImport:
    def test_existing_deck_type_is_importable(self) -> None:
        # resolve_deck_page が返す existing_deck の型が既存モデルと一致することを確認する
        assert ExistingDeck is not None
