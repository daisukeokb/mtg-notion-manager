from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from mtg_notion_manager.exceptions import NotionAPIError
from mtg_notion_manager.fetchers import get_fetcher
from mtg_notion_manager.mapping import normalize_colors, normalize_set_name
from mtg_notion_manager.models import DeckRecord, ExistingDeck
from mtg_notion_manager.notion.client import AMBIGUOUS_WRITE_STATUS_CODES
from mtg_notion_manager.notion.writer import DiffEntry, NotionWriter


class DeckCreateCompletion:
    """デッキ新規作成(POST /pages、べき等でない)が失敗したときに、書き込みの完了状態に
    ついてわかっている事実。NotionAPIError.__cause__ の構造化データ(例外の型・HTTP
    status code)だけから判定する(str(exc)のメッセージ文字列は一切見ない)。

    この層はError Contract(mutation_contract.py/error_contract.py)へ依存しない
    (公開contractの語彙への変換は import_deck_mutation_adapter.py の責務)。
    """

    #: サーバーが明示的に拒否したHTTPエラー応答(400等、リトライを使い切った429)。
    KNOWN_FAILED = "known_failed"
    #: 500/502/503/504(サーバー側で実際には作成済みの可能性を排除できない)、または
    #: タイムアウト・接続エラー等のHTTPエラー応答以外。
    UNKNOWN = "unknown"


def classify_deck_create_failure(exc: NotionAPIError) -> str:
    """execute_import()が送出したNotionAPIErrorの完了状態を判定する。

    分類規則はimport-cardsの新規カード作成(import_cards._create_completion_from_
    notion_api_error、Phase 3F-R1)と同じ: HTTPエラー応答のうち
    AMBIGUOUS_WRITE_STATUS_CODES(500/502/503/504)はUNKNOWN、それ以外の
    HTTPエラー応答はKNOWN_FAILED、HTTPエラー応答以外(タイムアウト・接続エラー・
    原因不明)は安全側のUNKNOWN。
    """
    cause = exc.__cause__
    if (
        isinstance(cause, httpx.HTTPStatusError)
        and cause.response.status_code not in AMBIGUOUS_WRITE_STATUS_CODES
    ):
        return DeckCreateCompletion.KNOWN_FAILED
    return DeckCreateCompletion.UNKNOWN


@dataclass(frozen=True)
class ImportPlan:
    """Notionへの書き込み前に確定する内容。

    dry-run/本番いずれもこのplanまでは同じルートで作成する。
    """

    record: DeckRecord
    existing: ExistingDeck | None
    diff: list[DiffEntry] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return self.existing is not None


def build_import_plan(
    url: str, writer: NotionWriter, deck_name: str | None = None, html: str | None = None
) -> ImportPlan:
    """URLからデッキ情報を取得・正規化し、Notion上の重複状況まで確認する。

    1ページに複数デッキが含まれる場合は deck_name で対象を指定する。
    ここではNotionへの書き込み(create)は行わない(検索のみ)。
    html を渡した場合はダウンロードを省略して再利用する。
    """
    fetcher = get_fetcher(url)
    raw = fetcher.parse(html, url, deck_name) if html is not None else fetcher.fetch(url, deck_name)

    record = DeckRecord(
        name=raw.name,
        commander=raw.commander,
        set_name=normalize_set_name(raw.set_raw),
        colors=normalize_colors(raw.colors_raw),
        deck_list_url=raw.source_url,
    )

    existing = writer.find_existing_deck(record.name)
    diff = writer.diff_against(existing, record) if existing is not None else []

    return ImportPlan(record=record, existing=existing, diff=diff)


def execute_import(plan: ImportPlan, writer: NotionWriter) -> dict:
    """Notionへ実際に書き込む。重複がある場合は呼び出し側の責任で事前にブロックすること。"""
    if plan.is_duplicate:
        raise ValueError("重複デッキが存在するため新規作成できません。")
    return writer.create_deck(plan.record)
