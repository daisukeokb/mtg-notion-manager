from __future__ import annotations

import json
from pathlib import Path

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services import apply_dedupe_plan as apply_mod
from mtg_notion_manager.services.audit_duplicates import ExclusionList

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class FakeNotionClient:
    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = pages or []
        self.update_calls: list[tuple] = []

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self.pages

    def get_data_source(self, data_source_id: str) -> dict:
        return {"properties": {"所持枚数": {"type": "number"}, "統合済み": {"type": "checkbox"}}}

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.update_calls.append((page_id, properties))
        for page in self.pages:
            if page["id"] == page_id:
                for name, value in properties.items():
                    page["properties"][name] = {**page["properties"].get(name, {}), **value}
                    page["properties"][name]["type"] = _infer_type(value)
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        return []


def _infer_type(value: dict) -> str:
    return next(iter(value.keys()))


def _page(
    page_id: str,
    name: str,
    english_name: str | None = None,
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
        "メモ": {"type": "rich_text", "rich_text": []},
    }
    if english_name is not None:
        properties["英語名"] = {"type": "rich_text", "rich_text": [{"plain_text": english_name}]}
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "created_time": created_time,
        "last_edited_time": last_edited_time,
        "properties": properties,
    }


def _repo(pages: list[dict]) -> tuple[DedupeRepository, FakeNotionClient]:
    client = FakeNotionClient(pages)
    return DedupeRepository(client, DATA_SOURCE_ID), client


def _write_report(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _report_item(
    card_name: str, page_ids: list[str], category: str = "auto", deck_relation_count: int = 0
) -> dict:
    return {
        "card_name": card_name,
        "category": category,
        "duplicate_count": len(page_ids),
        "recommended_representative_id": page_ids[0],
        "merged_deck_relation_count": deck_relation_count,
        "pages": [{"page_id": pid} for pid in page_ids],
    }


class TestLoadAuditReport:
    def test_only_auto_classification_is_loaded(self, tmp_path: Path) -> None:
        report_path = _write_report(
            tmp_path,
            [
                _report_item("沼", ["p1", "p2"], category="auto"),
                _report_item("秘儀の印鑑", ["p3", "p4"], category="needs_review"),
                _report_item("血染めのぬかるみ", ["p5", "p6"], category="manual_representative"),
            ],
        )

        groups = apply_mod.load_audit_report(report_path)

        assert [g.card_name for g in groups] == ["沼"]

    def test_needs_review_and_manual_are_never_selected(self, tmp_path: Path) -> None:
        report_path = _write_report(
            tmp_path,
            [
                _report_item("秘儀の印鑑", ["p3", "p4"], category="needs_review"),
                _report_item("血染めのぬかるみ", ["p5", "p6"], category="manual_representative"),
                _report_item("除外カード", ["p7", "p8"], category="excluded"),
            ],
        )

        groups = apply_mod.load_audit_report(report_path)

        assert groups == []


class TestSelectTargetGroups:
    def test_sorts_by_duplicate_count_then_deck_relation_count(self) -> None:
        groups = [
            apply_mod.ReportGroup("大きい", 5, 3, "p1", ["p1"]),
            apply_mod.ReportGroup("小さい", 2, 0, "p2", ["p2"]),
            apply_mod.ReportGroup("中くらい", 2, 2, "p3", ["p3"]),
        ]

        ordered = apply_mod.select_target_groups(groups)

        assert [g.card_name for g in ordered] == ["小さい", "中くらい", "大きい"]

    def test_limit_and_offset(self) -> None:
        groups = [
            apply_mod.ReportGroup(f"card{i}", 2, 0, f"p{i}", [f"p{i}"]) for i in range(5)
        ]

        page = apply_mod.select_target_groups(groups, limit=2, offset=1)

        assert len(page) == 2


class TestApplyDedupeBatchDryRun:
    def test_dry_run_does_not_call_update_page(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", deck_ids=["d1", "d2"]),
        ]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p2", ["p1", "p2"])]

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=False)

        assert client.update_calls == []
        assert outcomes[0].status == apply_mod.STATUS_PLANNED
        assert outcomes[0].representative_page_id == "p2"


class TestApplyDedupeBatchApply:
    def test_applies_and_marks_duplicates(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", deck_ids=["d1", "d2"]),
        ]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p2", ["p1", "p2"])]

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_APPLIED
        assert len(client.update_calls) == 2  # 代表1件 + 統合対象1件

    def test_no_delete_method_is_called_or_exists(self) -> None:
        pages = [_page("p1", "沼", deck_ids=["d1"]), _page("p2", "沼", deck_ids=["d1", "d2"])]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p2", ["p1", "p2"])]

        apply_mod.apply_dedupe_batch(repo, groups, apply=True)

        assert not hasattr(client, "delete_page")

    def test_partial_batch_failure_does_not_stop_other_groups(self) -> None:
        pages = [
            _page("p1", "沼", deck_ids=["d1"]),
            _page("p2", "沼", deck_ids=["d1", "d2"]),
            _page("p3", "山", deck_ids=["d3"]),
            _page("p4", "山", deck_ids=["d3", "d4"]),
        ]
        repo, client = _repo(pages)

        original_update = client.update_page

        def flaky_update(page_id: str, properties: dict) -> dict:
            if page_id == "p1":
                raise NotionAPIError("simulated failure (500)")
            return original_update(page_id, properties)

        # 「沼」グループの代表ページ更新だけ失敗させる。
        client.update_page = flaky_update  # type: ignore[method-assign]

        groups = [
            apply_mod.ReportGroup("沼", 2, 0, "p1", ["p1", "p2"]),
            apply_mod.ReportGroup("山", 2, 0, "p3", ["p3", "p4"]),
        ]

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=True)

        statuses = {o.card_name: o.status for o in outcomes}
        assert statuses["沼"] == apply_mod.STATUS_FAILED
        assert statuses["山"] == apply_mod.STATUS_APPLIED


class TestStalenessDetection:
    def test_category_changed_since_report_is_skipped(self) -> None:
        # 現在は競合が発生している(タイプが異なる)ため、レポート作成時のautoはもう成立しない
        pages = [
            _page("p1", "沼", deck_ids=["d1"]),
            _page("p2", "沼", deck_ids=["d1", "d2"]),
        ]
        pages[0]["properties"]["タイプ"] = {"type": "select", "select": {"name": "土地"}}
        pages[1]["properties"]["タイプ"] = {"type": "select", "select": {"name": "エンチャント"}}
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p1", ["p1", "p2"])]

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_STALE
        assert client.update_calls == []

    def test_page_composition_changed_is_skipped(self) -> None:
        # レポートにはp1,p2があるが、現在はp1,p3が重複グループになっている(異なる構成)
        pages = [
            _page("p1", "沼", deck_ids=["d1"]),
            _page("p3", "沼", deck_ids=["d1", "d2"]),
        ]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p1", ["p1", "p2"])]  # レポートはp1,p2

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_STALE
        assert client.update_calls == []

    def test_already_merged_group_is_skipped_as_not_duplicate(self) -> None:
        pages = [
            _page("p1", "沼", deck_ids=["d1"]),
            _page("p2", "沼", merged=True, deck_ids=["d1"]),
        ]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p1", ["p1", "p2"])]

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_NOT_DUPLICATE
        assert client.update_calls == []


class TestIdempotency:
    def test_rerunning_after_apply_skips_as_no_longer_duplicate(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", deck_ids=["d1"]),
            _page("p2", "沼", english_name="Swamp", deck_ids=["d1", "d2"]),
        ]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p2", ["p1", "p2"])]

        apply_mod.apply_dedupe_batch(repo, groups, apply=True)
        update_count_after_first_run = len(client.update_calls)

        # 統合済みフラグはFakeNotionClient.update_pageの中で既にpages配列に反映されている
        repo2 = DedupeRepository(client, DATA_SOURCE_ID)
        outcomes = apply_mod.apply_dedupe_batch(repo2, groups, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_NOT_DUPLICATE
        assert len(client.update_calls) == update_count_after_first_run  # 追加の書き込みなし


class TestExclusionListApplied:
    def test_excluded_card_name_is_skipped(self) -> None:
        pages = [_page("p1", "沼", deck_ids=["d1"]), _page("p2", "沼", deck_ids=["d1", "d2"])]
        repo, client = _repo(pages)
        groups = [apply_mod.ReportGroup("沼", 2, 0, "p1", ["p1", "p2"])]
        exclusions = ExclusionList(card_names=frozenset({"沼"}))

        outcomes = apply_mod.apply_dedupe_batch(repo, groups, apply=True, exclusions=exclusions)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_STALE
        assert client.update_calls == []


class TestWriteApplyLog:
    def test_writes_json_without_secrets(self, tmp_path: Path) -> None:
        outcomes = [
            apply_mod.GroupApplyOutcome(
                card_name="沼",
                status=apply_mod.STATUS_APPLIED,
                representative_page_id="p1",
                merged_page_ids=["p2"],
                before_snapshot=[{"page_id": "p1"}, {"page_id": "p2"}],
                after_snapshot={"representative_page_id": "p1", "quantity": 2},
            )
        ]

        paths = apply_mod.write_apply_log(
            outcomes,
            audit_report_path="reports/dedupe-audit-x.json",
            output_dir=tmp_path,
            applied=True,
            timestamp="20260101-000000",
        )

        assert paths.json_path.exists()
        content = paths.json_path.read_text(encoding="utf-8")
        assert "secret" not in content.lower()
        assert "api_key" not in content.lower()
        assert "authorization" not in content.lower()

        data = json.loads(content)
        assert data["summary"]["applied"] == 1
        assert data["delete_count"] == 0
        assert data["groups"][0]["card_name"] == "沼"
