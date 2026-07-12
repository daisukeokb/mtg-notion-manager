from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.card_match_overrides import (
    DEFAULT_OVERRIDES_PATH,
    load_card_match_overrides,
)
from mtg_notion_manager.config import Config
from mtg_notion_manager.exceptions import (
    CardMatchOverrideError,
    IntentionalDuplicateConfigError,
    NotionAPIError,
)
from mtg_notion_manager.intentional_duplicates import (
    DEFAULT_INTENTIONAL_DUPLICATES_PATH,
    IntentionalDuplicateGroup,
    load_intentional_duplicates,
)
from mtg_notion_manager.mapping import VALID_COLORS, VALID_SET_NAMES
from mtg_notion_manager.notion.card_repository import (
    ENGLISH_NAME_PROPERTY,
    MERGED_PROPERTY,
    TITLE_PROPERTY,
    CardRepository,
)
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.parsers.card_names import normalize_card_name

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


def run_doctor(
    config: Config,
    client: NotionClient,
    overrides_path: Path = DEFAULT_OVERRIDES_PATH,
    intentional_duplicates_path: Path = DEFAULT_INTENTIONAL_DUPLICATES_PATH,
) -> list[CheckResult]:
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

    results.extend(_check_card_match_overrides(client, config, overrides_path))
    results.extend(_check_intentional_duplicates(client, config, intentional_duplicates_path))

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


def _check_card_match_overrides(
    client: NotionClient, config: Config, overrides_path: Path
) -> list[CheckResult]:
    """card_match_overrides.jsonの妥当性・整合性を検証する(書き込みは行わない)。"""
    results: list[CheckResult] = []

    try:
        overrides = load_card_match_overrides(overrides_path)
    except CardMatchOverrideError as exc:
        results.append(CheckResult("card_match_overrides.json", False, str(exc)))
        return results

    entries: list[tuple[str, str, str]] = [
        (name, entry.canonical_page_id, "日本語名")
        for name, entry in overrides.by_japanese_name.items()
    ] + [
        (name, entry.canonical_page_id, "英語名")
        for name, entry in overrides.by_english_name.items()
    ]

    if not entries:
        results.append(CheckResult("card_match_overrides.json", True, "設定なし(検証対象なし)"))
        return results

    results.append(
        CheckResult("card_match_overrides.json", True, f"有効なJSON({len(entries)}件の設定)")
    )

    if not config.card_data_source_id:
        results.append(
            CheckResult(
                "card_match_overridesの整合性",
                False,
                "NOTION_CARD_DATA_SOURCE_ID が未設定のため検証できません",
            )
        )
        return results

    try:
        card_repo = CardRepository(client, config.card_data_source_id, overrides=overrides)
        card_repo.load()
    except NotionAPIError as exc:
        results.append(CheckResult("card_match_overridesの整合性", False, str(exc)))
        return results

    for normalized_name, page_id, kind in entries:
        results.append(_check_one_override(client, card_repo, normalized_name, page_id, kind))

    return results


def _check_one_override(
    client: NotionClient,
    card_repo: CardRepository,
    normalized_name: str,
    page_id: str,
    kind: str,
) -> CheckResult:
    label = f"card_match_overrides「{normalized_name}」({kind})"

    try:
        page = client.get_page(page_id)
    except NotionAPIError as exc:
        return CheckResult(label, False, f"page_idがカードDBに存在しません: {exc}")

    properties = page.get("properties", {})
    if bool(properties.get(MERGED_PROPERTY, {}).get("checkbox")):
        return CheckResult(label, False, "指定page_idは統合済み(統合済み=true)のページです")

    name_ja = _plain_text(properties.get(TITLE_PROPERTY))
    name_en = _plain_text(properties.get(ENGLISH_NAME_PROPERTY))

    if kind == "日本語名":
        matches = name_ja is not None and normalize_card_name(name_ja) == normalized_name
        candidates = card_repo.candidates_by_japanese_name(name_ja or "")
    else:
        matches = name_en is not None and normalize_card_name(name_en) == normalized_name
        candidates = card_repo.candidates_by_english_name(name_en or "")

    if not matches:
        return CheckResult(
            label,
            False,
            f"指定page_idのカード名/英語名が設定キーと一致しません"
            f"(カード名: {name_ja}, 英語名: {name_en})",
        )

    if page_id not in [c.page_id for c in candidates]:
        return CheckResult(label, False, "指定page_idが実際の曖昧一致候補集合に含まれません")

    return CheckResult(label, True, f"OK ({page.get('url', page_id)})")


def _check_intentional_duplicates(
    client: NotionClient, config: Config, path: Path
) -> list[CheckResult]:
    """intentional_duplicate_cards.jsonの妥当性・整合性を検証する(書き込みは行わない)。"""
    results: list[CheckResult] = []

    try:
        intentional = load_intentional_duplicates(path)
    except IntentionalDuplicateConfigError as exc:
        results.append(CheckResult("intentional_duplicate_cards.json", False, str(exc)))
        return results

    if not intentional.groups:
        results.append(
            CheckResult("intentional_duplicate_cards.json", True, "設定なし(検証対象なし)")
        )
        return results

    results.append(
        CheckResult(
            "intentional_duplicate_cards.json",
            True,
            f"有効なJSON({len(intentional.groups)}件のグループ)",
        )
    )

    if not config.card_data_source_id:
        results.append(
            CheckResult(
                "intentional_duplicate_cardsの整合性",
                False,
                "NOTION_CARD_DATA_SOURCE_ID が未設定のため検証できません",
            )
        )
        return results

    for group in intentional.groups:
        results.append(_check_one_intentional_duplicate_group(client, group))

    return results


def _check_one_intentional_duplicate_group(
    client: NotionClient, group: IntentionalDuplicateGroup
) -> CheckResult:
    label = f"intentional_duplicate_cards「{group.card_name_ja}」"

    if not group.enabled:
        return CheckResult(label, True, "無効化されています(enabled: false、検証スキップ)")

    pages = []
    for page_id in sorted(group.page_ids):
        try:
            page = client.get_page(page_id)
        except NotionAPIError as exc:
            return CheckResult(label, False, f"page_id '{page_id}' がカードDBに存在しません: {exc}")
        pages.append(page)

    for page in pages:
        properties = page.get("properties", {})
        if bool(properties.get(MERGED_PROPERTY, {}).get("checkbox")):
            return CheckResult(
                label, False, f"page_id '{page['id']}' は統合済み(統合済み=true)のページです"
            )

        name_ja = _plain_text(properties.get(TITLE_PROPERTY))
        name_en = _plain_text(properties.get(ENGLISH_NAME_PROPERTY))
        expected_ja = normalize_card_name(group.card_name_ja)
        expected_en = normalize_card_name(group.card_name_en)
        matches = (name_ja is not None and normalize_card_name(name_ja) == expected_ja) or (
            name_en is not None and normalize_card_name(name_en) == expected_en
        )
        if not matches:
            return CheckResult(
                label,
                False,
                f"page_id '{page['id']}' のカード名/英語名が設定と一致しません"
                f"(カード名: {name_ja}, 英語名: {name_en})",
            )

    return CheckResult(label, True, f"OK ({len(pages)}ページ)")


def _plain_text(prop: dict | None) -> str | None:
    if prop is None:
        return None
    prop_type = prop.get("type")
    if prop_type == "title":
        text = "".join(t.get("plain_text", "") for t in prop.get("title", []))
    elif prop_type == "rich_text":
        text = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    else:
        return None
    return text or None
