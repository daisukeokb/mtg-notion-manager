"""記事側デッキ名(deck-title)から既存Notion統率者ページを明示的に解決するための機能。

Wizards公式記事のdeck-title属性は記事ごとに言語が異なりうる
(例: Strixhavenは日本語deck-title、Lorwyn Eclipsedは英語deck-title)。
そのため、記事側デッキ名とNotion統率者DBの「名前」プロパティが完全一致しない
記事では、import-article/verify-importの既存の名前完全一致照合だけでは
既存デッキページを解決できない。

このモジュールは、そうした場合に「記事側デッキ名 → 既存Notionページ」の対応を
人間が確認した内容として明示的に設定できるようにする。

設計方針:
- ページIDを主識別子とし、ページ名は「指定ページが意図したページであること」の
  検証にのみ使う(名前だけで代替判定はしない)。
- 設定は1記事に対して1ファイル(article_urlで対象記事を検証する)。
  同じ記事側デッキ名が別記事に登場しても誤解決しないよう、記事単位で完結させる。
- fuzzy match・部分一致・類似検索・自動翻訳は一切行わない。
- import-article/verify-importはこのモジュールの resolve_deck_page() を
  共通のresolverとして使用する(解決ロジックの二重化を避ける)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mtg_notion_manager.exceptions import DeckPageMappingConfigError, NotionAPIError
from mtg_notion_manager.models import ExistingDeck
from mtg_notion_manager.notion.writer import TITLE_PROPERTY, NotionWriter

SUPPORTED_SCHEMA_VERSIONS = (1,)

RESOLUTION_EXACT_NAME_MATCH = "exact_name_match"
RESOLUTION_EXPLICIT_MAPPING = "explicit_page_mapping"

_REQUIRED_DECK_KEYS = ("article_deck_name", "page_id", "expected_page_name")
_PAGE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def normalize_article_url(url: str) -> str:
    """記事URLを比較用に正規化する。

    scheme/hostの大文字小文字を統一し、末尾スラッシュ・クエリ文字列・
    フラグメントを除去する。HTTPリダイレクトの解決は行わない
    (設定ファイルのarticle_urlは、実行時に指定するURLと文字列として
    一致する必要がある)。
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def normalize_page_id(raw: str) -> str:
    """ダッシュ有無を問わず、Notion標準のダッシュ付き表記(8-4-4-4-12)へ正規化する。"""
    compact = raw.replace("-", "")
    if not _PAGE_ID_RE.match(compact):
        raise DeckPageMappingConfigError(f"page_idの形式が不正です: {raw!r}")
    return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"


@dataclass(frozen=True)
class DeckPageMappingEntry:
    article_deck_name: str
    page_id: str
    expected_page_name: str


@dataclass(frozen=True)
class DeckPageMapping:
    article_url: str
    entries: dict[str, DeckPageMappingEntry]

    def resolve(self, article_deck_name: str) -> DeckPageMappingEntry | None:
        return self.entries.get(article_deck_name)


def load_deck_page_mapping(
    path: Path, article_url: str, all_deck_names: list[str]
) -> DeckPageMapping:
    """デッキページマッピング設定を読み込み、実行対象記事に対して検証する。

    all_deck_names には記事から抽出した全デッキ名(--include-deckでの選択有無を
    問わない全件)を渡すこと。設定内のarticle_deck_nameがこの一覧に存在しない
    場合はエラーにする(記事に存在するが今回選択されていないデッキのマッピングは
    エラーにしない)。
    """
    if not path.exists():
        raise DeckPageMappingConfigError(f"{path} が存在しません。")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeckPageMappingConfigError(f"{path} が有効なJSONではありません: {exc}") from exc

    if not isinstance(data, dict):
        raise DeckPageMappingConfigError(f"{path} の内容がオブジェクトではありません。")

    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DeckPageMappingConfigError(
            f"{path} のschema_version '{schema_version}' には対応していません"
            f"(対応バージョン: {SUPPORTED_SCHEMA_VERSIONS})。"
        )

    config_article_url = data.get("article_url")
    if not isinstance(config_article_url, str) or not config_article_url:
        raise DeckPageMappingConfigError(f"{path} にarticle_urlがありません。")
    if normalize_article_url(config_article_url) != normalize_article_url(article_url):
        raise DeckPageMappingConfigError(
            f"{path} のarticle_url '{config_article_url}' が実行対象記事"
            f" '{article_url}' と一致しません。"
        )

    raw_decks = data.get("decks")
    if not isinstance(raw_decks, list):
        raise DeckPageMappingConfigError(f"{path} の 'decks' が配列ではありません。")

    entries = [_parse_deck_entry(raw, path, i) for i, raw in enumerate(raw_decks)]
    _validate_no_duplicate_deck_names(entries, path)
    _validate_no_duplicate_page_ids(entries, path)
    _validate_deck_names_exist_in_article(entries, all_deck_names, path)

    return DeckPageMapping(
        article_url=config_article_url,
        entries={entry.article_deck_name: entry for entry in entries},
    )


def _parse_deck_entry(raw: object, path: Path, index: int) -> DeckPageMappingEntry:
    if not isinstance(raw, dict):
        raise DeckPageMappingConfigError(f"{path} の decks[{index}] がオブジェクトではありません。")

    missing = [key for key in _REQUIRED_DECK_KEYS if key not in raw]
    if missing:
        raise DeckPageMappingConfigError(
            f"{path} の decks[{index}] に必須キーがありません: {missing}"
        )

    article_deck_name = raw["article_deck_name"]
    page_id_raw = raw["page_id"]
    expected_page_name = raw["expected_page_name"]

    if not isinstance(article_deck_name, str) or not article_deck_name:
        raise DeckPageMappingConfigError(
            f"{path} の decks[{index}].article_deck_name が空、または文字列ではありません。"
        )
    if not isinstance(page_id_raw, str) or not page_id_raw:
        raise DeckPageMappingConfigError(
            f"{path} の decks[{index}].page_id が空、または文字列ではありません。"
        )
    if not isinstance(expected_page_name, str) or not expected_page_name:
        raise DeckPageMappingConfigError(
            f"{path} の decks[{index}].expected_page_name が空、または文字列ではありません。"
        )

    try:
        page_id = normalize_page_id(page_id_raw)
    except DeckPageMappingConfigError as exc:
        raise DeckPageMappingConfigError(
            f"{path} の decks[{index}].page_id が不正です: {exc}"
        ) from exc

    return DeckPageMappingEntry(
        article_deck_name=article_deck_name,
        page_id=page_id,
        expected_page_name=expected_page_name,
    )


def _validate_no_duplicate_deck_names(
    entries: list[DeckPageMappingEntry], path: Path
) -> None:
    seen: set[str] = set()
    for entry in entries:
        if entry.article_deck_name in seen:
            raise DeckPageMappingConfigError(
                f"{path}: article_deck_name '{entry.article_deck_name}' が重複しています。"
            )
        seen.add(entry.article_deck_name)


def _validate_no_duplicate_page_ids(entries: list[DeckPageMappingEntry], path: Path) -> None:
    seen: dict[str, str] = {}
    for entry in entries:
        if entry.page_id in seen:
            raise DeckPageMappingConfigError(
                f"{path}: page_id '{entry.page_id}' が"
                f" '{seen[entry.page_id]}' と '{entry.article_deck_name}' の"
                " 両方に割り当てられています(矛盾)。"
            )
        seen[entry.page_id] = entry.article_deck_name


def _validate_deck_names_exist_in_article(
    entries: list[DeckPageMappingEntry], all_deck_names: list[str], path: Path
) -> None:
    for entry in entries:
        if entry.article_deck_name not in all_deck_names:
            raise DeckPageMappingConfigError(
                f"{path}: article_deck_name '{entry.article_deck_name}' が"
                " 記事内のデッキ名一覧に存在しません"
                f"(記事内デッキ名: {all_deck_names})。"
            )


# --- 共通resolver -----------------------------------------------------------


@dataclass(frozen=True)
class DeckResolution:
    """1デッキ分の解決結果(import-article/verify-import共通)。"""

    deck_name: str
    resolved: bool
    resolution_method: str | None
    existing_deck: ExistingDeck | None
    error: str | None = None


def resolve_deck_page(
    deck_name: str, writer: NotionWriter, mapping: DeckPageMapping | None
) -> DeckResolution:
    """記事側デッキ名から既存Notion統率者ページを解決する(import/verify共通)。

    解決順序:
    1. mappingに当該デッキ名の明示的エントリがあれば、そのpage_idを検証して使う
       (検証に失敗しても名前完全一致へはフォールバックしない)。
    2. mappingにエントリがなければ、従来どおり名前完全一致を試す。
    """
    if mapping is not None:
        entry = mapping.resolve(deck_name)
        if entry is not None:
            return _resolve_via_mapping(deck_name, entry, writer)

    return _resolve_via_exact_name(deck_name, writer)


def _resolve_via_mapping(
    deck_name: str, entry: DeckPageMappingEntry, writer: NotionWriter
) -> DeckResolution:
    try:
        page = writer.get_page(entry.page_id)
    except NotionAPIError as exc:
        return DeckResolution(
            deck_name=deck_name,
            resolved=False,
            resolution_method=RESOLUTION_EXPLICIT_MAPPING,
            existing_deck=None,
            error=(
                f"マッピング先ページを取得できません(article_deck_name: '{deck_name}',"
                f" page_id: '{entry.page_id}', 期待ページ名:"
                f" '{entry.expected_page_name}'): {exc}"
            ),
        )

    parent = page.get("parent", {})
    if parent.get("data_source_id") != writer.data_source_id:
        return DeckResolution(
            deck_name=deck_name,
            resolved=False,
            resolution_method=RESOLUTION_EXPLICIT_MAPPING,
            existing_deck=None,
            error=(
                f"マッピング先ページが統率者DBに所属していません(article_deck_name:"
                f" '{deck_name}', page_id: '{entry.page_id}')。"
            ),
        )

    actual_name = _plain_title(page.get("properties", {}))
    if actual_name != entry.expected_page_name:
        return DeckResolution(
            deck_name=deck_name,
            resolved=False,
            resolution_method=RESOLUTION_EXPLICIT_MAPPING,
            existing_deck=None,
            error=(
                f"マッピング先ページの名前が期待値と一致しません(article_deck_name:"
                f" '{deck_name}', page_id: '{entry.page_id}', 期待ページ名:"
                f" '{entry.expected_page_name}', 実際のページ名: '{actual_name}')。"
            ),
        )

    existing_deck = ExistingDeck(
        page_id=page["id"], page_url=page.get("url", ""), properties=page.get("properties", {})
    )
    return DeckResolution(
        deck_name=deck_name,
        resolved=True,
        resolution_method=RESOLUTION_EXPLICIT_MAPPING,
        existing_deck=existing_deck,
        error=None,
    )


def _resolve_via_exact_name(deck_name: str, writer: NotionWriter) -> DeckResolution:
    matches = writer.find_existing_decks(deck_name)
    if not matches:
        return DeckResolution(
            deck_name=deck_name,
            resolved=False,
            resolution_method=None,
            existing_deck=None,
            error=None,
        )
    if len(matches) > 1:
        page_ids = sorted(m.page_id for m in matches)
        return DeckResolution(
            deck_name=deck_name,
            resolved=False,
            resolution_method=None,
            existing_deck=None,
            error=(
                f"デッキレコードが{len(matches)}件あり一意に特定できません"
                f"(page_ids: {page_ids})。"
            ),
        )
    return DeckResolution(
        deck_name=deck_name,
        resolved=True,
        resolution_method=RESOLUTION_EXACT_NAME_MATCH,
        existing_deck=matches[0],
        error=None,
    )


def _plain_title(properties: dict) -> str | None:
    prop = properties.get(TITLE_PROPERTY, {})
    return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or None
