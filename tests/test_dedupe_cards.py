from __future__ import annotations

from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services import dedupe_cards

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class FakeNotionClient:
    def __init__(self, pages: list[dict] | None = None, schema: dict | None = None) -> None:
        self.pages = pages or []
        self.schema = schema or {
            "properties": {"所持枚数": {"type": "number"}, "統合済み": {"type": "checkbox"}}
        }
        self.updated_pages: list[tuple[str, dict]] = []
        self.deleted_calls: list[str] = []

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self.pages

    def get_data_source(self, data_source_id: str) -> dict:
        return self.schema

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        self.schema["properties"].update(properties)
        return self.schema

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.updated_pages.append((page_id, properties))
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        return []

    # 削除APIは存在しない(このクラスに delete_page メソッドを実装しないこと自体が
    # 「削除機能は実装しない」ことのテスト上の裏付けになる)。


def _page(
    page_id: str,
    name: str,
    english_name: str | None = None,
    owned: bool = False,
    deck_ids: list[str] | None = None,
    card_type: str | None = None,
    symbols: list[str] | None = None,
    quantity: int | None = None,
    merged: bool = False,
    created_time: str = "2024-01-01T00:00:00.000Z",
    last_edited_time: str = "2024-01-01T00:00:00.000Z",
    note: str = "",
) -> dict:
    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name}]},
        "所持": {"type": "checkbox", "checkbox": owned},
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
    if quantity is not None:
        properties["所持枚数"] = {"type": "number", "number": quantity}

    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": created_time,
        "last_edited_time": last_edited_time,
        "properties": properties,
    }


def _repo(pages: list[dict]) -> tuple[DedupeRepository, FakeNotionClient]:
    client = FakeNotionClient(pages)
    repo = DedupeRepository(client, DATA_SOURCE_ID)
    return repo, client


class TestBuildDedupePlanRepresentativeSelection:
    def test_prefers_page_with_english_name(self) -> None:
        pages = [
            _page("p1", "沼"),
            _page("p2", "沼", english_name="Swamp"),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert len(plan.merge_plans) == 1
        assert plan.merge_plans[0].representative_page_id == "p2"

    def test_prefers_more_deck_relations_when_english_name_tied(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", deck_ids=["d1", "d2"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].representative_page_id == "p2"

    def test_prefers_more_filled_attributes_when_tied(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p1b", "沼", english_name="Swamp", deck_ids=["d1"], card_type="土地"),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].representative_page_id == "p1b"

    def test_prefers_most_recently_edited_when_otherwise_tied(self) -> None:
        pages = [
            _page(
                "p1",
                "沼",
                english_name="Swamp",
                deck_ids=["d1"],
                card_type="土地",
                last_edited_time="2024-01-01T00:00:00.000Z",
            ),
            _page(
                "p2",
                "沼",
                english_name="Swamp",
                deck_ids=["d1"],
                card_type="土地",
                last_edited_time="2024-06-01T00:00:00.000Z",
            ),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].representative_page_id == "p2"

    def test_prefers_oldest_created_as_final_tiebreak(self) -> None:
        pages = [
            _page("p1", "沼", created_time="2024-06-01T00:00:00.000Z"),
            _page("p2", "沼", created_time="2024-01-01T00:00:00.000Z"),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].representative_page_id == "p2"

    def test_manual_representative_override(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo, card_name="沼", representative_page_id="p2")

        assert plan.merge_plans[0].representative_page_id == "p2"

    def test_unresolvable_tie_is_reported_as_group_error(self) -> None:
        pages = [
            _page("p1", "沼", created_time="2024-01-01T00:00:00.000Z"),
            _page("p2", "沼", created_time="2024-01-01T00:00:00.000Z"),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans == []
        assert len(plan.group_errors) == 1
        assert plan.group_errors[0].error_type == "representative_selection"


class TestBuildDedupePlanMergeComputation:
    def test_deck_relations_are_unioned_without_duplicates(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1", "d2"]),
            _page("p2", "沼", deck_ids=["d2", "d3"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)
        merge_plan = plan.merge_plans[0]

        assert sorted(merge_plan.merged_deck_relation_ids) == ["d1", "d2", "d3"]

    def test_owned_is_true_if_any_page_owned(self) -> None:
        pages = [
            _page("p1", "沼", owned=False, deck_ids=["d1"]),
            _page("p2", "沼", owned=True, deck_ids=["d1", "d2"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].owned is True

    def test_quantity_defaults_to_duplicate_count(self) -> None:
        pages = [
            _page("p1", "沼", deck_ids=["d1"]),
            _page("p2", "沼", deck_ids=["d1", "d2"]),
            _page("p3", "沼", deck_ids=["d1", "d2", "d3"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].quantity == 3

    def test_quantity_does_not_shrink_below_existing_representative_value(self) -> None:
        # 代表候補となるページに既に大きい所持枚数が設定済みの場合、
        # グループが縮小していても枚数は減らない(部分失敗後の再実行を想定)。
        pages = [
            _page("p1", "沼", english_name="Swamp", quantity=10),
            _page("p2", "沼"),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans[0].quantity == 10

    def test_conflicting_english_names_raise_conflict_error_as_group_error(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Different Name", deck_ids=["d1", "d2"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans == []
        assert len(plan.group_errors) == 1
        assert plan.group_errors[0].error_type == "conflict"

    def test_conflicting_types_raise_conflict_error(self) -> None:
        pages = [
            _page("p1", "沼", card_type="土地", deck_ids=["d1"]),
            _page("p2", "沼", card_type="エンチャント", deck_ids=["d1", "d2"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert len(plan.group_errors) == 1
        assert plan.group_errors[0].error_type == "conflict"

    def test_multi_select_symbols_are_unioned_without_conflict(self) -> None:
        pages = [
            _page("p1", "沼", symbols=["黒"], deck_ids=["d1"]),
            _page("p2", "沼", symbols=["黒", "赤"], deck_ids=["d1", "d2"]),
        ]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans != []
        assert sorted(plan.merge_plans[0].multi_valued_attributes["シンボル"]) == ["赤", "黒"]

    def test_single_record_group_is_not_included(self) -> None:
        pages = [_page("p1", "統率の塔")]
        repo, _ = _repo(pages)

        plan = dedupe_cards.build_dedupe_plan(repo)

        assert plan.merge_plans == []
        assert plan.group_errors == []


class TestExecuteDedupePlan:
    def test_dry_run_build_plan_does_not_write(self) -> None:
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _repo(pages)

        dedupe_cards.build_dedupe_plan(repo)

        assert client.updated_pages == []

    def test_apply_updates_representative_and_marks_duplicates(self) -> None:
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        assert result.failed == []
        page_ids_updated = {page_id for page_id, _ in client.updated_pages}
        assert "p1" in page_ids_updated  # 代表
        assert "p2" in page_ids_updated  # 統合対象

        p2_update = next(props for page_id, props in client.updated_pages if page_id == "p2")
        assert p2_update["統合済み"]["checkbox"] is True

    def test_no_delete_api_is_ever_called(self) -> None:
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)

        dedupe_cards.execute_dedupe_plan(plan, repo)

        assert not hasattr(client, "delete_page")
        assert client.deleted_calls == []

    def test_rerun_after_apply_is_idempotent(self) -> None:
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)
        dedupe_cards.execute_dedupe_plan(plan, repo)

        # Notion側の状態変化を模擬: p2に統合済みフラグが立った状態で再度読み込む
        client.pages[1]["properties"]["統合済み"]["checkbox"] = True
        updated_count_before = len(client.updated_pages)

        repo2 = DedupeRepository(client, DATA_SOURCE_ID)
        plan2 = dedupe_cards.build_dedupe_plan(repo2)

        # p1のみアクティブなので重複グループは存在しない
        assert plan2.merge_plans == []
        assert len(client.updated_pages) == updated_count_before  # 追加の書き込みなし
