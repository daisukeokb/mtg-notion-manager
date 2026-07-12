from __future__ import annotations

import json
from pathlib import Path

from mtg_notion_manager.notion.dedupe_repository import DedupeRepository
from mtg_notion_manager.services import apply_price_link_dedupe as apply_mod
from mtg_notion_manager.services.audit_duplicates import ExclusionList
from mtg_notion_manager.services.review_duplicate_conflicts import (
    CATEGORY_MANUAL,
    CATEGORY_PRICE_ONLY,
)

DATA_SOURCE_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


class FakeNotionClient:
    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = pages or []
        self.update_calls: list[tuple] = []
        self.schema_update_calls: list[tuple] = []
        self.fetch_all_calls = 0

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        self.fetch_all_calls += 1
        return self.pages

    def get_data_source(self, data_source_id: str) -> dict:
        return {"properties": {"所持枚数": {"type": "number"}, "統合済み": {"type": "checkbox"}}}

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        self.schema_update_calls.append((data_source_id, properties))
        return {}

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.update_calls.append((page_id, properties))
        for page in self.pages:
            if page["id"] == page_id:
                for name, value in properties.items():
                    page["properties"][name] = {**page["properties"].get(name, {}), **value}
                    page["properties"][name]["type"] = next(iter(value.keys()))
        return {"id": page_id, "url": f"https://notion.so/{page_id}"}

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        return []


def _page(
    page_id: str,
    name: str,
    english_name: str | None = None,
    price: float | None = None,
    link: str | None = None,
    commander_tags: list[str] | None = None,
    merged: bool = False,
    owned: bool = False,
    created_time: str | None = None,
    last_edited_time: str | None = None,
    note: str = "",
) -> dict:
    offset = sum(ord(c) for c in page_id) % 50
    created_time = created_time or f"2024-01-01T00:{offset:02d}:00.000Z"
    last_edited_time = last_edited_time or f"2024-06-01T00:{offset:02d}:00.000Z"

    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name}]},
        "所持": {"type": "checkbox", "checkbox": owned},
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
    return DedupeRepository(client, DATA_SOURCE_ID), client


def _write_targets_report(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _target_item(
    card_name: str,
    page_ids: list[str],
    category: str = CATEGORY_PRICE_ONLY,
    prices: list[float] | None = None,
    links: list[str] | None = None,
    merged_deck_relation_count: int = 0,
) -> dict:
    return {
        "card_name": card_name,
        "review_category": category,
        "duplicate_count": len(page_ids),
        "prices": prices or [],
        "links": links or [],
        "merged_deck_relation_count": merged_deck_relation_count,
        "pages": [{"page_id": pid} for pid in page_ids],
    }


class TestLoadPriceLinkTargets:
    def test_only_price_only_and_manual_are_loaded(self, tmp_path: Path) -> None:
        report_path = _write_targets_report(
            tmp_path,
            [
                _target_item("沼", ["p1", "p2"], category="price_only"),
                _target_item("血染めのぬかるみ", ["p3", "p4"], category="manual_representative"),
                _target_item("特殊カード", ["p5", "p6"], category="special_version"),
            ],
        )

        targets = apply_mod.load_price_link_targets(report_path)

        assert {t.card_name for t in targets} == {"沼", "血染めのぬかるみ"}

    def test_manual_representative_override_is_applied(self, tmp_path: Path) -> None:
        report_path = _write_targets_report(
            tmp_path,
            [_target_item("血染めのぬかるみ", ["p3", "p4"], category="manual_representative")],
        )

        targets = apply_mod.load_price_link_targets(
            report_path, manual_representative_overrides={"血染めのぬかるみ": "p3"}
        )

        assert targets[0].representative_page_id == "p3"


class TestSelectCanaryTargets:
    def test_selects_only_two_page_price_diff_only_groups(self) -> None:
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "A", CATEGORY_PRICE_ONLY, ["p1", "p2"], [100, 200], [], 0
            ),
            apply_mod.PriceLinkTargetGroup(
                "B", CATEGORY_PRICE_ONLY, ["p3", "p4", "p5"], [100, 200], [], 0
            ),
            apply_mod.PriceLinkTargetGroup(
                "C", CATEGORY_PRICE_ONLY, ["p6", "p7"], [100], ["l1", "l2"], 0
            ),
        ]

        canary = apply_mod.select_canary_targets(targets, limit=3)

        assert [t.card_name for t in canary] == ["A"]

    def test_ranks_by_deck_relation_count_then_name(self) -> None:
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "Z", CATEGORY_PRICE_ONLY, ["p1", "p2"], [100, 200], [], 5
            ),
            apply_mod.PriceLinkTargetGroup(
                "A", CATEGORY_PRICE_ONLY, ["p3", "p4"], [100, 200], [], 1
            ),
            apply_mod.PriceLinkTargetGroup(
                "B", CATEGORY_PRICE_ONLY, ["p5", "p6"], [100, 200], [], 1
            ),
        ]

        canary = apply_mod.select_canary_targets(targets, limit=3)

        assert [t.card_name for t in canary] == ["A", "B", "Z"]


class TestApplyPriceLinkTargetsDryRun:
    def test_dry_run_does_not_call_update_page(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", price=3500),
            _page("p2", "沼", price=1800),
        ]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=False)

        assert client.update_calls == []
        assert outcomes[0].status == apply_mod.STATUS_PLANNED


class TestApplyPriceLinkTargetsApply:
    def test_applies_price_only_group_and_preserves_representative_price(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", price=3500),
            _page("p2", "沼", price=1800),
        ]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_APPLIED
        p1_update = next(props for page_id, props in client.update_calls if page_id == "p1")
        assert "販売価格" not in p1_update
        assert "1,800円" in p1_update["メモ"]["rich_text"][0]["text"]["content"]

    def test_manual_representative_group_uses_override(self) -> None:
        tie_created = "2024-01-01T00:00:00.000Z"
        tie_edited = "2024-06-01T00:00:00.000Z"
        pages = [
            _page("p1", "血染めのぬかるみ", created_time=tie_created, last_edited_time=tie_edited),
            _page("p2", "血染めのぬかるみ", created_time=tie_created, last_edited_time=tie_edited),
        ]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "血染めのぬかるみ",
                CATEGORY_MANUAL,
                ["p1", "p2"],
                [],
                [],
                0,
                representative_page_id="p1",
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_APPLIED
        assert outcomes[0].representative_page_id == "p1"

    def test_manual_representative_without_override_fails_without_writing(self) -> None:
        tie_created = "2024-01-01T00:00:00.000Z"
        tie_edited = "2024-06-01T00:00:00.000Z"
        pages = [
            _page("p1", "血染めのぬかるみ", created_time=tie_created, last_edited_time=tie_edited),
            _page("p2", "血染めのぬかるみ", created_time=tie_created, last_edited_time=tie_edited),
        ]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "血染めのぬかるみ", CATEGORY_MANUAL, ["p1", "p2"], [], [], 0
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_FAILED
        assert client.update_calls == []

    def test_no_delete_method_exists_on_client(self) -> None:
        pages = [_page("p1", "沼", price=3500), _page("p2", "沼", price=1800)]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert not hasattr(client, "delete_page")


class TestStalenessDetection:
    def test_page_composition_changed_is_skipped(self) -> None:
        pages = [_page("p1", "沼", price=3500), _page("p3", "沼", price=1800)]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_STALE
        assert client.update_calls == []

    def test_category_changed_is_skipped(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", price=3500),
            _page("p2", "沼", english_name="Different", price=1800),
        ]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_STALE
        assert client.update_calls == []

    def test_already_merged_group_is_skipped_as_not_duplicate(self) -> None:
        pages = [_page("p1", "沼", price=3500), _page("p2", "沼", price=1800, merged=True)]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        outcomes = apply_mod.apply_price_link_targets(repo, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_NOT_DUPLICATE
        assert client.update_calls == []


class TestExclusionListApplied:
    def test_excluded_card_name_is_skipped(self) -> None:
        pages = [_page("p1", "沼", price=3500), _page("p2", "沼", price=1800)]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]
        exclusions = ExclusionList(card_names=frozenset({"沼"}))

        outcomes = apply_mod.apply_price_link_targets(
            repo, targets, apply=True, exclusions=exclusions
        )

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_NOT_DUPLICATE
        assert client.update_calls == []


class TestIdempotency:
    def test_rerun_after_apply_appends_no_additional_history(self) -> None:
        pages = [
            _page("p1", "沼", english_name="Swamp", price=3500),
            _page("p2", "沼", price=1800),
        ]
        repo, client = _repo(pages)
        targets = [
            apply_mod.PriceLinkTargetGroup(
                "沼", CATEGORY_PRICE_ONLY, ["p1", "p2"], [1800, 3500], [], 0
            )
        ]

        apply_mod.apply_price_link_targets(repo, targets, apply=True)
        update_count_after_first_run = len(client.update_calls)

        repo2 = DedupeRepository(client, DATA_SOURCE_ID)
        outcomes = apply_mod.apply_price_link_targets(repo2, targets, apply=True)

        assert outcomes[0].status == apply_mod.STATUS_SKIPPED_NOT_DUPLICATE
        assert len(client.update_calls) == update_count_after_first_run


class TestBackupCardDb:
    def test_backup_writes_file_and_verifies_count(self, tmp_path: Path) -> None:
        pages = [_page("p1", "沼", price=3500), _page("p2", "山", price=100)]
        repo, client = _repo(pages)
        repo.load()

        result = apply_mod.backup_card_db(repo, tmp_path, timestamp="20260101-000000")

        assert result.path.exists()
        assert result.count == 2
        assert result.verified_count == 2
        assert result.verified is True

        data = json.loads(result.path.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert {d["card_name"] for d in data} == {"沼", "山"}

    def test_backup_contains_no_secrets(self, tmp_path: Path) -> None:
        pages = [_page("p1", "沼", price=3500)]
        repo, client = _repo(pages)
        repo.load()

        result = apply_mod.backup_card_db(repo, tmp_path, timestamp="20260101-000000")

        content = result.path.read_text(encoding="utf-8")
        assert "secret" not in content.lower()
        assert "api_key" not in content.lower()

    def test_backup_failure_to_verify_is_detectable(self, tmp_path: Path) -> None:
        pages = [_page("p1", "沼", price=3500), _page("p2", "山", price=100)]
        repo, client = _repo(pages)
        repo.load()

        # バックアップ後にNotion側でページが1件消えたケース(検証ロジックの確認用)を模擬
        result = apply_mod.backup_card_db(repo, tmp_path, timestamp="20260101-000000")
        client.pages.pop()
        mismatched = apply_mod.BackupResult(
            path=result.path, count=result.count, verified_count=len(client.pages)
        )

        assert mismatched.verified is False


class TestWritePriceLinkApplyLog:
    def test_writes_json_without_secrets(self, tmp_path: Path) -> None:
        outcomes = [
            apply_mod.GroupApplyOutcome(
                card_name="沼",
                status=apply_mod.STATUS_APPLIED,
                representative_page_id="p1",
                merged_page_ids=["p2"],
                before_snapshot=[{"page_id": "p1"}, {"page_id": "p2"}],
                after_snapshot={"representative_page_id": "p1", "quantity": 2},
                history_note_appended="[重複統合履歴 2026-07-12]\n統合元:\n...",
            )
        ]

        paths = apply_mod.write_price_link_apply_log(
            outcomes,
            targets_report_path="reports/dedupe-review-details-x.json",
            output_dir=tmp_path,
            applied=True,
            timestamp="20260101-000000",
        )

        assert paths.json_path.exists()
        content = paths.json_path.read_text(encoding="utf-8")
        assert "secret" not in content.lower()
        assert "api_key" not in content.lower()

        data = json.loads(content)
        assert data["summary"]["applied"] == 1
        assert data["delete_count"] == 0
        assert data["groups"][0]["card_name"] == "沼"
        assert "重複統合履歴" in data["groups"][0]["history_note_appended"]
