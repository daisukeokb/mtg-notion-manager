from __future__ import annotations

import json

import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.services import title_update_dry_run as planner

ARTICLE_DECK_ID = "deck-1"


def _write_manifest(tmp_path, entries: list[dict], schema_version: int = 1) -> Path:  # type: ignore[no-untyped-def]  # noqa: F821
    data = {
        "schema_version": schema_version,
        "purpose": "plan_existing_card_title_updates",
        "source_audit_report": "reports/audit-strixhaven-cards-20260714-125436.json",
        "entries": entries,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _entry(**overrides: object) -> dict:
    base = {
        "page_id": "00000000-0000-0000-0000-000000000001",
        "expected_current_title": "Elusive Otter",
        "confirmed_new_title": "神出鬼没のカワウソ",
        "expected_english_name": "Elusive Otter",
        "source_deck_ids": [ARTICLE_DECK_ID],
        "verification_status": "human_confirmed",
        "verification_actor": "user",
        "verification_note": "Japanese card title explicitly confirmed by the user",
    }
    base.update(overrides)
    return base


class TestManifestLoader:
    def test_arbitrary_valid_entry_count_loads(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entries = [
            _entry(page_id=f"page-{i}", expected_current_title=f"Card {i}") for i in range(3)
        ]
        path = _write_manifest(tmp_path, entries)

        manifest = planner.load_confirmed_title_update_manifest(path)

        assert len(manifest.entries) == 3

    def test_expected_entry_count_seven_is_enforced(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = _write_manifest(tmp_path, [_entry()])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path, expected_entry_count=7)

    def test_matching_expected_entry_count_passes(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entries = [
            _entry(page_id=f"page-{i}", expected_current_title=f"Card {i}") for i in range(7)
        ]
        path = _write_manifest(tmp_path, entries)

        manifest = planner.load_confirmed_title_update_manifest(path, expected_entry_count=7)

        assert len(manifest.entries) == 7

    def test_missing_page_id_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry()
        del entry["page_id"]
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_duplicate_page_id_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = _write_manifest(tmp_path, [_entry(), _entry()])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_missing_current_title_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry()
        del entry["expected_current_title"]
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_missing_new_title_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry()
        del entry["confirmed_new_title"]
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_missing_expected_english_name_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry()
        del entry["expected_english_name"]
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_missing_source_deck_ids_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry(source_deck_ids=[])
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_non_human_confirmed_status_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry(verification_status="auto_guessed")
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_same_current_and_new_title_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        entry = _entry(confirmed_new_title="Elusive Otter")
        path = _write_manifest(tmp_path, [entry])

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_unsupported_schema_version_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = _write_manifest(tmp_path, [_entry()], schema_version=999)

        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(path)

    def test_missing_file_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(planner.TitleUpdateManifestConfigError):
            planner.load_confirmed_title_update_manifest(tmp_path / "does-not-exist.json")


# --- dry-run planner用フェイク -------------------------------------------------


class _FakeUnderlyingClient:
    def __init__(
        self,
        pages: dict[str, dict] | None = None,
        title_query_results: dict[str, list[dict]] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.title_query_results = title_query_results or {}
        self.title_query_calls: list[tuple[str, str]] = []
        self.get_page_calls: list[str] = []
        self.create_page_calls: list[tuple] = []
        self.update_page_calls: list[tuple] = []
        self.update_data_source_schema_calls: list[tuple] = []

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls.append(page_id)
        if page_id not in self.pages:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました (404): {page_id}")
        return self.pages[page_id]

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
        self.title_query_calls.append((data_source_id, title))
        return self.title_query_results.get(title, [])

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return []

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        return []

    def read_relation_ids(self, properties: dict, page_id: str, property_name: str) -> list[str]:
        prop = properties.get(property_name, {})
        return [item["id"] for item in prop.get("relation", [])]

    def create_page(self, *args: object, **kwargs: object) -> dict:
        self.create_page_calls.append((args, kwargs))
        raise AssertionError("フェイクのcreate_page()が呼ばれた(書き込みガード漏れ)")

    def update_page(self, *args: object, **kwargs: object) -> dict:
        self.update_page_calls.append((args, kwargs))
        raise AssertionError("フェイクのupdate_page()が呼ばれた(書き込みガード漏れ)")

    def update_data_source_schema(self, *args: object, **kwargs: object) -> dict:
        self.update_data_source_schema_calls.append((args, kwargs))
        raise AssertionError(
            "フェイクのupdate_data_source_schema()が呼ばれた(書き込みガード漏れ)"
        )


def _card_page(
    page_id: str, title: str | None, english_name: str | None, deck_ids: list[str]
) -> dict:
    return {
        "id": page_id,
        "archived": False,
        "in_trash": False,
        "properties": {
            planner.TITLE_PROPERTY: (
                {"type": "title", "title": [{"plain_text": title}]}
                if title is not None
                else {"type": "title", "title": []}
            ),
            planner.ENGLISH_NAME_PROPERTY: (
                {"type": "rich_text", "rich_text": [{"plain_text": english_name}]}
                if english_name is not None
                else {"type": "rich_text", "rich_text": []}
            ),
            planner.DECKS_RELATION_PROPERTY: {"relation": [{"id": d} for d in deck_ids]},
        },
    }


def _deck_page(page_id: str, card_ids: list[str]) -> dict:
    return {
        "id": page_id,
        "properties": {
            planner.COMMANDER_CARDS_RELATION_PROPERTY: {
                "relation": [{"id": c} for c in card_ids]
            },
        },
    }


def _happy_path_setup():  # type: ignore[no-untyped-def]
    card = _card_page("card-1", "Elusive Otter", "Elusive Otter", ["deck-1"])
    deck = _deck_page("deck-1", ["card-1"])
    underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
    entry = _entry(page_id="card-1", source_deck_ids=["deck-1"])
    return underlying, entry


class TestDryRunTargetIdentification:
    def test_target_identified_by_page_id_only(self) -> None:
        underlying, entry = _happy_path_setup()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        manifest = planner.ConfirmedTitleUpdateManifest(
            schema_version=1, purpose="test", source_audit_report=None, entries=[]
        )
        manifest.entries.append(_to_entry(entry))

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert underlying.get_page_calls[0] == "card-1"
        assert report.entries[0].page_id == "card-1"

    def test_title_search_does_not_determine_target_page(self) -> None:
        """query_data_source_by_titleは同名衝突確認にのみ使う。対象特定には使わない。"""
        underlying, entry = _happy_path_setup()
        # 新タイトルで検索すると別の(無関係な)ページがヒットする状況を作る。
        underlying.title_query_results["神出鬼没のカワウソ"] = [
            {"id": "unrelated-page", "properties": {}}
        ]
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        manifest = planner.ConfirmedTitleUpdateManifest(
            schema_version=1, purpose="test", source_audit_report=None, entries=[_to_entry(entry)]
        )

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        # 対象は依然としてマニフェストのpage_id(card-1)であり、
        # 検索でヒットしたunrelated-pageに差し替わっていないこと。
        assert report.entries[0].page_id == "card-1"
        # 同名ページが見つかったため、blocking_same_titleとして扱われる。
        assert report.entries[0].same_title_check.classification == "blocking_same_title"
        assert not report.entries[0].eligible_for_future_update


def _to_entry(d: dict) -> planner.ConfirmedTitleUpdateEntry:
    return planner.ConfirmedTitleUpdateEntry(
        page_id=d["page_id"],
        expected_current_title=d["expected_current_title"],
        confirmed_new_title=d["confirmed_new_title"],
        expected_english_name=d["expected_english_name"],
        source_deck_ids=list(d["source_deck_ids"]),
        verification_status=d["verification_status"],
        verification_actor=d["verification_actor"],
        verification_note=d["verification_note"],
    )


class TestDryRunBlocking:
    def test_current_title_mismatch_blocks_entry(self) -> None:
        card = _card_page("card-1", "Different Title", "Elusive Otter", ["deck-1"])
        deck = _deck_page("deck-1", ["card-1"])
        underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entry = _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"]))
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [entry])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].current_title_matches is False
        assert not report.entries[0].eligible_for_future_update
        assert report.all_or_nothing_eligible is False

    def test_english_name_mismatch_blocks_entry(self) -> None:
        card = _card_page("card-1", "Elusive Otter", "Different English Name", ["deck-1"])
        deck = _deck_page("deck-1", ["card-1"])
        underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entry = _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"]))
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [entry])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].english_name_matches is False
        assert not report.entries[0].eligible_for_future_update

    def test_existing_same_title_page_blocks_entry(self) -> None:
        underlying, entry = _happy_path_setup()
        other_page_title_prop = {
            "type": "title",
            "title": [{"plain_text": "神出鬼没のカワウソ"}],
        }
        underlying.title_query_results["神出鬼没のカワウソ"] = [
            {"id": "other-page", "properties": {planner.TITLE_PROPERTY: other_page_title_prop}}
        ]
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [_to_entry(entry)])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].same_title_check.classification == "blocking_same_title"
        assert not report.entries[0].eligible_for_future_update

    def test_missing_strixhaven_deck_relation_blocks_entry(self) -> None:
        card = _card_page("card-1", "Elusive Otter", "Elusive Otter", [])  # relationが空
        deck = _deck_page("deck-1", [])
        underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entry = _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"]))
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [entry])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].relation_snapshot.source_deck_ids_present is False
        assert not report.entries[0].eligible_for_future_update

    def test_bidirectional_relation_inconsistency_blocks_entry(self) -> None:
        # カード側は採用デッキを持つが、デッキ側の採用カードに逆参照が無い。
        card = _card_page("card-1", "Elusive Otter", "Elusive Otter", ["deck-1"])
        deck = _deck_page("deck-1", [])  # card-1を含まない
        underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entry = _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"]))
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [entry])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].relation_snapshot.deck_to_card_consistent is False
        assert not report.entries[0].eligible_for_future_update

    def test_extra_unrelated_deck_relation_is_not_treated_as_anomalous(self) -> None:
        """監査対象外の別デッキが追加されていても、それだけでは異常としない。"""
        card = _card_page(
            "card-1", "Elusive Otter", "Elusive Otter", ["deck-1", "unrelated-deck"]
        )
        deck = _deck_page("deck-1", ["card-1"])
        underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entry = _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"]))
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [entry])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].relation_snapshot.source_deck_ids_present is True
        assert report.entries[0].eligible_for_future_update

    def test_single_failure_blocks_all_or_nothing(self) -> None:
        good_card = _card_page("card-1", "Elusive Otter", "Elusive Otter", ["deck-1"])
        bad_card = _card_page("card-2", "Wrong Title", "Forum of Amity", ["deck-1"])
        deck = _deck_page("deck-1", ["card-1", "card-2"])
        underlying = _FakeUnderlyingClient(
            pages={"card-1": good_card, "card-2": bad_card, "deck-1": deck}
        )
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entries = [
            _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"])),
            _to_entry(
                _entry(
                    page_id="card-2",
                    expected_current_title="Forum of Amity",
                    confirmed_new_title="アミティの公開討論所",
                    expected_english_name="Forum of Amity",
                    source_deck_ids=["deck-1"],
                )
            ),
        ]
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, entries)

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        assert report.entries[0].eligible_for_future_update is True
        assert report.entries[1].eligible_for_future_update is False
        assert report.all_or_nothing_eligible is False
        assert report.eligible_count == 1
        assert report.blocked_count == 1


class TestRelationPagination:
    def test_relation_ids_are_read_via_shared_pagination_helper(self) -> None:
        """25件超のrelationでも既存のページング処理(read_relation_ids)を経由することを確認する。"""
        many_ids = [f"card-{i}" for i in range(30)]
        card = _card_page("card-1", "Elusive Otter", "Elusive Otter", ["deck-1"])
        deck = _deck_page("deck-1", many_ids + ["card-1"])
        underlying = _FakeUnderlyingClient(pages={"card-1": card, "deck-1": deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        entry = _to_entry(_entry(page_id="card-1", source_deck_ids=["deck-1"]))
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [entry])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )

        deck_detail = report.entries[0].relation_snapshot.deck_relation_details[0]
        assert deck_detail.deck_relation_count == 31
        assert "read_relation_ids" in report.method_call_log


class TestReportOutput:
    def test_json_and_markdown_reports_are_written(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        underlying, entry = _happy_path_setup()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [_to_entry(entry)])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )
        data = planner.to_json_dict(report, write_operations=0, write_attempts=0)

        json_path = planner.write_json_report(data, tmp_path / "out.json")
        md_path = planner.write_markdown_report(data, tmp_path / "out.md")

        assert json_path.exists()
        assert md_path.exists()
        assert data["notion_write_operations"] == 0
        assert data["notion_write_attempts"] == 0


# --- 読み取り専用保証 ---------------------------------------------------------


class _FakeHttpxResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class TestReadOnlyGuarantees:
    def test_write_capable_repository_is_never_imported(self) -> None:
        import ast
        from pathlib import Path

        source = Path("src/mtg_notion_manager/services/title_update_dry_run.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.name)
        forbidden = {"DedupeRepository", "CardRepository", "NotionWriter"}
        assert not (imported_names & forbidden)

    def test_write_methods_raise_without_reaching_underlying_client(self) -> None:
        underlying, _ = _happy_path_setup()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]

        for name in ("create_page", "update_page", "update_data_source_schema"):
            with pytest.raises(planner.ReadOnlyGuardError):
                getattr(client, name)()

        assert underlying.create_page_calls == []
        assert underlying.update_page_calls == []
        assert underlying.update_data_source_schema_calls == []

    def test_http_write_requests_are_rejected(self) -> None:
        client = NotionClient(api_key="secret_test")
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, path: str, **kwargs: object) -> _FakeHttpxResponse:
            calls.append((method, path))
            return _FakeHttpxResponse({"results": [], "has_more": False})

        client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001
        planner.install_http_write_guard(client)

        with pytest.raises(planner.ReadOnlyGuardError):
            client.update_page("p1", {"カード名": {"title": []}})
        assert calls == []

        with pytest.raises(planner.ReadOnlyGuardError):
            client.create_page("ds-1", {"カード名": {"title": []}})
        assert calls == []

    def test_get_and_query_requests_are_allowed(self) -> None:
        client = NotionClient(api_key="secret_test")
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, path: str, **kwargs: object) -> _FakeHttpxResponse:
            calls.append((method, path))
            return _FakeHttpxResponse({"results": [], "has_more": False})

        client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001
        planner.install_http_write_guard(client)

        client.get_page("p1")
        client.query_data_source_all("ds-1")

        assert calls == [("GET", "/pages/p1"), ("POST", "/data_sources/ds-1/query")]

    def test_dry_run_writes_zero_operations_and_zero_attempts(self) -> None:
        underlying, entry = _happy_path_setup()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        manifest = planner.ConfirmedTitleUpdateManifest(1, "test", None, [_to_entry(entry)])

        report = planner.build_title_update_dry_run_plan(
            client, "card-ds-1", manifest, "manifest.json"
        )
        data = planner.to_json_dict(report, write_operations=0, write_attempts=0)

        assert data["notion_write_operations"] == 0
        assert data["notion_write_attempts"] == 0
