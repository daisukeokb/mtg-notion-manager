from __future__ import annotations

import httpx
import pytest

from mtg_notion_manager.exceptions import NotionAPIError
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
    price: float | None = None,
    link: str | None = None,
    commander_tags: list[str] | None = None,
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


def _repo(pages: list[dict]) -> tuple[DedupeRepository, FakeNotionClient]:
    client = FakeNotionClient(pages)
    repo = DedupeRepository(client, DATA_SOURCE_ID)
    return repo, client


class FailingNotionClient(FakeNotionClient):
    """指定したpage_idへのupdate_page()呼び出し時にNotionAPIErrorを送出する。

    失敗させたページの呼び出しはupdated_pagesへ記録しない(実際にNotion側への
    書き込みが完了しなかったことを模す)。それ以外の挙動はFakeNotionClientと同じ。
    """

    def __init__(
        self,
        pages: list[dict] | None = None,
        schema: dict | None = None,
        fail_on_page_ids: set[str] | None = None,
    ) -> None:
        super().__init__(pages, schema)
        self.fail_on_page_ids = fail_on_page_ids or set()
        self.attempted_page_ids: list[str] = []

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.attempted_page_ids.append(page_id)
        if page_id in self.fail_on_page_ids:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました: {page_id}")
        return super().update_page(page_id, properties)


def _failing_repo(
    pages: list[dict], fail_on_page_ids: set[str]
) -> tuple[DedupeRepository, FailingNotionClient]:
    client = FailingNotionClient(pages, fail_on_page_ids=fail_on_page_ids)
    repo = DedupeRepository(client, DATA_SOURCE_ID)
    return repo, client


def _http_status_error(status_code: int = 400) -> httpx.HTTPStatusError:
    """実際のNotionClientが送出するNotionAPIErrorのcauseを模す(定義済みのHTTP
    エラー応答 = サーバーが明示的に拒否した、という構造的signal)。"""
    request = httpx.Request("PATCH", "https://api.notion.com/v1/pages/x")
    response = httpx.Response(status_code, request=request, text="notion error body")
    return httpx.HTTPStatusError("HTTP error", request=request, response=response)


def _timeout_exception() -> httpx.TimeoutException:
    """実際のNotionClientが送出するNotionAPIErrorのcauseを模す(応答が一切
    得られない = 完了状態が不明、という構造的signal)。"""
    return httpx.TimeoutException("timed out")


class FailingWithCauseNotionClient(FakeNotionClient):
    """指定したpage_idへのupdate_page()呼び出し時に、指定したcauseを持つ
    NotionAPIErrorを送出する(実際のNotionClient._request()の
    `raise NotionAPIError(...) from exc` 連鎖をそのまま模したfake)。"""

    def __init__(
        self,
        pages: list[dict] | None = None,
        schema: dict | None = None,
        fail_on_page_ids: dict[str, BaseException] | None = None,
    ) -> None:
        super().__init__(pages, schema)
        self.fail_on_page_ids = fail_on_page_ids or {}
        self.attempted_page_ids: list[str] = []

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.attempted_page_ids.append(page_id)
        cause = self.fail_on_page_ids.get(page_id)
        if cause is not None:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました: {page_id}") from cause
        return super().update_page(page_id, properties)


def _cause_repo(
    pages: list[dict], fail_on_page_ids: dict[str, BaseException]
) -> tuple[DedupeRepository, FailingWithCauseNotionClient]:
    client = FailingWithCauseNotionClient(pages, fail_on_page_ids=fail_on_page_ids)
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


class TestApplyOneGroupResultFidelity:
    """_apply_one_group()が、NotionAPIErrorによる中断より前に実際に成功した
    書き込みだけを正確にGroupApplyResultへ反映することを検証する。

    stop-on-first-error(グループ内)・continue-on-error(グループ間)という
    既存挙動自体は変更していないことも、書き込み試行のassertionで併せて確認する。
    """

    def test_representative_failure_marks_nothing(self) -> None:
        """T1: 代表レコード更新自体が失敗した場合、representative_updated=False、
        marked=[]、重複ページへの書き込みは1件も試行されない。"""
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _failing_repo(pages, fail_on_page_ids={"p1"})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.representative_updated is False
        assert group_result.duplicate_page_ids_marked == []
        assert group_result.error is not None
        assert "p2" not in client.attempted_page_ids  # 重複ページへは一切試行されない

    def test_representative_success_then_first_duplicate_failure(self) -> None:
        """T2: 代表レコード更新は成功したが、最初の重複ページのmarkで失敗した場合、
        representative_updated=Trueが保持され(Falseへ戻らない)、marked=[]のまま。"""
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
            _page("p3", "沼"),
        ]
        repo, client = _failing_repo(pages, fail_on_page_ids={"p2"})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.representative_updated is True
        assert group_result.duplicate_page_ids_marked == []
        assert group_result.error is not None
        assert client.attempted_page_ids == ["p1", "p2"]  # p3は一切試行されない

    def test_partial_duplicate_prefix_is_preserved_in_order(self) -> None:
        """T3: 重複ページA・Bのmarkが成功した後、Cで失敗した場合、A・Bのmark記録が
        実際の書き込み順を保ったまま保持され、C・Dへは書き込みを試行しない。"""
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
            _page("p3", "沼"),
            _page("p4", "沼"),
            _page("p5", "沼"),
        ]
        repo, client = _failing_repo(pages, fail_on_page_ids={"p4"})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.representative_updated is True
        assert group_result.duplicate_page_ids_marked == ["p2", "p3"]  # 順序も維持
        assert group_result.error is not None
        assert client.attempted_page_ids == ["p1", "p2", "p3", "p4"]  # p5は未試行

    def test_complete_success_is_fully_compatible(self) -> None:
        """T4: 全件成功時は既存resultと完全互換(既存test_apply_updates_...と同内容)。"""
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼"), _page("p3", "沼")]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.representative_updated is True
        assert group_result.duplicate_page_ids_marked == ["p2", "p3"]
        assert group_result.error is None
        assert len(client.updated_pages) == 3

    def test_next_group_still_processed_after_partial_failure(self) -> None:
        """T5: グループ「沼」が部分失敗しても、グループ「島」の処理は継続される
        (across-group continue-on-error、既存挙動を維持)。"""
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
            _page("p10", "島", english_name="Island"),
            _page("p11", "島"),
        ]
        repo, client = _failing_repo(pages, fail_on_page_ids={"p2"})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        assert len(result.results) == 2
        swamp_result = next(r for r in result.results if r.card_name == "沼")
        island_result = next(r for r in result.results if r.card_name == "島")

        assert swamp_result.representative_updated is True
        assert swamp_result.duplicate_page_ids_marked == []
        assert swamp_result.error is not None

        # 島グループは沼グループの失敗と無関係に最後まで正常適用される。
        assert island_result.representative_updated is True
        assert island_result.duplicate_page_ids_marked == ["p11"]
        assert island_result.error is None
        assert "p11" in client.attempted_page_ids


class TestApplyOneGroupFailureMetadata:
    """GroupApplyResult(error!=None)のfailed_operation/failed_completionが、
    NotionAPIError.__cause__の型だけから(メッセージ文字列を一切見ずに)構造的に
    決まることを検証する。Phase 2Iで確立したrepresentative_updated/
    duplicate_page_ids_markedの正確性は変更しない。"""

    def test_representative_known_failure_via_http_status_error(self) -> None:
        """T1: 代表レコード更新自体がHTTPエラー応答で失敗した場合。"""
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _cause_repo(pages, fail_on_page_ids={"p1": _http_status_error()})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert (
            group_result.failed_operation
            == dedupe_cards.FailedGroupOperation.REPRESENTATIVE_UPDATE
        )
        assert group_result.failed_completion == dedupe_cards.GroupWriteCompletion.KNOWN_FAILED
        assert group_result.representative_updated is False
        assert group_result.duplicate_page_ids_marked == []
        assert "p2" not in client.attempted_page_ids

    def test_representative_unknown_completion_via_timeout(self) -> None:
        """T2: 代表レコード更新がタイムアウトで失敗した場合(完了状態不明)。"""
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _cause_repo(pages, fail_on_page_ids={"p1": _timeout_exception()})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert (
            group_result.failed_operation
            == dedupe_cards.FailedGroupOperation.REPRESENTATIVE_UPDATE
        )
        assert group_result.failed_completion == dedupe_cards.GroupWriteCompletion.UNKNOWN
        assert group_result.representative_updated is False
        assert group_result.duplicate_page_ids_marked == []
        assert "p2" not in client.attempted_page_ids

    def test_mark_known_failure_preserves_prefix(self) -> None:
        """T3/T9: 代表成功→A成功→BでHTTPエラー失敗→Cは未試行。"""
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
            _page("p3", "沼"),
            _page("p4", "沼"),
        ]
        repo, client = _cause_repo(pages, fail_on_page_ids={"p3": _http_status_error()})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.representative_updated is True
        assert group_result.duplicate_page_ids_marked == ["p2"]
        assert group_result.failed_operation == dedupe_cards.FailedGroupOperation.MARK_MERGED
        assert group_result.failed_completion == dedupe_cards.GroupWriteCompletion.KNOWN_FAILED
        assert client.attempted_page_ids == ["p1", "p2", "p3"]  # p4は未試行

    def test_mark_unknown_completion_preserves_prefix(self) -> None:
        """T4/T9: 代表成功→A成功→Bでタイムアウト失敗→Cは未試行。"""
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
            _page("p3", "沼"),
            _page("p4", "沼"),
        ]
        repo, client = _cause_repo(pages, fail_on_page_ids={"p3": _timeout_exception()})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.representative_updated is True
        assert group_result.duplicate_page_ids_marked == ["p2"]
        assert group_result.failed_operation == dedupe_cards.FailedGroupOperation.MARK_MERGED
        assert group_result.failed_completion == dedupe_cards.GroupWriteCompletion.UNKNOWN
        assert client.attempted_page_ids == ["p1", "p2", "p3"]  # p4は未試行

    def test_complete_success_has_no_failure_metadata(self) -> None:
        """T5: 全件成功時はfailed_operation/failed_completion/errorすべてNone
        (Phase 2Iのresultと完全互換)。"""
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.error is None
        assert group_result.failed_operation is None
        assert group_result.failed_completion is None

    def test_cause_less_error_defaults_to_unknown(self) -> None:
        """T6: causeを持たないbare NotionAPIError(既存FailingNotionClient)は、
        メッセージから推測せず安全側のUNKNOWNへ倒す。"""
        pages = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        repo, client = _failing_repo(pages, fail_on_page_ids={"p1"})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        group_result = result.results[0]
        assert group_result.failed_completion == dedupe_cards.GroupWriteCompletion.UNKNOWN

    def test_classification_is_independent_of_message_text(self) -> None:
        """T7: メッセージ文言が何であっても、__cause__の型だけで分類されることを
        証明する(文字列解析への依存が無いことの直接的な証拠)。"""

        class MisleadingTextClient(FakeNotionClient):
            def update_page(self, page_id: str, properties: dict) -> dict:
                if page_id == "p1":
                    raise NotionAPIError(
                        "タイムアウトしましたが実際には成功している可能性があります"
                    ) from _http_status_error()
                if page_id == "p3":
                    raise NotionAPIError(
                        "確実に失敗しました(400 Bad Request)"
                    ) from _timeout_exception()
                return super().update_page(page_id, properties)

        # ケース1: メッセージは「タイムアウト」を含むが、causeはHTTPStatusError → KNOWN_FAILED
        pages_a = [_page("p1", "沼", english_name="Swamp"), _page("p2", "沼")]
        client_a = MisleadingTextClient(pages_a)
        repo_a = DedupeRepository(client_a, DATA_SOURCE_ID)
        plan_a = dedupe_cards.build_dedupe_plan(repo_a)
        result_a = dedupe_cards.execute_dedupe_plan(plan_a, repo_a)
        assert (
            result_a.results[0].failed_completion == dedupe_cards.GroupWriteCompletion.KNOWN_FAILED
        )

        # ケース2: メッセージは確定的な失敗を装うが、causeはTimeoutException → UNKNOWN
        pages_b = [
            _page("p3", "島", english_name="Island"),
            _page("p4", "島"),
        ]
        client_b = MisleadingTextClient(pages_b)
        repo_b = DedupeRepository(client_b, DATA_SOURCE_ID)
        plan_b = dedupe_cards.build_dedupe_plan(repo_b)
        result_b = dedupe_cards.execute_dedupe_plan(plan_b, repo_b)
        assert result_b.results[0].failed_completion == dedupe_cards.GroupWriteCompletion.UNKNOWN

    def test_across_group_failure_metadata_preservation(self) -> None:
        """T8: グループ「沼」が完了状態不明な部分失敗をしても、グループ「島」は
        正常に完全成功として処理される(両方のstructured metadataが正しく保持される)。"""
        pages = [
            _page("p1", "沼", english_name="Swamp"),
            _page("p2", "沼"),
            _page("p10", "島", english_name="Island"),
            _page("p11", "島"),
        ]
        repo, client = _cause_repo(pages, fail_on_page_ids={"p2": _timeout_exception()})
        plan = dedupe_cards.build_dedupe_plan(repo)

        result = dedupe_cards.execute_dedupe_plan(plan, repo)

        swamp_result = next(r for r in result.results if r.card_name == "沼")
        island_result = next(r for r in result.results if r.card_name == "島")

        assert swamp_result.representative_updated is True
        assert swamp_result.duplicate_page_ids_marked == []
        assert swamp_result.failed_operation == dedupe_cards.FailedGroupOperation.MARK_MERGED
        assert swamp_result.failed_completion == dedupe_cards.GroupWriteCompletion.UNKNOWN

        assert island_result.representative_updated is True
        assert island_result.duplicate_page_ids_marked == ["p11"]
        assert island_result.error is None
        assert island_result.failed_operation is None
        assert island_result.failed_completion is None

    def test_group_apply_result_rejects_inconsistent_failure_metadata(self) -> None:
        """GroupApplyResultの整合性不変条件(§14)を直接固定する。"""
        with pytest.raises(ValueError):
            dedupe_cards.GroupApplyResult(
                card_name="沼",
                representative_page_id="p1",
                representative_updated=True,
                error="失敗しました",
                failed_operation=None,
                failed_completion=None,
            )
        with pytest.raises(ValueError):
            dedupe_cards.GroupApplyResult(
                card_name="沼",
                representative_page_id="p1",
                representative_updated=True,
                failed_operation=dedupe_cards.FailedGroupOperation.MARK_MERGED,
                failed_completion=None,
            )


class TestPriceLinkMergeHistoryNote:
    def test_price_difference_is_recorded_in_history(self) -> None:
        duplicate_pages = [_page("p2", "沼", price=1800, commander_tags=["ディサ"])]

        note = dedupe_cards.build_merge_history_note(
            duplicate_pages, existing_note=None, today="2026-07-12"
        )

        assert note is not None
        assert "[重複統合履歴 2026-07-12]" in note
        assert "1,800円" in note
        assert "元ページID: p2" in note
        assert "ディサ" in note

    def test_link_difference_is_recorded_in_history(self) -> None:
        duplicate_pages = [_page("p2", "沼", link="https://example.com/a")]

        note = dedupe_cards.build_merge_history_note(
            duplicate_pages, existing_note=None, today="2026-07-12"
        )

        assert note is not None
        assert "https://example.com/a" in note

    def test_missing_price_and_link_render_as_unknown_or_none(self) -> None:
        duplicate_pages = [_page("p2", "沼")]

        note = dedupe_cards.build_merge_history_note(
            duplicate_pages, existing_note=None, today="2026-07-12"
        )

        assert note is not None
        assert "販売価格: 不明" in note
        assert "販売リンク: なし" in note

    def test_existing_note_is_preserved_not_overwritten(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", note="既存の手動メモ"),
            _page("p2", "沼", price=1800),
        ]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)

        dedupe_cards.execute_dedupe_plan(plan, repo)

        p1_update = next(props for page_id, props in client.updated_pages if page_id == "p1")
        note_text = p1_update["メモ"]["rich_text"][0]["text"]["content"]
        assert note_text.startswith("既存の手動メモ")
        assert "[重複統合履歴" in note_text

    def test_same_duplicate_id_history_is_not_appended_twice(self) -> None:
        existing_note = "[重複統合履歴 2026-07-12]\n統合元:\n- ページ: url\n  元ページID: p2\n  ..."
        duplicate_pages = [_page("p2", "沼", price=1800)]

        note = dedupe_cards.build_merge_history_note(
            duplicate_pages, existing_note=existing_note, today="2026-07-12"
        )

        assert note is None

    def test_representative_price_and_link_are_never_overwritten(self) -> None:
        pages = [
            _page(
                "p1",
                "沼",
                english_name="Swamp",
                price=3500,
                link="https://example.com/rep",
            ),
            _page("p2", "沼", price=1800, link="https://example.com/dup"),
        ]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)

        dedupe_cards.execute_dedupe_plan(plan, repo)

        p1_update = next(props for page_id, props in client.updated_pages if page_id == "p1")
        assert "販売価格" not in p1_update
        assert "販売リンク" not in p1_update

    def test_rerun_after_full_merge_appends_no_additional_history(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", price=3500),
            _page("p2", "沼", price=1800),
        ]
        repo, client = _repo(pages)
        plan = dedupe_cards.build_dedupe_plan(repo)
        dedupe_cards.execute_dedupe_plan(plan, repo)

        # p2に統合済みフラグが立った状態を模擬(execute側の更新結果を反映)
        client.pages[1]["properties"]["統合済み"]["checkbox"] = True
        updated_count_before = len(client.updated_pages)

        repo2 = DedupeRepository(client, DATA_SOURCE_ID)
        plan2 = dedupe_cards.build_dedupe_plan(repo2)
        dedupe_cards.execute_dedupe_plan(plan2, repo2)

        assert len(client.updated_pages) == updated_count_before
