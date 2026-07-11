from __future__ import annotations

from dataclasses import dataclass, field

from mtg_notion_manager.fetchers import get_fetcher
from mtg_notion_manager.mapping import normalize_colors, normalize_set_name
from mtg_notion_manager.models import DeckRecord, ExistingDeck
from mtg_notion_manager.notion.writer import DiffEntry, NotionWriter


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
    url: str, writer: NotionWriter, deck_name: str | None = None
) -> ImportPlan:
    """URLからデッキ情報を取得・正規化し、Notion上の重複状況まで確認する。

    1ページに複数デッキが含まれる場合は deck_name で対象を指定する。
    ここではNotionへの書き込み(create)は行わない(検索のみ)。
    """
    fetcher = get_fetcher(url)
    raw = fetcher.fetch(url, deck_name)

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
