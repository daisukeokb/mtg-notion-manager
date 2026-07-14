"""既存カードページの日本語タイトル更新を計画する、読み取り専用dry-runプランナー。

背景:
Strixhavenカード監査(feat/audit-strixhaven-card-duplicates、未マージ)で、
英語記事由来の新規カード作成時に日本語名が確認されないまま英語名がタイトルへ
書き込まれた疑いのあるページが7件見つかった。このモジュールは、それらについて
人間が確認済みの日本語名を用いた「タイトル変更計画」を読み取り専用で作成する。

実更新は一切行わない。update_page 等の書き込み系メソッドはこのモジュールから
一切importしない・参照しない(ReadOnlyNotionClientが書き込み系メソッド名を
公開せず、呼び出されれば例外にする設計、かつHTTP送信層でも二重にガードする)。

このモジュールは監査ブランチ(feat/audit-strixhaven-card-duplicates)の
コードに一切依存しない(そのブランチは未マージのため)。読み取り専用ガードの
実装方針は同種だが、独立して書き起こしている。
"""

from __future__ import annotations

import datetime
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.notion.client import NotionClient

TITLE_PROPERTY = "カード名"
ENGLISH_NAME_PROPERTY = "英語名"
DECKS_RELATION_PROPERTY = "採用デッキ"
COMMANDER_CARDS_RELATION_PROPERTY = "採用カード"

SUPPORTED_MANIFEST_SCHEMA_VERSIONS = (1,)
REQUIRED_VERIFICATION_STATUS = "human_confirmed"

_REQUIRED_ENTRY_KEYS = (
    "page_id",
    "expected_current_title",
    "confirmed_new_title",
    "expected_english_name",
    "source_deck_ids",
    "verification_status",
    "verification_actor",
    "verification_note",
)


class TitleUpdateManifestConfigError(MtgNotionManagerError):
    """人間確認済みタイトル更新マニフェストが不正(必須項目欠落・矛盾など)。"""


class ReadOnlyGuardError(MtgNotionManagerError):
    """dry-run処理から書き込み系Notion操作が呼び出された(安全機構違反)。"""


# --- マニフェスト -----------------------------------------------------------


@dataclass(frozen=True)
class ConfirmedTitleUpdateEntry:
    page_id: str
    expected_current_title: str
    confirmed_new_title: str
    expected_english_name: str
    source_deck_ids: list[str]
    verification_status: str
    verification_actor: str
    verification_note: str
    reference_url: str | None = None


@dataclass(frozen=True)
class ConfirmedTitleUpdateManifest:
    schema_version: int
    purpose: str
    source_audit_report: str | None
    entries: list[ConfirmedTitleUpdateEntry]


def load_confirmed_title_update_manifest(
    path: Path, expected_entry_count: int | None = None
) -> ConfirmedTitleUpdateManifest:
    """人間確認済みタイトル更新マニフェストを読み込み、検証する。

    任意件数のentryを扱える汎用設計。expected_entry_countを指定した場合、
    件数が一致しなければ即座に失敗する(呼び出し側が「今回は7件必須」のように
    実行単位で強制したい場合に使う)。

    不正な設定は未指定扱いへフォールバックせず、即座に
    TitleUpdateManifestConfigError を送出する。
    """
    if not path.exists():
        raise TitleUpdateManifestConfigError(f"{path} が存在しません。")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TitleUpdateManifestConfigError(f"{path} が有効なJSONではありません: {exc}") from exc

    if not isinstance(data, dict):
        raise TitleUpdateManifestConfigError(f"{path} の内容がオブジェクトではありません。")

    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        raise TitleUpdateManifestConfigError(
            f"{path} のschema_version '{schema_version}' には対応していません"
            f"(対応バージョン: {SUPPORTED_MANIFEST_SCHEMA_VERSIONS})。"
        )

    purpose = data.get("purpose")
    if not isinstance(purpose, str) or not purpose:
        raise TitleUpdateManifestConfigError(f"{path} にpurposeがありません。")

    source_audit_report = data.get("source_audit_report")
    if source_audit_report is not None and not isinstance(source_audit_report, str):
        raise TitleUpdateManifestConfigError(
            f"{path} のsource_audit_reportは文字列またはnullである必要があります。"
        )

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise TitleUpdateManifestConfigError(f"{path} の 'entries' が配列ではありません。")

    entries = [_parse_entry(raw, path, i) for i, raw in enumerate(raw_entries)]
    _validate_no_duplicate_page_ids(entries, path)

    if expected_entry_count is not None and len(entries) != expected_entry_count:
        raise TitleUpdateManifestConfigError(
            f"{path} のentries件数が期待値と一致しません"
            f"(期待: {expected_entry_count}, 実際: {len(entries)})。"
        )

    return ConfirmedTitleUpdateManifest(
        schema_version=schema_version,
        purpose=purpose,
        source_audit_report=source_audit_report,
        entries=entries,
    )


def _parse_entry(raw: object, path: Path, index: int) -> ConfirmedTitleUpdateEntry:
    if not isinstance(raw, dict):
        raise TitleUpdateManifestConfigError(
            f"{path} の entries[{index}] がオブジェクトではありません。"
        )

    missing = [key for key in _REQUIRED_ENTRY_KEYS if key not in raw]
    if missing:
        raise TitleUpdateManifestConfigError(
            f"{path} の entries[{index}] に必須キーがありません: {missing}"
        )

    page_id = raw["page_id"]
    expected_current_title = raw["expected_current_title"]
    confirmed_new_title = raw["confirmed_new_title"]
    expected_english_name = raw["expected_english_name"]
    source_deck_ids = raw["source_deck_ids"]
    verification_status = raw["verification_status"]
    verification_actor = raw["verification_actor"]
    verification_note = raw["verification_note"]
    reference_url = raw.get("reference_url")

    def _require_nonempty_str(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise TitleUpdateManifestConfigError(
                f"{path} の entries[{index}].{field_name} が空、または文字列ではありません。"
            )
        return value

    page_id = _require_nonempty_str(page_id, "page_id")
    expected_current_title = _require_nonempty_str(
        expected_current_title, "expected_current_title"
    )
    confirmed_new_title = _require_nonempty_str(confirmed_new_title, "confirmed_new_title")
    expected_english_name = _require_nonempty_str(
        expected_english_name, "expected_english_name"
    )
    verification_actor = _require_nonempty_str(verification_actor, "verification_actor")
    verification_note = _require_nonempty_str(verification_note, "verification_note")

    if not isinstance(source_deck_ids, list) or not source_deck_ids:
        raise TitleUpdateManifestConfigError(
            f"{path} の entries[{index}].source_deck_ids が空、または配列ではありません。"
        )
    for deck_id in source_deck_ids:
        if not isinstance(deck_id, str) or not deck_id:
            raise TitleUpdateManifestConfigError(
                f"{path} の entries[{index}].source_deck_ids に不正な値があります: {deck_id!r}"
            )

    if verification_status != REQUIRED_VERIFICATION_STATUS:
        raise TitleUpdateManifestConfigError(
            f"{path} の entries[{index}].verification_status が"
            f" '{REQUIRED_VERIFICATION_STATUS}' ではありません(実際: {verification_status!r})。"
        )

    if expected_current_title == confirmed_new_title:
        raise TitleUpdateManifestConfigError(
            f"{path} の entries[{index}]: expected_current_title と confirmed_new_title が"
            f" 同一です('{expected_current_title}')。変更の必要がないエントリは含めないでください。"
        )

    if reference_url is not None and not isinstance(reference_url, str):
        raise TitleUpdateManifestConfigError(
            f"{path} の entries[{index}].reference_url は文字列またはnullである必要があります。"
        )

    return ConfirmedTitleUpdateEntry(
        page_id=page_id,
        expected_current_title=expected_current_title,
        confirmed_new_title=confirmed_new_title,
        expected_english_name=expected_english_name,
        source_deck_ids=list(source_deck_ids),
        verification_status=verification_status,
        verification_actor=verification_actor,
        verification_note=verification_note,
        reference_url=reference_url,
    )


def _validate_no_duplicate_page_ids(
    entries: list[ConfirmedTitleUpdateEntry], path: Path
) -> None:
    seen: set[str] = set()
    for entry in entries:
        if entry.page_id in seen:
            raise TitleUpdateManifestConfigError(
                f"{path}: page_id '{entry.page_id}' が重複しています。"
            )
        seen.add(entry.page_id)


# --- HTTPレベルの書き込みガード(監査ブランチとは独立に実装) -----------------------


@dataclass(frozen=True)
class HttpCallRecord:
    method: str
    url: str
    allowed: bool


def install_http_write_guard(client: NotionClient) -> list[HttpCallRecord]:
    """NotionClientの実HTTP送信層を差し替え、書き込み系リクエストを検出即失敗にする。

    許可: GET(全エンドポイント)、POST .../query であり、かつpayloadが
    ページ作成/更新を示すキー(properties/parent/archived/in_trash)を
    含まない場合のみ(Notionのデータソースクエリはfilter/page_size/
    start_cursorのみを持ち、これらのキーを含まない)。
    禁止: 上記条件を満たさない全てのリクエスト(PATCH・DELETE、
    POST /pages、及び想定外payloadを持つPOST)。
    """
    call_log: list[HttpCallRecord] = []
    original_request = client._client.request  # noqa: SLF001 (意図的な計装)

    def guarded_request(method: str, path: str, **kwargs: Any) -> Any:
        upper = method.upper()
        payload = kwargs.get("json") or {}
        is_query_endpoint = upper == "POST" and path.rstrip("/").endswith("/query")
        payload_looks_mutating = isinstance(payload, dict) and bool(
            {"properties", "parent", "archived", "in_trash"} & set(payload.keys())
        )
        allowed = upper == "GET" or (is_query_endpoint and not payload_looks_mutating)

        call_log.append(HttpCallRecord(method=upper, url=path, allowed=allowed))
        if not allowed:
            raise ReadOnlyGuardError(
                f"dry-run中に書き込み系(または判定不能な)HTTPリクエストが検出されました:"
                f" {upper} {path}(許可されるのは GET と、ページ作成/更新を示す"
                " キーを含まないPOST .../queryのみ)"
            )
        return original_request(method, path, **kwargs)

    client._client.request = guarded_request  # type: ignore[method-assign,assignment]  # noqa: SLF001
    return call_log


# --- 読み取り専用NotionClientラッパー(監査ブランチとは独立に実装) -----------------


class ReadOnlyNotionClient:
    """NotionClientの読み取り専用ラッパー。書き込み系メソッドは公開しない。"""

    def __init__(self, client: NotionClient) -> None:
        self._client = client
        self.method_log: list[str] = []

    def _record(self, name: str) -> None:
        self.method_log.append(name)

    def get_page(self, page_id: str) -> dict:
        self._record("get_page")
        return self._client.get_page(page_id)

    def get_data_source(self, data_source_id: str) -> dict:
        self._record("get_data_source")
        return self._client.get_data_source(data_source_id)

    def query_data_source_by_title(
        self, data_source_id: str, title_property: str, title: str
    ) -> list[dict]:
        self._record("query_data_source_by_title")
        return self._client.query_data_source_by_title(data_source_id, title_property, title)

    def query_data_source_all(self, data_source_id: str, page_size: int = 100) -> list[dict]:
        self._record("query_data_source_all")
        return self._client.query_data_source_all(data_source_id, page_size=page_size)

    def get_page_property_item(
        self, page_id: str, property_id: str, page_size: int = 100
    ) -> list[dict]:
        self._record("get_page_property_item")
        return self._client.get_page_property_item(page_id, property_id, page_size=page_size)

    def read_relation_ids(self, properties: dict, page_id: str, property_name: str) -> list[str]:
        self._record("read_relation_ids")
        return self._client.read_relation_ids(properties, page_id, property_name)

    # --- 書き込み系: 明示的に拒否する ------------------------------------------

    def create_page(self, *args: object, **kwargs: object) -> None:
        raise ReadOnlyGuardError("dry-run処理はcreate_page()を呼び出せません(安全機構違反)。")

    def update_page(self, *args: object, **kwargs: object) -> None:
        raise ReadOnlyGuardError("dry-run処理はupdate_page()を呼び出せません(安全機構違反)。")

    def update_data_source_schema(self, *args: object, **kwargs: object) -> None:
        raise ReadOnlyGuardError(
            "dry-run処理はupdate_data_source_schema()を呼び出せません(安全機構違反)。"
        )


# --- dry-run計画 -------------------------------------------------------------


@dataclass(frozen=True)
class SameTitlePageRef:
    page_id: str
    title: str | None


@dataclass(frozen=True)
class SameTitleCheck:
    searched_title: str
    classification: str  # no_existing_same_title / blocking_same_title
    matching_pages: list[SameTitlePageRef]


@dataclass(frozen=True)
class DeckRelationDetail:
    deck_id: str
    deck_page_fetched: bool
    card_present_in_deck_relation: bool
    deck_relation_count: int | None


@dataclass(frozen=True)
class RelationSnapshot:
    card_to_deck_ids: list[str]
    card_to_deck_count: int
    source_deck_ids_present: bool
    deck_to_card_consistent: bool
    deck_relation_details: list[DeckRelationDetail]


@dataclass(frozen=True)
class TitleUpdatePlanEntry:
    page_id: str
    current_title: str | None
    expected_current_title: str
    confirmed_new_title: str
    current_english_name: str | None
    expected_english_name: str
    verification_status: str
    verification_actor: str
    verification_note: str
    current_title_matches: bool
    english_name_matches: bool
    is_archived_or_trashed: bool
    same_title_check: SameTitleCheck | None
    relation_snapshot: RelationSnapshot | None
    eligible_for_future_update: bool
    blocking_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TitleUpdateDryRunReport:
    audit_timestamp: str
    manifest_path: str
    expected_target_count: int | None
    entries: list[TitleUpdatePlanEntry]
    method_call_log: list[str]
    http_call_log: list[HttpCallRecord]

    @property
    def eligible_count(self) -> int:
        return sum(1 for e in self.entries if e.eligible_for_future_update)

    @property
    def blocked_count(self) -> int:
        return len(self.entries) - self.eligible_count

    @property
    def all_or_nothing_eligible(self) -> bool:
        return bool(self.entries) and self.eligible_count == len(self.entries)


def build_title_update_dry_run_plan(
    client: ReadOnlyNotionClient,
    card_data_source_id: str,
    manifest: ConfirmedTitleUpdateManifest,
    manifest_path: str,
    *,
    now: Any = datetime.datetime.now,
) -> TitleUpdateDryRunReport:
    """人間確認済みマニフェストに基づき、タイトル変更のdry-run計画を作成する。

    対象ページは page_id のみで特定する(英語名・日本語名での検索結果を
    対象決定には使わない。検索は同名ページ衝突確認にのみ使用する)。
    Notionへの書き込みは一切行わない。
    """
    plan_entries = [_plan_one(client, card_data_source_id, entry) for entry in manifest.entries]
    return TitleUpdateDryRunReport(
        audit_timestamp=now().isoformat(),
        manifest_path=manifest_path,
        expected_target_count=None,
        entries=plan_entries,
        method_call_log=list(client.method_log),
        http_call_log=[],
    )


def _plan_one(
    client: ReadOnlyNotionClient, card_data_source_id: str, entry: ConfirmedTitleUpdateEntry
) -> TitleUpdatePlanEntry:
    blocking_reasons: list[str] = []

    try:
        page = client.get_page(entry.page_id)
    except MtgNotionManagerError as exc:
        return TitleUpdatePlanEntry(
            page_id=entry.page_id,
            current_title=None,
            expected_current_title=entry.expected_current_title,
            confirmed_new_title=entry.confirmed_new_title,
            current_english_name=None,
            expected_english_name=entry.expected_english_name,
            verification_status=entry.verification_status,
            verification_actor=entry.verification_actor,
            verification_note=entry.verification_note,
            current_title_matches=False,
            english_name_matches=False,
            is_archived_or_trashed=False,
            same_title_check=None,
            relation_snapshot=None,
            eligible_for_future_update=False,
            blocking_reasons=[f"page_not_found: {exc}"],
        )

    properties = page.get("properties", {})
    current_title = plain_text(properties, TITLE_PROPERTY)
    current_english_name = plain_text(properties, ENGLISH_NAME_PROPERTY)

    is_archived_or_trashed = bool(page.get("archived")) or bool(page.get("in_trash"))
    if is_archived_or_trashed:
        blocking_reasons.append("page_is_archived_or_trashed")

    current_title_matches = current_title == entry.expected_current_title
    if not current_title_matches:
        blocking_reasons.append(
            f"current_title_mismatch: expected={entry.expected_current_title!r},"
            f" actual={current_title!r}"
        )

    english_name_matches = current_english_name == entry.expected_english_name
    if not english_name_matches:
        blocking_reasons.append(
            f"english_name_mismatch: expected={entry.expected_english_name!r},"
            f" actual={current_english_name!r}"
        )

    same_title_check = check_same_title(
        client, card_data_source_id, entry.confirmed_new_title, entry.page_id
    )
    if same_title_check.classification == "blocking_same_title":
        blocking_reasons.append("blocking_same_title_exists")

    relation_snapshot = build_relation_snapshot(client, entry, page, properties)
    if not relation_snapshot.source_deck_ids_present:
        blocking_reasons.append("source_deck_relation_missing")
    if not relation_snapshot.deck_to_card_consistent:
        blocking_reasons.append("deck_to_card_relation_missing")

    return TitleUpdatePlanEntry(
        page_id=entry.page_id,
        current_title=current_title,
        expected_current_title=entry.expected_current_title,
        confirmed_new_title=entry.confirmed_new_title,
        current_english_name=current_english_name,
        expected_english_name=entry.expected_english_name,
        verification_status=entry.verification_status,
        verification_actor=entry.verification_actor,
        verification_note=entry.verification_note,
        current_title_matches=current_title_matches,
        english_name_matches=english_name_matches,
        is_archived_or_trashed=is_archived_or_trashed,
        same_title_check=same_title_check,
        relation_snapshot=relation_snapshot,
        eligible_for_future_update=not blocking_reasons,
        blocking_reasons=blocking_reasons,
    )


def check_same_title(
    client: ReadOnlyNotionClient, card_data_source_id: str, new_title: str, self_page_id: str
) -> SameTitleCheck:
    results = client.query_data_source_by_title(card_data_source_id, TITLE_PROPERTY, new_title)
    matching = [
        SameTitlePageRef(
            page_id=page["id"], title=plain_text(page.get("properties", {}), TITLE_PROPERTY)
        )
        for page in results
        if page["id"] != self_page_id
    ]
    classification = "blocking_same_title" if matching else "no_existing_same_title"
    return SameTitleCheck(
        searched_title=new_title, classification=classification, matching_pages=matching
    )


def build_relation_snapshot(
    client: ReadOnlyNotionClient,
    entry: ConfirmedTitleUpdateEntry,
    page: dict,
    properties: dict,
) -> RelationSnapshot:
    card_to_deck_ids = sorted(
        set(client.read_relation_ids(properties, entry.page_id, DECKS_RELATION_PROPERTY))
    )
    source_deck_ids_present = set(entry.source_deck_ids).issubset(set(card_to_deck_ids))

    deck_details: list[DeckRelationDetail] = []
    deck_to_card_consistent = True
    for deck_id in entry.source_deck_ids:
        try:
            deck_page = client.get_page(deck_id)
        except MtgNotionManagerError:
            deck_details.append(
                DeckRelationDetail(
                    deck_id=deck_id,
                    deck_page_fetched=False,
                    card_present_in_deck_relation=False,
                    deck_relation_count=None,
                )
            )
            deck_to_card_consistent = False
            continue
        deck_properties = deck_page.get("properties", {})
        deck_card_ids = client.read_relation_ids(
            deck_properties, deck_id, COMMANDER_CARDS_RELATION_PROPERTY
        )
        card_present = entry.page_id in deck_card_ids
        if not card_present:
            deck_to_card_consistent = False
        deck_details.append(
            DeckRelationDetail(
                deck_id=deck_id,
                deck_page_fetched=True,
                card_present_in_deck_relation=card_present,
                deck_relation_count=len(deck_card_ids),
            )
        )

    return RelationSnapshot(
        card_to_deck_ids=card_to_deck_ids,
        card_to_deck_count=len(card_to_deck_ids),
        source_deck_ids_present=source_deck_ids_present,
        deck_to_card_consistent=deck_to_card_consistent,
        deck_relation_details=deck_details,
    )


def plain_text(properties: dict, name: str) -> str | None:
    prop = properties.get(name)
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


# --- レポート出力 -----------------------------------------------------------


def to_json_dict(
    report: TitleUpdateDryRunReport, write_operations: int, write_attempts: int
) -> dict:
    return {
        "mode": "dry_run",
        "audit_timestamp": report.audit_timestamp,
        "manifest_path": report.manifest_path,
        "expected_target_count": report.expected_target_count,
        "actual_target_count": len(report.entries),
        "eligible_count": report.eligible_count,
        "blocked_count": report.blocked_count,
        "all_or_nothing_eligible": report.all_or_nothing_eligible,
        "notion_write_operations": write_operations,
        "notion_write_attempts": write_attempts,
        "notion_access": {
            "called_methods": sorted(set(report.method_call_log)),
            "method_call_count": len(report.method_call_log),
        },
        "findings": [_entry_to_dict(e) for e in report.entries],
    }


def _entry_to_dict(entry: TitleUpdatePlanEntry) -> dict:
    return {
        "page_id": entry.page_id,
        "current_title": entry.current_title,
        "expected_current_title": entry.expected_current_title,
        "confirmed_new_title": entry.confirmed_new_title,
        "current_english_name": entry.current_english_name,
        "expected_english_name": entry.expected_english_name,
        "verification_status": entry.verification_status,
        "verification_actor": entry.verification_actor,
        "verification_note": entry.verification_note,
        "current_title_matches": entry.current_title_matches,
        "english_name_matches": entry.english_name_matches,
        "is_archived_or_trashed": entry.is_archived_or_trashed,
        "same_title_check": (
            {
                "searched_title": entry.same_title_check.searched_title,
                "classification": entry.same_title_check.classification,
                "matching_pages": [
                    {"page_id": p.page_id, "title": p.title}
                    for p in entry.same_title_check.matching_pages
                ],
            }
            if entry.same_title_check is not None
            else None
        ),
        "relation_snapshot": (
            {
                "card_to_deck_ids": entry.relation_snapshot.card_to_deck_ids,
                "card_to_deck_count": entry.relation_snapshot.card_to_deck_count,
                "source_deck_ids_present": entry.relation_snapshot.source_deck_ids_present,
                "deck_to_card_consistent": entry.relation_snapshot.deck_to_card_consistent,
                "deck_relation_details": [
                    {
                        "deck_id": d.deck_id,
                        "deck_page_fetched": d.deck_page_fetched,
                        "card_present_in_deck_relation": d.card_present_in_deck_relation,
                        "deck_relation_count": d.deck_relation_count,
                    }
                    for d in entry.relation_snapshot.deck_relation_details
                ],
            }
            if entry.relation_snapshot is not None
            else None
        ),
        "planned_change": {
            "property": TITLE_PROPERTY,
            "before": entry.current_title,
            "after": entry.confirmed_new_title,
        },
        "properties_confirmed_unchanged": [ENGLISH_NAME_PROPERTY, DECKS_RELATION_PROPERTY],
        "eligible_for_future_update": entry.eligible_for_future_update,
        "blocking_reasons": entry.blocking_reasons,
    }


def write_json_report(data: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_markdown_report(data: dict, path: Path) -> Path:
    buf = io.StringIO()
    buf.write("# カードタイトル更新 dry-runレポート(読み取り専用)\n\n")
    buf.write(f"- 実行日時: {data['audit_timestamp']}\n")
    buf.write(f"- マニフェスト: {data['manifest_path']}\n")
    buf.write(
        f"- 期待件数: {data['expected_target_count']} / 実際件数: {data['actual_target_count']}\n"
    )
    buf.write(f"- 適用可能: {data['eligible_count']} / ブロック: {data['blocked_count']}\n")
    buf.write(f"- all-or-nothing判定: {data['all_or_nothing_eligible']}\n")
    buf.write(f"- Notion書き込み操作数: {data['notion_write_operations']}\n")
    buf.write(f"- Notion書き込み試行数: {data['notion_write_attempts']}\n\n")

    buf.write("## 対象一覧\n\n")
    buf.write(
        "| page_id | 現在タイトル | 新タイトル | タイトル一致 | 英語名一致 | "
        "同名衝突 | relation整合 | 適用可能 | ブロック理由 |\n"
    )
    buf.write("|---|---|---|---|---|---|---|---|---|\n")
    for f in data["findings"]:
        same_title = f["same_title_check"]["classification"] if f["same_title_check"] else "N/A"
        rel_ok = (
            f["relation_snapshot"]["source_deck_ids_present"]
            and f["relation_snapshot"]["deck_to_card_consistent"]
            if f["relation_snapshot"]
            else False
        )
        buf.write(
            f"| {f['page_id']} | {f['current_title']} | {f['confirmed_new_title']} |"
            f" {f['current_title_matches']} | {f['english_name_matches']} | {same_title} |"
            f" {rel_ok} | {f['eligible_for_future_update']} |"
            f" {'; '.join(f['blocking_reasons']) or '-'} |\n"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path
