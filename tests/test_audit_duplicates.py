from __future__ import annotations

import csv
import json
from pathlib import Path

from mtg_notion_manager.intentional_duplicates import (
    IntentionalDuplicateConfig,
    IntentionalDuplicateGroup,
)
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services import audit_duplicates as audit_mod

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


def _intentional_config(
    page_ids: frozenset[str],
    card_name_ja: str = "苦渋の破棄",
    card_name_en: str = "Anguished Unmaking",
    reason: str = "通常版とショーケース版を別レコードとして保持する",
    enabled: bool = True,
) -> IntentionalDuplicateConfig:
    return IntentionalDuplicateConfig(
        groups=[
            IntentionalDuplicateGroup(
                card_name_en=card_name_en,
                card_name_ja=card_name_ja,
                page_ids=page_ids,
                reason=reason,
                enabled=enabled,
            )
        ]
    )


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
    note: str = "",
    price: float | None = None,
    link: str | None = None,
    deck_ids: list[str] | None = None,
    merged: bool = False,
    created_time: str = "2024-01-01T00:00:00.000Z",
    last_edited_time: str = "2024-01-01T00:00:00.000Z",
) -> dict:
    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name}]},
        "所持": {"type": "checkbox", "checkbox": False},
        "統合済み": {"type": "checkbox", "checkbox": merged},
        "採用デッキ": {
            "type": "relation",
            "id": f"rel-{page_id}",
            "relation": [{"id": rid} for rid in (deck_ids or [])],
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
    if price is not None:
        properties["販売価格"] = {"type": "number", "number": price}
    if link is not None:
        properties["販売リンク"] = {"type": "url", "url": link}

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


class TestClassifyAuto:
    def test_matching_pages_are_classified_auto(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert len(audits) == 1
        assert audits[0].category == audit_mod.CATEGORY_AUTO
        assert audits[0].recommended_representative_id == "p2"


class TestClassifyNeedsReview:
    def test_english_name_conflict_is_needs_review(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Different", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW
        assert any(c.property_name == "英語名" for c in audits[0].conflicts)

    def test_type_conflict_is_needs_review(self) -> None:
        pages = [
            _page("p1", "沼", card_type="土地", deck_ids=["d1"]),
            _page("p2", "沼", card_type="エンチャント", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW
        assert any(c.property_name == "タイプ" for c in audits[0].conflicts)

    def test_symbol_conflict_is_needs_review(self) -> None:
        pages = [
            _page("p1", "沼", symbols=["黒"], deck_ids=["d1"]),
            _page("p2", "沼", symbols=["黒", "赤"], deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW
        assert any(c.property_name == "シンボル" for c in audits[0].conflicts)

    def test_special_version_keyword_in_note_is_needs_review(self) -> None:
        pages = [
            _page("p1", "沼", note="通常版", deck_ids=["d1"]),
            _page("p2", "沼", note="ショーケース版で入手", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW
        assert "ショーケース" in audits[0].special_version_flags

    def test_price_difference_is_needs_review(self) -> None:
        pages = [
            _page("p1", "沼", price=100, deck_ids=["d1"]),
            _page("p2", "沼", price=200, deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW
        assert audits[0].price_link_differs is True

    def test_link_difference_is_needs_review(self) -> None:
        pages = [
            _page("p1", "沼", link="https://example.com/a", deck_ids=["d1"]),
            _page("p2", "沼", link="https://example.com/b", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW
        assert audits[0].price_link_differs is True


class TestClassifyManualRepresentative:
    def test_perfect_tie_is_manual_representative(self) -> None:
        pages = [_page("p1", "沼"), _page("p2", "沼")]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits[0].category == audit_mod.CATEGORY_MANUAL_REPRESENTATIVE
        assert audits[0].recommended_representative_id is None


class TestExclusionList:
    def test_excluded_card_name_is_classified_excluded(self) -> None:
        pages = [_page("p1", "血染めのぬかるみ"), _page("p2", "血染めのぬかるみ")]
        repo = _repo(pages)
        exclusions = audit_mod.ExclusionList(card_names=frozenset({"血染めのぬかるみ"}))

        audits = audit_mod.audit_duplicate_groups(repo, exclusions=exclusions)

        assert audits[0].category == audit_mod.CATEGORY_EXCLUDED
        assert audits[0].excluded_reason is not None

    def test_excluded_page_id_is_classified_excluded(self) -> None:
        pages = [_page("p1", "沼"), _page("p2", "沼")]
        repo = _repo(pages)
        exclusions = audit_mod.ExclusionList(page_ids=frozenset({"p1"}))

        audits = audit_mod.audit_duplicate_groups(repo, exclusions=exclusions)

        assert audits[0].category == audit_mod.CATEGORY_EXCLUDED

    def test_load_exclusions_reads_json(self, tmp_path: Path) -> None:
        path = tmp_path / "dedupe_exclusions.json"
        path.write_text(
            json.dumps({"card_names": ["血染めのぬかるみ"], "page_ids": ["p1"]}),
            encoding="utf-8",
        )

        exclusions = audit_mod.load_exclusions(path)

        assert "血染めのぬかるみ" in exclusions.card_names
        assert "p1" in exclusions.page_ids

    def test_load_exclusions_missing_file_returns_empty(self, tmp_path: Path) -> None:
        exclusions = audit_mod.load_exclusions(tmp_path / "does-not-exist.json")

        assert exclusions.card_names == frozenset()
        assert exclusions.page_ids == frozenset()


class TestMergedPagesExcluded:
    def test_merged_pages_are_not_audited_as_duplicates(self) -> None:
        pages = [
            _page("p1", "沼", deck_ids=["d1"]),
            _page("p2", "沼", merged=True, deck_ids=["d1"]),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo)

        assert audits == []  # p1のみアクティブなので単一レコード扱い


class TestCardNameFilter:
    def test_filters_to_single_card_name(self) -> None:
        pages = [
            _page("p1", "沼"),
            _page("p2", "沼"),
            _page("p3", "山"),
            _page("p4", "山"),
        ]
        repo = _repo(pages)

        audits = audit_mod.audit_duplicate_groups(repo, card_name="山")

        assert len(audits) == 1
        assert audits[0].card_name == "山"


class TestWriteReports:
    def test_writes_json_csv_and_markdown(self, tmp_path: Path) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)
        audits = audit_mod.audit_duplicate_groups(repo)

        paths = audit_mod.write_audit_reports(audits, tmp_path, timestamp="20260101-000000")

        assert paths.json_path.exists()
        assert paths.csv_path.exists()
        assert paths.markdown_path.exists()

        data = json.loads(paths.json_path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["card_name"] == "沼"
        assert data[0]["category"] == audit_mod.CATEGORY_AUTO
        assert len(data[0]["pages"]) == 2

        with paths.csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["card_name"] == "沼"

        markdown_text = paths.markdown_path.read_text(encoding="utf-8")
        assert "重複カード監査レポート" in markdown_text
        assert "沼" in markdown_text

    def test_output_directory_is_created_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "reports"
        pages = [_page("p1", "沼"), _page("p2", "沼")]
        repo = _repo(pages)
        audits = audit_mod.audit_duplicate_groups(repo)

        paths = audit_mod.write_audit_reports(audits, nested, timestamp="20260101-000000")

        assert paths.json_path.exists()


class TestAuditDoesNotWrite:
    def test_audit_never_calls_update_apis(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Different", card_type="エンチャント"),
            _page("p3", "山"),
            _page("p4", "山"),
        ]
        client = FakeNotionClient(pages)
        repo = DedupeRepository(client, DATA_SOURCE_ID)

        audit_mod.audit_duplicate_groups(repo)

        assert client.update_calls == []
        assert client.schema_update_calls == []


class TestIntentionalDuplicates:
    def test_exact_page_id_match_is_classified_intentional(self) -> None:
        pages = [
            _page("p1", "苦渋の破棄", english_name="Anguished Unmaking", note="ショーケース"),
            _page("p2", "苦渋の破棄", english_name="Anguished Unmaking", price=200),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(frozenset({"p1", "p2"}))

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        assert len(audits) == 1
        assert audits[0].category == audit_mod.CATEGORY_INTENTIONAL_DUPLICATE

    def test_page_id_order_does_not_matter(self) -> None:
        pages = [
            _page("p2", "苦渋の破棄", english_name="Anguished Unmaking"),
            _page("p1", "苦渋の破棄", english_name="Anguished Unmaking"),
        ]
        repo = _repo(pages)
        # 設定側は逆順で登録
        intentional = _intentional_config(frozenset({"p2", "p1"}))

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        assert audits[0].category == audit_mod.CATEGORY_INTENTIONAL_DUPLICATE

    def test_intentional_duplicate_not_counted_as_needs_review(self) -> None:
        pages = [
            _page("p1", "苦渋の破棄", english_name="Anguished Unmaking", price=100),
            _page("p2", "苦渋の破棄", english_name="Anguished Unmaking", price=200),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(frozenset({"p1", "p2"}))

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        needs_review = [a for a in audits if a.category == audit_mod.CATEGORY_NEEDS_REVIEW]
        assert needs_review == []

    def test_intentional_duplicate_appears_in_json_report_with_dedicated_fields(
        self, tmp_path: Path
    ) -> None:
        pages = [
            _page("p1", "苦渋の破棄", english_name="Anguished Unmaking"),
            _page("p2", "苦渋の破棄", english_name="Anguished Unmaking"),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(frozenset({"p1", "p2"}))

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)
        paths = audit_mod.write_audit_reports(audits, tmp_path, timestamp="20260101-000000")

        data = json.loads(paths.json_path.read_text(encoding="utf-8"))
        assert len(data) == 1
        entry = data[0]
        assert entry["category"] == "intentional_duplicates"
        assert entry["card_name_en"] == "Anguished Unmaking"
        assert entry["card_name_ja"] == "苦渋の破棄"
        assert set(entry["page_ids"]) == {"p1", "p2"}
        assert entry["reason"] == "通常版とショーケース版を別レコードとして保持する"
        assert entry["status"] == "intentional_duplicate"
        assert entry["source"] == "config/intentional_duplicate_cards.json"

    def test_reason_is_included_in_group_audit(self) -> None:
        pages = [
            _page("p1", "苦渋の破棄", english_name="Anguished Unmaking"),
            _page("p2", "苦渋の破棄", english_name="Anguished Unmaking"),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(
            frozenset({"p1", "p2"}), reason="カスタム理由テキスト"
        )

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        assert audits[0].intentional_duplicate_reason == "カスタム理由テキスト"

    def test_disabled_config_does_not_apply(self) -> None:
        pages = [
            _page(
                "p1",
                "苦渋の破棄",
                english_name="Anguished Unmaking",
                price=100,
                last_edited_time="2024-01-01T00:00:00.000Z",
            ),
            _page(
                "p2",
                "苦渋の破棄",
                english_name="Anguished Unmaking",
                price=200,
                last_edited_time="2024-06-01T00:00:00.000Z",
            ),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(frozenset({"p1", "p2"}), enabled=False)

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        assert audits[0].category == audit_mod.CATEGORY_NEEDS_REVIEW

    def test_anguished_unmaking_two_pages_classified_intentional(self) -> None:
        """苦渋の破棄(Anguished Unmaking)の実データに近い形の回帰テスト。"""
        pages = [
            _page(
                "78a2b136-bef4-487a-9b46-ec08bdf8d4cb",
                "苦渋の破棄",
                english_name="Anguished Unmaking",
                note="ショーケース",
                price=150,
            ),
            _page(
                "28ef458e-b1f4-4226-98d8-cc6c3c144d2a",
                "苦渋の破棄",
                english_name="Anguished Unmaking",
                note="ショーケース",
                price=200,
                link="https://www.hareruyamtg.com/ja/products/detail/154784?lang=JP",
            ),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(
            frozenset(
                {
                    "78a2b136-bef4-487a-9b46-ec08bdf8d4cb",
                    "28ef458e-b1f4-4226-98d8-cc6c3c144d2a",
                }
            )
        )

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        assert len(audits) == 1
        assert audits[0].category == audit_mod.CATEGORY_INTENTIONAL_DUPLICATE

    def test_extra_page_in_group_is_not_treated_as_intentional(self) -> None:
        # 監査候補が3件あるが設定は2件のみ → 適用しない(部分一致は不可)
        pages = [
            _page("p1", "苦渋の破棄", english_name="Anguished Unmaking"),
            _page("p2", "苦渋の破棄", english_name="Anguished Unmaking"),
            _page("p3", "苦渋の破棄", english_name="Anguished Unmaking"),
        ]
        repo = _repo(pages)
        intentional = _intentional_config(frozenset({"p1", "p2"}))

        audits = audit_mod.audit_duplicate_groups(repo, intentional_duplicates=intentional)

        assert audits[0].category != audit_mod.CATEGORY_INTENTIONAL_DUPLICATE

    def test_existing_classification_unchanged_without_intentional_config(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", card_type="土地", deck_ids=["d1", "d2"]),
        ]
        repo = _repo(pages)

        without_param = audit_mod.audit_duplicate_groups(repo)
        with_empty_config = audit_mod.audit_duplicate_groups(
            repo, intentional_duplicates=IntentionalDuplicateConfig(groups=[])
        )

        assert without_param[0].category == with_empty_config[0].category == audit_mod.CATEGORY_AUTO
