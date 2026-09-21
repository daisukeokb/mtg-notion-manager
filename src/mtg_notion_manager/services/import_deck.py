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
    url: str,
    writer: NotionWriter,
    deck_name: str | None = None,
    html: str | None = None,
    name_override: str | None = None,
    commander_override: str | None = None,
) -> ImportPlan:
    """URLからデッキ情報を取得・正規化し、Notion上の重複状況まで確認する。

    1ページに複数デッキが含まれる場合は deck_name で対象を指定する
    (記事側のdeck-title属性と完全一致する値。英語記事でdeck-titleや統率者名が
    英語表記のままの場合など、そのままではNotion側の登録名として不適切な
    ことがあるため、name_override / commander_override を指定すると
    Notionへ登録する「名前」「統率者」だけをそれぞれ上書きできる
    (deck_nameによる記事側のデッキ選択には影響しない)。
    ここではNotionへの書き込み(create)は行わない(検索のみ)。
    html を渡した場合はダウンロードを省略して再利用する。
    """
    fetcher = get_fetcher(url)
    raw = fetcher.parse(html, url, deck_name) if html is not None else fetcher.fetch(url, deck_name)

    record = DeckRecord(
        name=name_override if name_override else raw.name,
        commander=commander_override if commander_override else raw.commander,
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
