"""人間確認済みカードタイトルを、対象1件・プロパティ1件・書き込み1回に限定して
安全に更新するための機能。

背景:
`title_update_dry_run.py` は読み取り専用のdry-run計画のみを提供する。本モジュールは
それを再利用しつつ、「将来1件だけを安全に実更新する」ための追加の安全層を実装する:

- operation digest: preflight結果から算出するSHA-256ダイジェスト。適用直前に
  再計算した値と `--approval-digest` が完全一致しない限り書き込みを許可しない。
- 最小権限writer(SingleTitleUpdateWriter): page_id・タイトルプロパティ名・
  新タイトルの3値しか受け取れない型。relationや他プロパティを一切渡せない。
- 単一操作HTTP write guard: 承認されたpage_idへの、タイトルプロパティ1件だけの
  PATCHを、最大1回だけ許可する。それ以外(別page_id・2回目の書き込み・
  他プロパティ・relation・ページ作成/削除・コメント・スキーマ変更等)は
  検出即座に例外にする。
- 書き込み直前の楽観的ロック: 適用直前にpreflightを再実行し、タイトル・英語名・
  relation・アーカイブ状態・last_edited_time・digestのいずれかが変化していれば
  書き込みを行わず中止する。
- 事後検証: 更新直後にページとrelationを再取得し、タイトルの変更と、
  それ以外(英語名・page_id・relation・アーカイブ状態)が不変であることを確認する。
  事後検証に失敗しても自動rollbackや自動再試行は一切行わない
  (誤更新時は本モジュールとは独立したrollbackマニフェスト・別承認フローを
  別途用意して対応する前提であり、本モジュールにその機能は含まれない)。

このモジュールはNotionへ実際に書き込みを行う経路(SingleTitleUpdateWriter経由)を
含むが、CLI側で `--apply` かつ `--approval-digest` 完全一致かつ全安全確認通過の
場合のみ到達する設計であり、それ以外の経路(dry-run/preflightのみ)では一切
書き込みを行わない。
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.services.title_update_dry_run import (
    COMMANDER_CARDS_RELATION_PROPERTY,
    DECKS_RELATION_PROPERTY,
    ENGLISH_NAME_PROPERTY,
    ConfirmedTitleUpdateEntry,
    ConfirmedTitleUpdateManifest,
    ReadOnlyNotionClient,
    build_relation_snapshot,
    check_same_title,
    load_confirmed_title_update_manifest,
    plain_text,
)

TITLE_PROPERTY_TYPE = "title"


class SingleUpdateConfigError(MtgNotionManagerError):
    """1件専用更新の設定・入力(マニフェスト・件数・承認ダイジェスト等)が不正。"""


class SingleUpdateGuardError(MtgNotionManagerError):
    """1件専用更新の安全機構違反(書き込みガード拒否・楽観的ロック不一致等)。"""


# --- タイトルプロパティ名の解決(推測しない) -----------------------------------


def resolve_title_property_name(schema: dict) -> str:
    """データソースの実スキーマから、type=="title" のプロパティ名を取得する。

    ハードコードされた名前を仮定しない。Notionのデータベースには
    title型プロパティが必ず1つだけ存在するため、0件・複数件はスキーマ異常として
    エラーにする。
    """
    properties = schema.get("properties", {})
    title_props = [name for name, prop in properties.items() if prop.get("type") == "title"]
    if len(title_props) != 1:
        raise SingleUpdateConfigError(
            f"データソースのtitle型プロパティが1件ではありません(検出: {title_props})。"
        )
    return title_props[0]


# --- マニフェスト(既存ローダーを再利用し、1件限定を追加検証) ---------------------


def load_single_update_manifest(path: Path) -> ConfirmedTitleUpdateEntry:
    """1件専用更新マニフェストを読み込み、entryが正確に1件であることを検証する。

    既存の load_confirmed_title_update_manifest() をそのまま再利用する
    (expected_entry_count=1 を強制することで1件専用の制約を実装する)。
    """
    manifest = load_confirmed_title_update_manifest(path, expected_entry_count=1)
    return manifest.entries[0]


# --- preflight・operation digest --------------------------------------------


@dataclass(frozen=True)
class SingleUpdatePreflightResult:
    page_id: str
    title_property_name: str
    current_title: str | None
    expected_current_title: str
    confirmed_new_title: str
    current_english_name: str | None
    expected_english_name: str
    is_archived_or_trashed: bool
    last_edited_time: str | None
    same_title_check: Any
    relation_snapshot: Any
    current_title_matches: bool
    english_name_matches: bool
    eligible_for_future_update: bool
    blocking_reasons: list[str] = field(default_factory=list)
    operation_digest: str = ""


def build_single_update_preflight(
    client: ReadOnlyNotionClient,
    card_data_source_id: str,
    entry: ConfirmedTitleUpdateEntry,
) -> SingleUpdatePreflightResult:
    """対象1件について、読み取り専用preflightを実行する。

    Notionへの書き込みは一切行わない(ReadOnlyNotionClientのみ使用)。
    """
    schema = client.get_data_source(card_data_source_id)
    title_property_name = resolve_title_property_name(schema)

    blocking_reasons: list[str] = []

    try:
        page = client.get_page(entry.page_id)
    except MtgNotionManagerError as exc:
        return SingleUpdatePreflightResult(
            page_id=entry.page_id,
            title_property_name=title_property_name,
            current_title=None,
            expected_current_title=entry.expected_current_title,
            confirmed_new_title=entry.confirmed_new_title,
            current_english_name=None,
            expected_english_name=entry.expected_english_name,
            is_archived_or_trashed=False,
            last_edited_time=None,
            same_title_check=None,
            relation_snapshot=None,
            current_title_matches=False,
            english_name_matches=False,
            eligible_for_future_update=False,
            blocking_reasons=[f"page_not_found: {exc}"],
        )

    properties = page.get("properties", {})
    current_title = plain_text(properties, title_property_name)
    current_english_name = plain_text(properties, ENGLISH_NAME_PROPERTY)
    is_archived_or_trashed = bool(page.get("archived")) or bool(page.get("in_trash"))
    last_edited_time = page.get("last_edited_time")

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

    result = SingleUpdatePreflightResult(
        page_id=entry.page_id,
        title_property_name=title_property_name,
        current_title=current_title,
        expected_current_title=entry.expected_current_title,
        confirmed_new_title=entry.confirmed_new_title,
        current_english_name=current_english_name,
        expected_english_name=entry.expected_english_name,
        is_archived_or_trashed=is_archived_or_trashed,
        last_edited_time=last_edited_time,
        same_title_check=same_title_check,
        relation_snapshot=relation_snapshot,
        current_title_matches=current_title_matches,
        english_name_matches=english_name_matches,
        eligible_for_future_update=not blocking_reasons,
        blocking_reasons=blocking_reasons,
    )
    digest = compute_operation_digest(entry, result)
    return SingleUpdatePreflightResult(**{**result.__dict__, "operation_digest": digest})


def compute_operation_digest(
    entry: ConfirmedTitleUpdateEntry, preflight: SingleUpdatePreflightResult
) -> str:
    """preflight結果から正規化済みJSONを作り、SHA-256ダイジェストを算出する。

    固定の承認トークンはコードへ埋め込まない。ダイジェストは
    page_id・期待タイトル・新タイトル・期待英語名・source_deck_ids・
    現在のrelationページID一覧・アーカイブ/ゴミ箱状態・last_edited_time
    (利用可能な場合)から決定的に算出する。
    """
    relation_ids = (
        sorted(preflight.relation_snapshot.card_to_deck_ids)
        if preflight.relation_snapshot is not None
        else []
    )
    payload = {
        "page_id": entry.page_id,
        "expected_current_title": entry.expected_current_title,
        "confirmed_new_title": entry.confirmed_new_title,
        "expected_english_name": entry.expected_english_name,
        "source_deck_ids": sorted(entry.source_deck_ids),
        "current_relation_page_ids": relation_ids,
        "is_archived_or_trashed": preflight.is_archived_or_trashed,
        "last_edited_time": preflight.last_edited_time,
    }
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# --- 最小権限writer -----------------------------------------------------------


class SingleTitleUpdateWriter:
    """タイトルプロパティ1件だけを更新できる、最小権限のwriter。

    page_id・タイトルプロパティ名・新タイトルの3値しか受け取らない
    (relationや他プロパティを渡す手段がAPI上存在しない)。
    """

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def update_title(self, page_id: str, title_property_name: str, new_title: str) -> dict:
        properties = {title_property_name: {"title": [{"text": {"content": new_title}}]}}
        return self._client.update_page(page_id, properties)


# --- 単一操作HTTP write guard --------------------------------------------------


@dataclass(frozen=True)
class GuardedHttpCallRecord:
    method: str
    url: str
    allowed: bool
    reason: str = ""


def install_single_title_update_write_guard(
    client: NotionClient,
    *,
    approved_page_id: str,
    title_property_name: str,
    approved_new_title: str,
) -> list[GuardedHttpCallRecord]:
    """実HTTP送信層を差し替え、承認された1操作だけを許可するガードを設置する。

    許可: 承認されたpage_idへの PATCH /pages/{approved_page_id} で、
    payloadのpropertiesキーがタイトルプロパティ1件だけであり、
    その内容が approved_new_title と完全一致する場合に限り、最大1回だけ許可する。
    そのほか(GET・POST .../query を含む読み取り操作)は従来どおり許可する。
    それ以外は全て拒否する。
    """
    call_log: list[GuardedHttpCallRecord] = []
    write_count = {"value": 0}
    original_request = client._client.request  # noqa: SLF001 (意図的な計装)
    expected_path = f"/pages/{approved_page_id}"
    expected_properties = {
        title_property_name: {"title": [{"text": {"content": approved_new_title}}]}
    }

    def guarded_request(method: str, path: str, **kwargs: Any) -> Any:
        upper = method.upper()
        payload = kwargs.get("json") or {}
        is_query_post = upper == "POST" and path.rstrip("/").endswith("/query")

        if upper == "GET" or is_query_post:
            call_log.append(GuardedHttpCallRecord(method=upper, url=path, allowed=True))
            return original_request(method, path, **kwargs)

        reason = _classify_write_rejection(
            upper,
            path,
            payload,
            expected_path=expected_path,
            expected_properties=expected_properties,
            write_count=write_count["value"],
        )
        if reason is not None:
            call_log.append(
                GuardedHttpCallRecord(method=upper, url=path, allowed=False, reason=reason)
            )
            raise SingleUpdateGuardError(
                f"承認されていない書き込みリクエストが検出されました: {upper} {path}"
                f"({reason})"
            )

        write_count["value"] += 1
        call_log.append(GuardedHttpCallRecord(method=upper, url=path, allowed=True))
        return original_request(method, path, **kwargs)

    client._client.request = guarded_request  # type: ignore[method-assign,assignment]  # noqa: SLF001
    return call_log


def _classify_write_rejection(
    method: str,
    path: str,
    payload: dict,
    *,
    expected_path: str,
    expected_properties: dict,
    write_count: int,
) -> str | None:
    if write_count >= 1:
        return "write_limit_exceeded(2回目以降の書き込み)"
    if method != "PATCH":
        return f"unexpected_http_method({method})"
    if path != expected_path:
        return f"unexpected_page_id(path={path})"
    if not isinstance(payload, dict):
        return "unexpected_payload_type"
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        return "missing_properties_key"
    if set(payload.keys()) != {"properties"}:
        return f"unexpected_top_level_keys({sorted(payload.keys())})"
    if set(properties.keys()) != set(expected_properties.keys()):
        return f"unexpected_properties({sorted(properties.keys())})"
    if properties != expected_properties:
        return "title_content_mismatch"
    return None


# --- 書き込み直前の楽観的ロック -------------------------------------------------


def verify_optimistic_lock(
    baseline: SingleUpdatePreflightResult, fresh: SingleUpdatePreflightResult
) -> list[str]:
    """適用直前の再取得結果が、preflight時点から変化していないか確認する。

    1件でも変化があれば、その理由のリストを返す(空リストなら変化なし)。
    呼び出し側は、空リストでない場合に書き込みを行ってはならない。
    """
    changes: list[str] = []
    if baseline.current_title != fresh.current_title:
        changes.append("current_title_changed")
    if baseline.current_english_name != fresh.current_english_name:
        changes.append("english_name_changed")
    if baseline.relation_snapshot is not None and fresh.relation_snapshot is not None:
        if (
            baseline.relation_snapshot.card_to_deck_ids
            != fresh.relation_snapshot.card_to_deck_ids
        ):
            changes.append("relation_ids_changed")
    if baseline.is_archived_or_trashed != fresh.is_archived_or_trashed:
        changes.append("archive_or_trash_state_changed")
    if baseline.last_edited_time != fresh.last_edited_time:
        changes.append("last_edited_time_changed")
    if baseline.operation_digest != fresh.operation_digest:
        changes.append("operation_digest_changed")
    return changes


# --- 適用前スナップショット(将来のrollback判断材料。rollback機能自体は未実装) -----


@dataclass(frozen=True)
class PreApplySnapshot:
    page_id: str
    title_before: str | None
    english_name_before: str | None
    relation_ids_before: list[str]
    is_archived_or_trashed: bool
    operation_digest: str
    captured_at: str

    @staticmethod
    def from_preflight(preflight: SingleUpdatePreflightResult, now: str) -> PreApplySnapshot:
        relation_ids = (
            list(preflight.relation_snapshot.card_to_deck_ids)
            if preflight.relation_snapshot is not None
            else []
        )
        return PreApplySnapshot(
            page_id=preflight.page_id,
            title_before=preflight.current_title,
            english_name_before=preflight.current_english_name,
            relation_ids_before=relation_ids,
            is_archived_or_trashed=preflight.is_archived_or_trashed,
            operation_digest=preflight.operation_digest,
            captured_at=now,
        )


# --- 事後検証 -----------------------------------------------------------------


@dataclass(frozen=True)
class PostVerificationResult:
    title_updated_to_expected: bool
    english_name_unchanged: bool
    page_id_unchanged: bool
    relation_ids_unchanged: bool
    source_deck_relation_maintained: bool
    deck_to_card_consistent: bool
    archive_or_trash_state_unchanged: bool
    write_count_is_one: bool

    @property
    def all_passed(self) -> bool:
        return all(
            [
                self.title_updated_to_expected,
                self.english_name_unchanged,
                self.page_id_unchanged,
                self.relation_ids_unchanged,
                self.source_deck_relation_maintained,
                self.deck_to_card_consistent,
                self.archive_or_trash_state_unchanged,
                self.write_count_is_one,
            ]
        )


def verify_post_update(
    snapshot: PreApplySnapshot,
    after: SingleUpdatePreflightResult,
    entry: ConfirmedTitleUpdateEntry,
    write_count: int,
) -> PostVerificationResult:
    """更新直後の再取得結果を、適用前スナップショットと突き合わせる(読み取り専用)。

    失敗しても自動rollback・自動再試行は一切行わない(呼び出し側の責務)。
    """
    relation_after = (
        list(after.relation_snapshot.card_to_deck_ids)
        if after.relation_snapshot is not None
        else []
    )
    return PostVerificationResult(
        title_updated_to_expected=after.current_title == entry.confirmed_new_title,
        english_name_unchanged=after.current_english_name == snapshot.english_name_before,
        page_id_unchanged=after.page_id == snapshot.page_id,
        relation_ids_unchanged=sorted(relation_after) == sorted(snapshot.relation_ids_before),
        source_deck_relation_maintained=(
            after.relation_snapshot.source_deck_ids_present
            if after.relation_snapshot is not None
            else False
        ),
        deck_to_card_consistent=(
            after.relation_snapshot.deck_to_card_consistent
            if after.relation_snapshot is not None
            else False
        ),
        archive_or_trash_state_unchanged=(
            after.is_archived_or_trashed == snapshot.is_archived_or_trashed
        ),
        write_count_is_one=(write_count == 1),
    )


# --- レポート出力 -----------------------------------------------------------


def preflight_to_json_dict(preflight: SingleUpdatePreflightResult, write_operations: int) -> dict:
    return {
        "mode": "preflight",
        "page_id": preflight.page_id,
        "title_property_name": preflight.title_property_name,
        "current_title": preflight.current_title,
        "expected_current_title": preflight.expected_current_title,
        "confirmed_new_title": preflight.confirmed_new_title,
        "current_english_name": preflight.current_english_name,
        "expected_english_name": preflight.expected_english_name,
        "current_title_matches": preflight.current_title_matches,
        "english_name_matches": preflight.english_name_matches,
        "is_archived_or_trashed": preflight.is_archived_or_trashed,
        "last_edited_time": preflight.last_edited_time,
        "same_title_check": (
            {
                "searched_title": preflight.same_title_check.searched_title,
                "classification": preflight.same_title_check.classification,
                "matching_pages": [
                    {"page_id": p.page_id, "title": p.title}
                    for p in preflight.same_title_check.matching_pages
                ],
            }
            if preflight.same_title_check is not None
            else None
        ),
        "relation_snapshot": (
            {
                "card_to_deck_ids": preflight.relation_snapshot.card_to_deck_ids,
                "card_to_deck_count": preflight.relation_snapshot.card_to_deck_count,
                "source_deck_ids_present": preflight.relation_snapshot.source_deck_ids_present,
                "deck_to_card_consistent": preflight.relation_snapshot.deck_to_card_consistent,
            }
            if preflight.relation_snapshot is not None
            else None
        ),
        "eligible_for_future_update": preflight.eligible_for_future_update,
        "blocking_reasons": preflight.blocking_reasons,
        "operation_digest": preflight.operation_digest,
        "approval_required": True,
        "notion_write_operations": write_operations,
        "notion_write_attempts": write_operations,
    }


def write_json_report(data: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_markdown_report(data: dict, path: Path) -> Path:
    buf = io.StringIO()
    buf.write("# カードタイトル1件更新 preflightレポート(読み取り専用)\n\n")
    buf.write(f"- page_id: {data['page_id']}\n")
    buf.write(f"- 現在タイトル: {data['current_title']} (期待: {data['expected_current_title']})\n")
    buf.write(f"- 新タイトル: {data['confirmed_new_title']}\n")
    buf.write(f"- タイトル一致: {data['current_title_matches']}\n")
    buf.write(f"- 英語名一致: {data['english_name_matches']}\n")
    buf.write(f"- 適用可能: {data['eligible_for_future_update']}\n")
    buf.write(f"- ブロック理由: {'; '.join(data['blocking_reasons']) or 'なし'}\n")
    buf.write(f"- operation_digest: `{data['operation_digest']}`\n")
    buf.write(f"- approval_required: {data['approval_required']}\n")
    buf.write(f"- Notion書き込み操作数: {data['notion_write_operations']}\n")
    buf.write(f"- Notion書き込み試行数: {data['notion_write_attempts']}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


__all__ = [
    "COMMANDER_CARDS_RELATION_PROPERTY",
    "DECKS_RELATION_PROPERTY",
    "ConfirmedTitleUpdateManifest",
    "GuardedHttpCallRecord",
    "PostVerificationResult",
    "PreApplySnapshot",
    "SingleTitleUpdateWriter",
    "SingleUpdateConfigError",
    "SingleUpdateGuardError",
    "SingleUpdatePreflightResult",
    "build_single_update_preflight",
    "compute_operation_digest",
    "install_single_title_update_write_guard",
    "load_single_update_manifest",
    "preflight_to_json_dict",
    "resolve_title_property_name",
    "verify_optimistic_lock",
    "verify_post_update",
    "write_json_report",
    "write_markdown_report",
]
