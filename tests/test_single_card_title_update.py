from __future__ import annotations

import json

import pytest

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.services import single_card_title_update as single
from mtg_notion_manager.services import title_update_dry_run as planner

FAKE_PAGE_ID = "00000000-0000-0000-0000-000000000001"
FAKE_DECK_ID = "00000000-0000-0000-0000-000000000002"
FAKE_OTHER_DECK_ID = "00000000-0000-0000-0000-000000000009"


def _manifest_dict(**overrides: object) -> dict:
    entry = {
        "page_id": FAKE_PAGE_ID,
        "expected_current_title": "Elusive Otter",
        "confirmed_new_title": "神出鬼没のカワウソ",
        "expected_english_name": "Elusive Otter",
        "source_deck_ids": [FAKE_DECK_ID],
        "verification_status": "human_confirmed",
        "verification_actor": "user",
        "verification_note": "Japanese card title explicitly confirmed by the user",
    }
    entry.update(overrides)
    return {
        "schema_version": 1,
        "purpose": "apply_single_confirmed_card_title_update",
        "entries": [entry],
    }


def _write_manifest(tmp_path, data: dict, entries: list[dict] | None = None):  # type: ignore[no-untyped-def]
    if entries is not None:
        data = {**data, "entries": entries}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _entry(**overrides: object) -> planner.ConfirmedTitleUpdateEntry:
    base = dict(
        page_id=FAKE_PAGE_ID,
        expected_current_title="Elusive Otter",
        confirmed_new_title="神出鬼没のカワウソ",
        expected_english_name="Elusive Otter",
        source_deck_ids=[FAKE_DECK_ID],
        verification_status="human_confirmed",
        verification_actor="user",
        verification_note="note",
    )
    base.update(overrides)
    return planner.ConfirmedTitleUpdateEntry(**base)  # type: ignore[arg-type]


# --- マニフェスト(1件専用) ---------------------------------------------------


class TestSingleUpdateManifestLoader:
    def test_single_entry_loads(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = _write_manifest(tmp_path, _manifest_dict())
        entry = single.load_single_update_manifest(path)
        assert entry.page_id == FAKE_PAGE_ID

    def test_two_entries_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        data = _manifest_dict()
        entries = data["entries"] * 2
        entries = [
            {**entries[0], "page_id": FAKE_PAGE_ID},
            {**entries[1], "page_id": FAKE_DECK_ID},
        ]
        path = _write_manifest(tmp_path, data, entries=entries)
        with pytest.raises(planner.TitleUpdateManifestConfigError):
            single.load_single_update_manifest(path)

    def test_zero_entries_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = _write_manifest(tmp_path, _manifest_dict(), entries=[])
        with pytest.raises(planner.TitleUpdateManifestConfigError):
            single.load_single_update_manifest(path)

    def test_non_human_confirmed_fails(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        data = _manifest_dict()
        data["entries"][0]["verification_status"] = "auto_guessed"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(planner.TitleUpdateManifestConfigError):
            single.load_single_update_manifest(path)


# --- タイトルプロパティ名の解決 -------------------------------------------------


class TestResolveTitlePropertyName:
    def test_resolves_from_schema(self) -> None:
        schema = {"properties": {"カード名": {"type": "title"}, "英語名": {"type": "rich_text"}}}
        assert single.resolve_title_property_name(schema) == "カード名"

    def test_zero_title_properties_raises(self) -> None:
        schema = {"properties": {"英語名": {"type": "rich_text"}}}
        with pytest.raises(single.SingleUpdateConfigError):
            single.resolve_title_property_name(schema)

    def test_multiple_title_properties_raises(self) -> None:
        schema = {"properties": {"A": {"type": "title"}, "B": {"type": "title"}}}
        with pytest.raises(single.SingleUpdateConfigError):
            single.resolve_title_property_name(schema)


# --- フェイクのReadOnlyNotionClient裏側 ----------------------------------------


class _FakeUnderlyingClient:
    def __init__(
        self,
        pages: dict[str, dict] | None = None,
        schema: dict | None = None,
        title_query_results: dict[str, list[dict]] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.schema = schema or {
            "properties": {"カード名": {"type": "title"}, "英語名": {"type": "rich_text"}}
        }
        self.title_query_results = title_query_results or {}
        self.get_page_calls: list[str] = []
        self.create_page_calls: list[tuple] = []
        self.update_page_calls: list[tuple] = []
        self.update_data_source_schema_calls: list[tuple] = []

    def get_page(self, page_id: str) -> dict:
        self.get_page_calls.append(page_id)
        if page_id not in self.pages:
            raise NotionAPIError(f"Notion API呼び出しに失敗しました (404): {page_id}")
        return self.pages[page_id]

    def get_data_source(self, data_source_id: str) -> dict:
        return self.schema

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
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
    page_id: str,
    title: str | None,
    english_name: str | None,
    deck_ids: list[str],
    archived: bool = False,
    in_trash: bool = False,
    last_edited_time: str | None = "2026-07-14T00:00:00.000Z",
) -> dict:
    return {
        "id": page_id,
        "archived": archived,
        "in_trash": in_trash,
        "last_edited_time": last_edited_time,
        "properties": {
            "カード名": (
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


def _happy_underlying() -> _FakeUnderlyingClient:
    card = _card_page(FAKE_PAGE_ID, "Elusive Otter", "Elusive Otter", [FAKE_DECK_ID])
    deck = _deck_page(FAKE_DECK_ID, [FAKE_PAGE_ID])
    return _FakeUnderlyingClient(pages={FAKE_PAGE_ID: card, FAKE_DECK_ID: deck})


# --- preflight ---------------------------------------------------------------


class TestPreflight:
    def test_eligible_when_everything_matches(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.eligible_for_future_update is True
        assert result.blocking_reasons == []
        assert result.operation_digest

    def test_current_title_mismatch_blocks(self) -> None:
        card = _card_page(FAKE_PAGE_ID, "Different Title", "Elusive Otter", [FAKE_DECK_ID])
        deck = _deck_page(FAKE_DECK_ID, [FAKE_PAGE_ID])
        underlying = _FakeUnderlyingClient(pages={FAKE_PAGE_ID: card, FAKE_DECK_ID: deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.current_title_matches is False
        assert result.eligible_for_future_update is False

    def test_english_name_mismatch_blocks(self) -> None:
        card = _card_page(FAKE_PAGE_ID, "Elusive Otter", "Wrong Name", [FAKE_DECK_ID])
        deck = _deck_page(FAKE_DECK_ID, [FAKE_PAGE_ID])
        underlying = _FakeUnderlyingClient(pages={FAKE_PAGE_ID: card, FAKE_DECK_ID: deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.english_name_matches is False
        assert result.eligible_for_future_update is False

    def test_existing_same_title_page_blocks(self) -> None:
        underlying = _happy_underlying()
        underlying.title_query_results["神出鬼没のカワウソ"] = [
            {
                "id": "other-page",
                "properties": {
                    "カード名": {"type": "title", "title": [{"plain_text": "神出鬼没のカワウソ"}]}
                },
            }
        ]
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.same_title_check.classification == "blocking_same_title"
        assert result.eligible_for_future_update is False

    def test_missing_deck_relation_blocks(self) -> None:
        card = _card_page(FAKE_PAGE_ID, "Elusive Otter", "Elusive Otter", [])
        deck = _deck_page(FAKE_DECK_ID, [])
        underlying = _FakeUnderlyingClient(pages={FAKE_PAGE_ID: card, FAKE_DECK_ID: deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.relation_snapshot.source_deck_ids_present is False
        assert result.eligible_for_future_update is False

    def test_relation_inconsistency_blocks(self) -> None:
        card = _card_page(FAKE_PAGE_ID, "Elusive Otter", "Elusive Otter", [FAKE_DECK_ID])
        deck = _deck_page(FAKE_DECK_ID, [])  # 逆参照なし
        underlying = _FakeUnderlyingClient(pages={FAKE_PAGE_ID: card, FAKE_DECK_ID: deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.relation_snapshot.deck_to_card_consistent is False
        assert result.eligible_for_future_update is False

    def test_archived_page_blocks(self) -> None:
        card = _card_page(
            FAKE_PAGE_ID, "Elusive Otter", "Elusive Otter", [FAKE_DECK_ID], archived=True
        )
        deck = _deck_page(FAKE_DECK_ID, [FAKE_PAGE_ID])
        underlying = _FakeUnderlyingClient(pages={FAKE_PAGE_ID: card, FAKE_DECK_ID: deck})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.is_archived_or_trashed is True
        assert result.eligible_for_future_update is False

    def test_page_not_found_blocks(self) -> None:
        underlying = _FakeUnderlyingClient(pages={})
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        result = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert result.eligible_for_future_update is False
        assert any("page_not_found" in r for r in result.blocking_reasons)


# --- operation digest ---------------------------------------------------------


class TestOperationDigest:
    def test_digest_is_deterministic(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        r1 = single.build_single_update_preflight(client, "card-ds-1", _entry())
        r2 = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert r1.operation_digest == r2.operation_digest

    def test_digest_changes_when_relation_changes(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())

        card = _card_page(
            FAKE_PAGE_ID, "Elusive Otter", "Elusive Otter", [FAKE_DECK_ID, FAKE_OTHER_DECK_ID]
        )
        underlying.pages[FAKE_PAGE_ID] = card
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        assert before.operation_digest != after.operation_digest

    def test_digest_changes_when_last_edited_time_changes(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())

        card = _card_page(
            FAKE_PAGE_ID,
            "Elusive Otter",
            "Elusive Otter",
            [FAKE_DECK_ID],
            last_edited_time="2026-07-15T00:00:00.000Z",
        )
        underlying.pages[FAKE_PAGE_ID] = card
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        assert before.operation_digest != after.operation_digest

    def test_digest_changes_when_input_entry_changes(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        r1 = single.build_single_update_preflight(client, "card-ds-1", _entry())
        r2 = single.build_single_update_preflight(
            client, "card-ds-1", _entry(confirmed_new_title="別の新タイトル")
        )
        assert r1.operation_digest != r2.operation_digest


# --- 楽観的ロック --------------------------------------------------------------


class TestOptimisticLock:
    def test_no_changes_returns_empty(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        baseline = single.build_single_update_preflight(client, "card-ds-1", _entry())
        fresh = single.build_single_update_preflight(client, "card-ds-1", _entry())
        assert single.verify_optimistic_lock(baseline, fresh) == []

    def test_title_changed_between_preflight_and_apply_blocks(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        baseline = single.build_single_update_preflight(client, "card-ds-1", _entry())

        underlying.pages[FAKE_PAGE_ID] = _card_page(
            FAKE_PAGE_ID,
            "Someone Changed This",
            "Elusive Otter",
            [FAKE_DECK_ID],
            last_edited_time="2026-07-15T00:00:00.000Z",
        )
        fresh = single.build_single_update_preflight(client, "card-ds-1", _entry())

        changes = single.verify_optimistic_lock(baseline, fresh)
        assert "current_title_changed" in changes
        assert "operation_digest_changed" in changes


# --- 単一操作HTTP write guard --------------------------------------------------


class _FakeHttpxResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _guarded_client() -> tuple[NotionClient, list[tuple[str, str]]]:
    client = NotionClient(api_key="secret_test")
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, **kwargs: object) -> _FakeHttpxResponse:
        calls.append((method, path))
        return _FakeHttpxResponse({"id": FAKE_PAGE_ID})

    client._client.request = fake_request  # type: ignore[method-assign]  # noqa: SLF001
    single.install_single_title_update_write_guard(
        client,
        approved_page_id=FAKE_PAGE_ID,
        title_property_name="カード名",
        approved_new_title="神出鬼没のカワウソ",
    )
    return client, calls


class TestSingleWriteGuard:
    def test_approved_title_only_update_is_allowed(self) -> None:
        client, calls = _guarded_client()
        writer = single.SingleTitleUpdateWriter(client)

        writer.update_title(FAKE_PAGE_ID, "カード名", "神出鬼没のカワウソ")

        assert calls == [("PATCH", f"/pages/{FAKE_PAGE_ID}")]

    def test_rejects_non_title_property(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.update_page(FAKE_PAGE_ID, {"英語名": {"rich_text": []}})
        assert calls == []

    def test_rejects_relation_property(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.update_page(FAKE_PAGE_ID, {"採用デッキ": {"relation": []}})
        assert calls == []

    def test_rejects_multiple_properties(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.update_page(
                FAKE_PAGE_ID,
                {
                    "カード名": {"title": [{"text": {"content": "神出鬼没のカワウソ"}}]},
                    "英語名": {"rich_text": []},
                },
            )
        assert calls == []

    def test_rejects_unapproved_page_id(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.update_page(
                "some-other-page-id",
                {"カード名": {"title": [{"text": {"content": "神出鬼没のカワウソ"}}]}},
            )
        assert calls == []

    def test_rejects_wrong_new_title(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.update_page(
                FAKE_PAGE_ID, {"カード名": {"title": [{"text": {"content": "違うタイトル"}}]}}
            )
        assert calls == []

    def test_rejects_second_write(self) -> None:
        client, calls = _guarded_client()
        writer = single.SingleTitleUpdateWriter(client)
        writer.update_title(FAKE_PAGE_ID, "カード名", "神出鬼没のカワウソ")

        with pytest.raises(single.SingleUpdateGuardError):
            writer.update_title(FAKE_PAGE_ID, "カード名", "神出鬼没のカワウソ")

        assert calls == [("PATCH", f"/pages/{FAKE_PAGE_ID}")]

    def test_rejects_page_creation(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.create_page("card-ds-1", {"カード名": {"title": []}})
        assert calls == []

    def test_rejects_data_source_schema_update(self) -> None:
        client, calls = _guarded_client()
        with pytest.raises(single.SingleUpdateGuardError):
            client.update_data_source_schema("card-ds-1", {"所持枚数": {"number": {}}})
        assert calls == []

    def test_get_and_query_still_allowed(self) -> None:
        client, calls = _guarded_client()
        client.get_page(FAKE_PAGE_ID)
        assert calls == [("GET", f"/pages/{FAKE_PAGE_ID}")]


# --- 最小権限writer ------------------------------------------------------------


class TestMinimalPrivilegeWriter:
    def test_writer_only_accepts_page_id_property_name_and_title(self) -> None:
        import inspect

        sig = inspect.signature(single.SingleTitleUpdateWriter.update_title)
        params = list(sig.parameters.keys())
        assert params == ["self", "page_id", "title_property_name", "new_title"]


# --- 事後検証 -----------------------------------------------------------------


class TestPostVerification:
    def test_all_checks_pass_after_successful_update(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())
        snapshot = single.PreApplySnapshot.from_preflight(before, now="2026-07-14T00:00:00")

        underlying.pages[FAKE_PAGE_ID] = _card_page(
            FAKE_PAGE_ID, "神出鬼没のカワウソ", "Elusive Otter", [FAKE_DECK_ID]
        )
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        result = single.verify_post_update(snapshot, after, _entry(), write_count=1)
        assert result.title_updated_to_expected is True
        assert result.english_name_unchanged is True
        assert result.relation_ids_unchanged is True
        assert result.all_passed is True

    def test_title_not_changed_fails_check(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())
        snapshot = single.PreApplySnapshot.from_preflight(before, now="2026-07-14T00:00:00")

        # ページが更新されなかった状態を模す(タイトル不変)。
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        result = single.verify_post_update(snapshot, after, _entry(), write_count=1)
        assert result.title_updated_to_expected is False
        assert result.all_passed is False

    def test_english_name_changed_fails_check(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())
        snapshot = single.PreApplySnapshot.from_preflight(before, now="2026-07-14T00:00:00")

        underlying.pages[FAKE_PAGE_ID] = _card_page(
            FAKE_PAGE_ID, "神出鬼没のカワウソ", "Something Else", [FAKE_DECK_ID]
        )
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        result = single.verify_post_update(snapshot, after, _entry(), write_count=1)
        assert result.english_name_unchanged is False
        assert result.all_passed is False

    def test_relation_changed_fails_check(self) -> None:
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())
        snapshot = single.PreApplySnapshot.from_preflight(before, now="2026-07-14T00:00:00")

        underlying.pages[FAKE_PAGE_ID] = _card_page(
            FAKE_PAGE_ID,
            "神出鬼没のカワウソ",
            "Elusive Otter",
            [FAKE_DECK_ID, FAKE_OTHER_DECK_ID],
        )
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        result = single.verify_post_update(snapshot, after, _entry(), write_count=1)
        assert result.relation_ids_unchanged is False
        assert result.all_passed is False

    def test_failed_post_verification_does_not_trigger_rollback(self) -> None:
        """事後検証が失敗しても、rollback関数やそれに類する書き込みは
        一切呼ばれない(このモジュールにrollback機能自体が存在しない)ことを確認する。"""
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        before = single.build_single_update_preflight(client, "card-ds-1", _entry())
        snapshot = single.PreApplySnapshot.from_preflight(before, now="2026-07-14T00:00:00")
        after = single.build_single_update_preflight(client, "card-ds-1", _entry())

        result = single.verify_post_update(snapshot, after, _entry(), write_count=1)

        assert result.all_passed is False
        # 事後検証関数自体が読み取り専用クライアントしか呼んでおらず、
        # update_page/create_page は一度も呼ばれていない(フェイクが例外を送出する設計のため、
        # ここまで到達していること自体が書き込みゼロの証明になる)。
        assert underlying.update_page_calls == []
        assert underlying.create_page_calls == []
        assert not hasattr(single, "rollback")
        assert not hasattr(single, "rollback_update")


# --- レポート出力 -----------------------------------------------------------


class TestReportOutput:
    def test_json_and_markdown_reports_are_written(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        underlying = _happy_underlying()
        client = planner.ReadOnlyNotionClient(underlying)  # type: ignore[arg-type]
        preflight = single.build_single_update_preflight(client, "card-ds-1", _entry())
        data = single.preflight_to_json_dict(preflight, write_operations=0)

        json_path = single.write_json_report(data, tmp_path / "preflight.json")
        md_path = single.write_markdown_report(data, tmp_path / "preflight.md")

        assert json_path.exists()
        assert md_path.exists()
        assert data["notion_write_operations"] == 0
        assert data["notion_write_attempts"] == 0
        assert data["approval_required"] is True
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        assert loaded["operation_digest"] == preflight.operation_digest


# --- 公開リポジトリでのID取り扱い ----------------------------------------------


class TestNoProductionIdsInRepo:
    def test_gitignore_covers_local_config_directory(self) -> None:
        gitignore = open(".gitignore", encoding="utf-8").read()
        assert "config/local/" in gitignore

    def test_example_and_source_files_use_only_fake_ids(self) -> None:
        import glob
        import json
        import re

        # 本番ID自体はこのテストファイル(コミット対象)に literal で持たない。
        # gitignore対象のローカル実マニフェストが存在する場合のみ、そこから
        # 実IDを動的に読み取って接頭辞を導出し、コミット対象ファイルを検査する。
        local_manifest_paths = glob.glob("config/local/confirmed_card_title_updates/*.json")
        real_prefixes: set[str] = set()
        uuid_pattern = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        )
        for manifest_path in local_manifest_paths:
            data = json.loads(open(manifest_path, encoding="utf-8").read())
            for entry in data.get("entries", []):
                for value in entry.values():
                    values = value if isinstance(value, list) else [value]
                    for v in values:
                        if isinstance(v, str) and uuid_pattern.fullmatch(v):
                            real_prefixes.add(v.split("-")[0])
        if not real_prefixes:
            pytest.skip("ローカル実マニフェストが存在しないため、この検査をスキップします")

        targets = (
            glob.glob("config/confirmed_card_title_updates/*.example.json")
            + glob.glob("src/mtg_notion_manager/**/*.py", recursive=True)
            + glob.glob("tests/test_title_update_dry_run.py")
            + glob.glob("tests/test_cli_plan_title_updates.py")
        )
        for path in targets:
            content = open(path, encoding="utf-8").read()
            for prefix in real_prefixes:
                assert prefix not in content, f"{path} に本番ID接頭辞が含まれています"

    def test_real_manifest_is_not_tracked_by_git(self) -> None:
        import subprocess

        result = subprocess.run(
            ["git", "ls-files", "config/local/"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == ""
