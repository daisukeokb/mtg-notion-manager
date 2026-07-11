from __future__ import annotations

from mtg_notion_manager.config import Config
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.services.doctor import run_doctor

COMMANDER_DS_ID = "39aa97c8-7142-80a1-85c2-000b7f998d48"
CARD_DS_ID = "81eec501-574b-4222-ad69-87a6f68fdf2b"


def _valid_commander_schema() -> dict:
    return {
        "name": "MTG統率者DB",
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


class FakeClient:
    def __init__(
        self,
        user: dict | None = None,
        commander_schema: dict | None = None,
        card_schema: dict | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self._user = user or {"name": "test-bot"}
        self._commander_schema = commander_schema
        self._card_schema = card_schema
        self._raise_on = raise_on or set()

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
            card_schema={"name": "MTGカードDB"},
        )
        results = run_doctor(_config(), client)

        assert all(r.ok for r in results), [r for r in results if not r.ok]
        names = {r.name for r in results}
        assert "Notion認証" in names
        assert "MTG統率者DB接続" in names
        assert "MTGカードDB接続" in names

    def test_auth_failure_stops_further_checks(self) -> None:
        client = FakeClient(raise_on={"user"})
        results = run_doctor(_config(), client)

        assert len(results) == 1
        assert results[0].name == "Notion認証"
        assert results[0].ok is False

    def test_missing_property_is_reported(self) -> None:
        schema = _valid_commander_schema()
        del schema["properties"]["統率者"]
        client = FakeClient(commander_schema=schema, card_schema={"name": "MTGカードDB"})

        results = run_doctor(_config(), client)

        failed = {r.name: r for r in results if not r.ok}
        assert "プロパティ「統率者」" in failed

    def test_missing_set_option_is_reported(self) -> None:
        schema = _valid_commander_schema()
        schema["properties"]["発売セット"]["select"]["options"] = [
            {"name": "ブルームバロウ"}
        ]
        client = FakeClient(commander_schema=schema, card_schema={"name": "MTGカードDB"})

        results = run_doctor(_config(), client)

        failed = {r.name: r for r in results if not r.ok}
        assert "「発売セット」選択肢の整合性" in failed

    def test_card_data_source_skipped_when_not_configured(self) -> None:
        client = FakeClient(commander_schema=_valid_commander_schema())
        results = run_doctor(_config(card_id=None), client)

        card_result = next(r for r in results if r.name == "MTGカードDB接続")
        assert card_result.ok is True
        assert "スキップ" in card_result.message

    def test_commander_connection_failure_is_reported(self) -> None:
        client = FakeClient(raise_on={"commander"}, card_schema={"name": "MTGカードDB"})
        results = run_doctor(_config(), client)

        commander_result = next(r for r in results if r.name == "MTG統率者DB接続")
        assert commander_result.ok is False
