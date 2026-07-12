from __future__ import annotations

from pathlib import Path

from mtg_notion_manager.config import Config
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.services.doctor import run_doctor

NO_OVERRIDES_PATH = Path("does-not-exist/card_match_overrides.json")
NO_INTENTIONAL_DUPLICATES_PATH = Path("does-not-exist/intentional_duplicate_cards.json")

COMMANDER_DS_ID = "39aa97c8-7142-80a1-85c2-000b7f998d48"
CARD_DS_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


def _title(text: str) -> list[dict]:
    return [{"plain_text": text}]


def _valid_commander_schema() -> dict:
    return {
        "title": _title("MTG統率者DB"),
        "properties": {
            "名前": {"type": "title"},
            "統率者": {"type": "rich_text"},
            "発売セット": {
                "type": "select",
                "select": {
                    "options": [
                        {"name": "マーベル スーパー・ヒーローズ"},
                        {"name": "モダンホライゾン3"},
                        {"name": "ブルームバロウ"},
                        {"name": "ダスクモーン：戦慄の館"},
                        {"name": "指輪物語：中つ国の伝承"},
                        {"name": "Fallout"},
                        {"name": "サンダー・ジャンクションの無法者"},
                        {"name": "エルドレインの森"},
                        {"name": "ストリクスヘイヴン"},
                        {"name": "ストリクスヘイヴンの秘密"},
                        {"name": "ローウィンの昏明"},
                        {"name": "タルキール：龍嵐録"},
                        {"name": "イニストラード：真紅の契り"},
                    ]
                },
            },
            "所有状況": {"type": "select", "select": {"options": []}},
            "タイプ": {"type": "select", "select": {"options": []}},
            "改造状況": {"type": "select", "select": {"options": []}},
            "色": {
                "type": "multi_select",
                "multi_select": {
                    "options": [
                        {"name": "白"},
                        {"name": "青"},
                        {"name": "黒"},
                        {"name": "赤"},
                        {"name": "緑"},
                        {"name": "無色"},
                    ]
                },
            },
            "デッキリスト": {"type": "url"},
        },
    }


def _valid_card_schema(include_optional: bool = False) -> dict:
    properties = {
        "カード名": {"type": "title"},
        "英語名": {"type": "rich_text"},
        "所持": {"type": "checkbox"},
        "採用デッキ": {"type": "relation"},
    }
    if include_optional:
        properties["所持枚数"] = {"type": "number"}
        properties["統合済み"] = {"type": "checkbox"}
    return {"title": _title("MTGカードDB"), "properties": properties}


class FakeClient:
    def __init__(
        self,
        user: dict | None = None,
        commander_schema: dict | None = None,
        card_schema: dict | None = None,
        raise_on: set[str] | None = None,
        card_pages: list[dict] | None = None,
    ) -> None:
        self._user = user or {"name": "test-bot"}
        self._commander_schema = commander_schema
        self._card_schema = card_schema
        self._raise_on = raise_on or set()
        self._card_pages = card_pages or []

    def get_current_user(self) -> dict:
        if "user" in self._raise_on:
            raise NotionAPIError("認証に失敗しました (401)")
        return self._user

    def get_data_source(self, data_source_id: str) -> dict:
        if data_source_id == COMMANDER_DS_ID:
            if "commander" in self._raise_on:
                raise NotionAPIError("統率者DBが見つかりません (404)")
            return self._commander_schema
        if data_source_id == CARD_DS_ID:
            if "card" in self._raise_on:
                raise NotionAPIError("カードDBが見つかりません (404)")
            return self._card_schema
        raise AssertionError(f"unexpected data_source_id: {data_source_id}")

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        return self._card_pages

    def get_page(self, page_id: str) -> dict:
        for page in self._card_pages:
            if page["id"] == page_id:
                return page
        raise NotionAPIError(f"ページが見つかりません: {page_id}")


def _config(card_id: str | None = CARD_DS_ID) -> Config:
    return Config(
        notion_api_key="secret_test",
        commander_data_source_id=COMMANDER_DS_ID,
        card_data_source_id=card_id,
    )


class TestRunDoctor:
    def test_all_checks_pass(self) -> None:
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
        )
        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        assert all(r.ok for r in results), [r for r in results if not r.ok]
        names = {r.name for r in results}
        assert "Notion認証" in names
        assert "MTG統率者DB接続" in names
        assert "MTGカードDB接続" in names
        assert "カードDBプロパティ「カード名」" in names

    def test_all_checks_pass_with_optional_properties_present(self) -> None:
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(include_optional=True),
        )
        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        assert all(r.ok for r in results), [r for r in results if not r.ok]

    def test_auth_failure_stops_further_checks(self) -> None:
        client = FakeClient(raise_on={"user"})
        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        assert len(results) == 1
        assert results[0].name == "Notion認証"
        assert results[0].ok is False

    def test_missing_property_is_reported(self) -> None:
        schema = _valid_commander_schema()
        del schema["properties"]["統率者"]
        client = FakeClient(commander_schema=schema, card_schema=_valid_card_schema())

        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert "プロパティ「統率者」" in failed

    def test_missing_set_option_is_reported(self) -> None:
        schema = _valid_commander_schema()
        schema["properties"]["発売セット"]["select"]["options"] = [{"name": "ブルームバロウ"}]
        client = FakeClient(commander_schema=schema, card_schema=_valid_card_schema())

        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert "「発売セット」選択肢の整合性" in failed

    def test_card_data_source_required_when_not_configured(self) -> None:
        client = FakeClient(commander_schema=_valid_commander_schema())
        results = run_doctor(
            _config(card_id=None),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        card_result = next(r for r in results if r.name == "MTGカードDB接続")
        assert card_result.ok is False
        assert "未設定" in card_result.message

    def test_commander_connection_failure_is_reported(self) -> None:
        client = FakeClient(raise_on={"commander"}, card_schema=_valid_card_schema())
        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        commander_result = next(r for r in results if r.name == "MTG統率者DB接続")
        assert commander_result.ok is False

    def test_missing_card_property_is_reported(self) -> None:
        schema = _valid_card_schema()
        del schema["properties"]["採用デッキ"]
        client = FakeClient(commander_schema=_valid_commander_schema(), card_schema=schema)

        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert "カードDBプロパティ「採用デッキ」" in failed

    def test_optional_card_properties_missing_is_informational_not_failure(self) -> None:
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(include_optional=False),
        )
        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        quantity_result = next(
            r for r in results if r.name == "カードDBプロパティ「所持枚数」(任意)"
        )
        assert quantity_result.ok is True
        assert "存在しません" in quantity_result.message

    def test_card_connection_failure_is_reported(self) -> None:
        client = FakeClient(commander_schema=_valid_commander_schema(), raise_on={"card"})
        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        card_result = next(r for r in results if r.name == "MTGカードDB接続")
        assert card_result.ok is False


def _card_page(
    page_id: str,
    name_ja: str,
    name_en: str | None = None,
    merged: bool = False,
) -> dict:
    properties: dict = {
        "カード名": {"type": "title", "title": [{"plain_text": name_ja}]},
        "統合済み": {"type": "checkbox", "checkbox": merged},
    }
    if name_en is not None:
        properties["英語名"] = {"type": "rich_text", "rich_text": [{"plain_text": name_en}]}
    return {"id": page_id, "url": f"https://notion.so/{page_id}", "properties": properties}


class TestCardMatchOverridesCheck:
    def test_valid_override_passes(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "card_match_overrides.json"
        overrides_path.write_text(
            '{"by_japanese_name": {"苦渋の破棄": '
            '{"canonical_page_id": "p1", "reason": "テスト"}}}',
            encoding="utf-8",
        )
        card_pages = [
            _card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking"),
            _card_page("p2", "苦渋の破棄", name_en="Anguished Unmaking"),
        ]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=overrides_path,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = [r for r in results if not r.ok]
        assert failed == [], failed

    def test_invalid_json_fails(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "card_match_overrides.json"
        overrides_path.write_text("{not valid json", encoding="utf-8")
        client = FakeClient(
            commander_schema=_valid_commander_schema(), card_schema=_valid_card_schema()
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=overrides_path,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert "card_match_overrides.json" in failed

    def test_page_id_not_found_fails(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "card_match_overrides.json"
        overrides_path.write_text(
            '{"by_japanese_name": {"苦渋の破棄": '
            '{"canonical_page_id": "does-not-exist", "reason": "テスト"}}}',
            encoding="utf-8",
        )
        card_pages = [_card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking")]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=overrides_path,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("苦渋の破棄" in name for name in failed)

    def test_merged_page_id_fails(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "card_match_overrides.json"
        overrides_path.write_text(
            '{"by_japanese_name": {"苦渋の破棄": '
            '{"canonical_page_id": "p1", "reason": "テスト"}}}',
            encoding="utf-8",
        )
        card_pages = [
            _card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking", merged=True),
        ]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=overrides_path,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("統合済み" in r.message for r in failed.values())

    def test_name_mismatch_fails(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "card_match_overrides.json"
        overrides_path.write_text(
            '{"by_japanese_name": {"存在しないカード名": '
            '{"canonical_page_id": "p1", "reason": "テスト"}}}',
            encoding="utf-8",
        )
        card_pages = [_card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking")]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=overrides_path,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("一致しません" in r.message for r in failed.values())

    def test_not_in_candidate_set_fails(self, tmp_path: Path) -> None:
        # カード名は一致するが、重複していない(単独レコードで曖昧一致候補になりえない)場合。
        overrides_path = tmp_path / "card_match_overrides.json"
        overrides_path.write_text(
            '{"by_japanese_name": {"苦渋の破棄": '
            '{"canonical_page_id": "p1", "reason": "テスト"}}}',
            encoding="utf-8",
        )
        # p1は単独なので候補集合は自分自身のみ("苦渋の破棄"で正規化された1件)。
        # ここではp1のカード名をあえて別名にして「候補内に含まれない」ケースを再現する。
        card_pages = [_card_page("p1", "違うカード名", name_en="Anguished Unmaking")]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=overrides_path,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("一致しません" in r.message for r in failed.values())

    def test_no_overrides_file_is_informational_pass(self) -> None:
        client = FakeClient(
            commander_schema=_valid_commander_schema(), card_schema=_valid_card_schema()
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        override_result = next(r for r in results if r.name == "card_match_overrides.json")
        assert override_result.ok is True
        assert "設定なし" in override_result.message


class TestIntentionalDuplicatesCheck:
    def test_valid_config_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional_duplicate_cards.json"
        path.write_text(
            '{"groups": [{"card_name_en": "Anguished Unmaking", "card_name_ja": "苦渋の破棄",'
            ' "page_ids": ["p1", "p2"],'
            ' "reason": "通常版とショーケース版を別レコードとして保持する", "enabled": true}]}',
            encoding="utf-8",
        )
        card_pages = [
            _card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking"),
            _card_page("p2", "苦渋の破棄", name_en="Anguished Unmaking"),
        ]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(), client, overrides_path=NO_OVERRIDES_PATH, intentional_duplicates_path=path
        )

        failed = [r for r in results if not r.ok]
        assert failed == [], failed

    def test_invalid_json_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional_duplicate_cards.json"
        path.write_text("{not valid json", encoding="utf-8")
        client = FakeClient(
            commander_schema=_valid_commander_schema(), card_schema=_valid_card_schema()
        )

        results = run_doctor(
            _config(), client, overrides_path=NO_OVERRIDES_PATH, intentional_duplicates_path=path
        )

        failed = {r.name: r for r in results if not r.ok}
        assert "intentional_duplicate_cards.json" in failed

    def test_missing_page_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional_duplicate_cards.json"
        path.write_text(
            '{"groups": [{"card_name_en": "Anguished Unmaking", "card_name_ja": "苦渋の破棄",'
            ' "page_ids": ["p1", "does-not-exist"],'
            ' "reason": "テスト", "enabled": true}]}',
            encoding="utf-8",
        )
        card_pages = [_card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking")]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(), client, overrides_path=NO_OVERRIDES_PATH, intentional_duplicates_path=path
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("苦渋の破棄" in name for name in failed)

    def test_merged_page_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional_duplicate_cards.json"
        path.write_text(
            '{"groups": [{"card_name_en": "Anguished Unmaking", "card_name_ja": "苦渋の破棄",'
            ' "page_ids": ["p1", "p2"],'
            ' "reason": "テスト", "enabled": true}]}',
            encoding="utf-8",
        )
        card_pages = [
            _card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking"),
            _card_page("p2", "苦渋の破棄", name_en="Anguished Unmaking", merged=True),
        ]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(), client, overrides_path=NO_OVERRIDES_PATH, intentional_duplicates_path=path
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("統合済み" in r.message for r in failed.values())

    def test_name_mismatch_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional_duplicate_cards.json"
        path.write_text(
            '{"groups": [{"card_name_en": "Wrong Name", "card_name_ja": "存在しないカード名",'
            ' "page_ids": ["p1", "p2"],'
            ' "reason": "テスト", "enabled": true}]}',
            encoding="utf-8",
        )
        card_pages = [
            _card_page("p1", "苦渋の破棄", name_en="Anguished Unmaking"),
            _card_page("p2", "苦渋の破棄", name_en="Anguished Unmaking"),
        ]
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=card_pages,
        )

        results = run_doctor(
            _config(), client, overrides_path=NO_OVERRIDES_PATH, intentional_duplicates_path=path
        )

        failed = {r.name: r for r in results if not r.ok}
        assert any("一致しません" in r.message for r in failed.values())

    def test_disabled_group_skips_live_validation(self, tmp_path: Path) -> None:
        path = tmp_path / "intentional_duplicate_cards.json"
        path.write_text(
            '{"groups": [{"card_name_en": "Anguished Unmaking", "card_name_ja": "苦渋の破棄",'
            ' "page_ids": ["does-not-exist-1", "does-not-exist-2"],'
            ' "reason": "テスト", "enabled": false}]}',
            encoding="utf-8",
        )
        client = FakeClient(
            commander_schema=_valid_commander_schema(),
            card_schema=_valid_card_schema(),
            card_pages=[],
        )

        results = run_doctor(
            _config(), client, overrides_path=NO_OVERRIDES_PATH, intentional_duplicates_path=path
        )

        failed = [r for r in results if not r.ok]
        assert failed == [], failed

    def test_no_config_file_is_informational_pass(self) -> None:
        client = FakeClient(
            commander_schema=_valid_commander_schema(), card_schema=_valid_card_schema()
        )

        results = run_doctor(
            _config(),
            client,
            overrides_path=NO_OVERRIDES_PATH,
            intentional_duplicates_path=NO_INTENTIONAL_DUPLICATES_PATH,
        )

        result = next(r for r in results if r.name == "intentional_duplicate_cards.json")
        assert result.ok is True
        assert "設定なし" in result.message
