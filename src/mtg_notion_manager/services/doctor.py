from __future__ import annotations

from dataclasses import dataclass

from mtg_notion_manager.config import Config
from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.mapping import VALID_COLORS, VALID_SET_NAMES
from mtg_notion_manager.notion.client import NotionClient

# Notion「MTG統率者DB」に存在すべき必須プロパティと型。
REQUIRED_COMMANDER_PROPERTIES: dict[str, str] = {
    "名前": "title",
    "統率者": "rich_text",
    "発売セット": "select",
    "所有状況": "select",
    "タイプ": "select",
    "改造状況": "select",
    "色": "multi_select",
    "デッキリスト": "url",
}

# Notion「MTGカードDB」でカード取り込み機能が使用する必須プロパティと型。
REQUIRED_CARD_PROPERTIES: dict[str, str] = {
    "カード名": "title",
    "英語名": "rich_text",
    "所持": "checkbox",
    "採用デッキ": "relation",
}

# dedupe-cards機能が使用する、あれば使う(スキーマ変更で追加しうる)プロパティ。
# 未存在の場合は import-cards 側では書き込まず内部集計にのみ使う。
# dedupe-cards --apply-schema で追加されると number/checkbox として存在するようになる。
OPTIONAL_CARD_PROPERTIES: dict[str, str] = {
    "所持枚数": "number",
    "統合済み": "checkbox",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str


def run_doctor(config: Config, client: NotionClient) -> list[CheckResult]:
    """Notion認証・DB接続・スキーマの健全性を検証する。"""
    results: list[CheckResult] = []

    try:
        user = client.get_current_user()
        bot_name = user.get("name") or user.get("bot", {}).get("owner", {}).get("type", "?")
        results.append(CheckResult("Notion認証", True, f"OK (integration: {bot_name})"))
    except NotionAPIError as exc:
        results.append(CheckResult("Notion認証", False, str(exc)))
        return results  # 認証に失敗した場合、以降のチェックは実施不可

    try:
        commander_ds = client.get_data_source(config.commander_data_source_id)
        results.append(CheckResult("MTG統率者DB接続", True, f"OK ({_extract_title(commander_ds)})"))
        results.extend(_check_commander_schema(commander_ds))
    except NotionAPIError as exc:
        results.append(CheckResult("MTG統率者DB接続", False, str(exc)))

    if config.card_data_source_id:
        try:
            card_ds = client.get_data_source(config.card_data_source_id)
            results.append(CheckResult("MTGカードDB接続", True, f"OK ({_extract_title(card_ds)})"))
            results.extend(_check_card_schema(card_ds))
        except NotionAPIError as exc:
            results.append(CheckResult("MTGカードDB接続", False, str(exc)))
    else:
        results.append(
            CheckResult(
                "MTGカードDB接続",
                False,
                "NOTION_CARD_DATA_SOURCE_ID が未設定です(import-cardsコマンドに必須)",
            )
        )

    return results


def _extract_title(data_source: dict) -> str:
    parts = data_source.get("title", [])
    text = "".join(part.get("plain_text", "") for part in parts)
    return text or "?"


def _check_commander_schema(data_source: dict) -> list[CheckResult]:
    results: list[CheckResult] = []
    schema: dict = data_source.get("properties", {})

    for prop_name, expected_type in REQUIRED_COMMANDER_PROPERTIES.items():
        prop = schema.get(prop_name)
        if prop is None:
            results.append(CheckResult(f"プロパティ「{prop_name}」", False, "存在しません"))
        elif prop.get("type") != expected_type:
            results.append(
                CheckResult(
                    f"プロパティ「{prop_name}」",
                    False,
                    f"型が想定と異なります(期待: {expected_type}, 実際: {prop.get('type')})",
                )
            )
        else:
            results.append(CheckResult(f"プロパティ「{prop_name}」", True, "OK"))

    results.append(_check_select_options(schema, "発売セット", "select", VALID_SET_NAMES))
    results.append(_check_select_options(schema, "色", "multi_select", VALID_COLORS))

    return results


def _check_select_options(
    schema: dict, prop_name: str, prop_type: str, expected: frozenset[str]
) -> CheckResult:
    prop = schema.get(prop_name)
    if prop is None or prop.get("type") != prop_type:
        return CheckResult(f"「{prop_name}」選択肢の整合性", False, "プロパティが見つかりません")

    live_options = {opt["name"] for opt in prop.get(prop_type, {}).get("options", [])}
    missing_in_notion = expected - live_options

    if missing_in_notion:
        return CheckResult(
            f"「{prop_name}」選択肢の整合性",
            False,
            f"mapping.pyにあるがNotionに存在しない選択肢: {sorted(missing_in_notion)}",
        )

    extra_in_notion = live_options - expected
    if extra_in_notion:
        return CheckResult(
            f"「{prop_name}」選択肢の整合性",
            True,
            f"OK (mapping.py未登録の新しい選択肢がNotion側にあります: {sorted(extra_in_notion)})",
        )

    return CheckResult(f"「{prop_name}」選択肢の整合性", True, "OK")


def _check_card_schema(data_source: dict) -> list[CheckResult]:
    results: list[CheckResult] = []
    schema: dict = data_source.get("properties", {})

    for prop_name, expected_type in REQUIRED_CARD_PROPERTIES.items():
        prop = schema.get(prop_name)
        if prop is None:
            results.append(CheckResult(f"カードDBプロパティ「{prop_name}」", False, "存在しません"))
        elif prop.get("type") != expected_type:
            results.append(
                CheckResult(
                    f"カードDBプロパティ「{prop_name}」",
                    False,
                    f"型が想定と異なります(期待: {expected_type}, 実際: {prop.get('type')})",
                )
            )
        else:
            results.append(CheckResult(f"カードDBプロパティ「{prop_name}」", True, "OK"))

    for prop_name, expected_type in OPTIONAL_CARD_PROPERTIES.items():
        prop = schema.get(prop_name)
        if prop is None:
            results.append(
                CheckResult(
                    f"カードDBプロパティ「{prop_name}」(任意)",
                    True,
                    "存在しません(import-cardsはこの項目をNotionへ書き込みません)",
                )
            )
        elif prop.get("type") != expected_type:
            results.append(
                CheckResult(
                    f"カードDBプロパティ「{prop_name}」(任意)",
                    False,
                    f"型が想定と異なります(期待: {expected_type}, 実際: {prop.get('type')})",
                )
            )
        else:
            results.append(CheckResult(f"カードDBプロパティ「{prop_name}」(任意)", True, "OK"))

    return results
