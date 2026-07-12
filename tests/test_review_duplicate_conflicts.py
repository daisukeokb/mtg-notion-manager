from __future__ import annotations

import csv
import json
from pathlib import Path

from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services import review_duplicate_conflicts as review_mod
from mtg_notion_manager.services.audit_duplicates import ExclusionList

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class FakeNotionClient:
    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = pages or []
        self.update_calls: list[tuple] = []
        self.schema_update_calls: list[tuple] = []

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self.pages

    def get_data_source(self, data_source_id: str) -> dict:
        return {"properties": {"所持枚数": {"type": "number"}, "統合済み": {"type": "checkbox"}}}

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        self.schema_update_calls.append((data_source_id, properties))
        return {}

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.update_calls.append((page_id, properties))
        return {"id": page_id}

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        return []


def _page(
    page_id: str,
    name: str,
    english_name: str | None = None,
    card_type: str | None = None,
    symbols: list[str] | None = None,
    roles: list[str] | None = None,
    note: str = "",
    price: float | None = None,
    link: str | None = None,
    commander_tags: list[str] | None = None,
    merged: bool = False,
    created_time: str | None = None,
    last_edited_time: str | None = None,
) -> dict:
    # 既定ではページIDから決定的にずらし、代表選択の同点(manual_representative)を避ける。
    offset = sum(ord(c) for c in page_id) % 50
    created_time = created_time or f"2024-01-01T00:{offset:02d}:00.000Z"
    last_edited_time = last_edited_time or f"2024-06-01T00:{offset:02d}:00.000Z"
    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name}]},
        "所持": {"type": "checkbox", "checkbox": False},
        "統合済み": {"type": "checkbox", "checkbox": merged},
        "採用デッキ": {
            "type": "relation",
            "id": f"rel-{page_id}",
            "relation": [],
            "has_more": False,
        },
        "メモ": {"type": "rich_text", "rich_text": [{"plain_text": note}] if note else []},
    }
    if english_name is not None:
        properties["英語名"] = {"type": "rich_text", "rich_text": [{"plain_text": english_name}]}
    if card_type is not None:
        properties["タイプ"] = {"type": "select", "select": {"name": card_type}}
    if symbols is not None:
        properties["シンボル"] = {
            "type": "multi_select",
            "multi_select": [{"name": s} for s in symbols],
        }
    if roles is not None:
        properties["役割（標準）"] = {
            "type": "multi_select",
            "multi_select": [{"name": r} for r in roles],
        }
    if price is not None:
        properties["販売価格"] = {"type": "number", "number": price}
    if link is not None:
        properties["販売リンク"] = {"type": "url", "url": link}
    if commander_tags is not None:
        properties["統率者"] = {
            "type": "multi_select",
            "multi_select": [{"name": t} for t in commander_tags],
        }

    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": created_time,
        "last_edited_time": last_edited_time,
        "properties": properties,
    }


def _repo(pages: list[dict]) -> DedupeRepository:
    client = FakeNotionClient(pages)
    return DedupeRepository(client, DATA_SOURCE_ID)


class TestPriceOnlyClassification:
    def test_price_difference_only_is_price_only(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", card_type="土地", price=100),
            _page("p2", "沼", english_name="Swamp", card_type="土地", price=200),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert len(reviews) == 1
        assert reviews[0].review_category == review_mod.CATEGORY_PRICE_ONLY
        assert reviews[0].integrable is True
        assert reviews[0].prices == [100, 200]

    def test_link_difference_only_is_price_only(self) -> None:
        pages = [
            _page("p1", "沼", link="https://example.com/a"),
            _page("p2", "沼", link="https://example.com/b"),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_PRICE_ONLY
        assert reviews[0].integrable is True


class TestSpecialVersionClassification:
    def test_expanded_keyword_is_detected(self) -> None:
        pages = [
            _page("p1", "沼", price=100, note="通常版"),
            _page("p2", "沼", price=200, note="拡張アート版"),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_SPECIAL_VERSION
        assert "拡張アート" in reviews[0].special_flags
        assert reviews[0].integrable is False

    def test_keyword_not_in_original_basic_set_is_still_detected(self) -> None:
        # "surge foil" は audit_duplicates.py の基本キーワードセットには含まれない
        pages = [
            _page("p1", "沼", price=100, note="通常"),
            _page("p2", "沼", price=200, note="surge foil仕様"),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_SPECIAL_VERSION


class TestIdentityConflictClassification:
    def test_english_name_conflict_is_identity_conflict(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", price=100),
            _page("p2", "沼", english_name="Different Name", price=200),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_IDENTITY_CONFLICT
        assert reviews[0].integrable is False

    def test_type_conflict_is_identity_conflict(self) -> None:
        pages = [
            _page("p1", "沼", card_type="土地", price=100),
            _page("p2", "沼", card_type="エンチャント", price=200),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_IDENTITY_CONFLICT

    def test_symbol_conflict_is_identity_conflict(self) -> None:
        pages = [
            _page("p1", "沼", symbols=["黒"], price=100),
            _page("p2", "沼", symbols=["黒", "赤"], price=200),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_IDENTITY_CONFLICT


class TestManualRepresentative:
    def test_perfect_tie_is_manual_representative(self) -> None:
        tie_time_created = "2024-01-01T00:00:00.000Z"
        tie_time_edited = "2024-06-01T00:00:00.000Z"
        pages = [
            _page(
                "p1",
                "血染めのぬかるみ",
                created_time=tie_time_created,
                last_edited_time=tie_time_edited,
            ),
            _page(
                "p2",
                "血染めのぬかるみ",
                created_time=tie_time_created,
                last_edited_time=tie_time_edited,
            ),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews[0].review_category == review_mod.CATEGORY_MANUAL
        assert reviews[0].representative_candidate_id is None
        assert reviews[0].integrable is False


class TestMergedPagesExcluded:
    def test_merged_pages_are_not_reviewed(self) -> None:
        pages = [
            _page("p1", "沼", price=100),
            _page("p2", "沼", price=200, merged=True),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(repo)

        assert reviews == []  # p1のみアクティブなので単一レコード扱い


class TestCategoryFilter:
    def test_filters_to_price_only(self) -> None:
        pages = [
            _page("p1", "沼", price=100),
            _page("p2", "沼", price=200),
            _page("p3", "山", english_name="A", price=100),
            _page("p4", "山", english_name="B", price=100),
        ]
        repo = _repo(pages)

        reviews = review_mod.review_duplicate_conflicts(
            repo, category=review_mod.CATEGORY_PRICE_ONLY
        )

        assert len(reviews) == 1
        assert reviews[0].card_name == "沼"


class TestExclusionListRespected:
    def test_excluded_group_is_not_included(self) -> None:
        pages = [_page("p1", "沼", price=100), _page("p2", "沼", price=200)]
        repo = _repo(pages)
        exclusions = ExclusionList(card_names=frozenset({"沼"}))

        reviews = review_mod.review_duplicate_conflicts(repo, exclusions=exclusions)

        assert reviews == []


class TestNoWriteAPIsCalled:
    def test_review_never_calls_write_apis(self) -> None:
        pages = [
            _page("p1", "沼", price=100, english_name="Swamp"),
            _page("p2", "沼", price=200, english_name="Different"),
            _page("p3", "血染めのぬかるみ"),
            _page("p4", "血染めのぬかるみ"),
        ]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        review_mod.review_duplicate_conflicts(repo)

        assert client.update_calls == []
        assert client.schema_update_calls == []


class TestWriteReviewReports:
    def test_writes_json_csv_markdown(self, tmp_path: Path) -> None:
        pages = [
            _page("p1", "沼", price=100, english_name="Swamp"),
            _page("p2", "沼", price=200, english_name="Swamp"),
        ]
        repo = _repo(pages)
        reviews = review_mod.review_duplicate_conflicts(repo)

        paths = review_mod.write_review_reports(reviews, tmp_path, timestamp="20260101-000000")

        assert paths.json_path.exists()
        assert paths.csv_path.exists()
        assert paths.markdown_path.exists()

        data = json.loads(paths.json_path.read_text(encoding="utf-8"))
        assert data[0]["card_name"] == "沼"
        assert data[0]["review_category"] == review_mod.CATEGORY_PRICE_ONLY

        with paths.csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["card_name"] == "沼"

        markdown_text = paths.markdown_path.read_text(encoding="utf-8")
        assert "要確認グループ詳細分類レポート" in markdown_text
        assert "所持コピーDB" in markdown_text
