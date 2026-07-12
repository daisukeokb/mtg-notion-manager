"""公式記事内の複数統率者デッキを安全に一括取り込みするオーケストレーション(import-article)。

処理フロー:
1. 記事HTMLを1回だけ取得する(以降は使い回し、記事内デッキ数だけ再取得しない)。
2. 記事内の全デッキ名を抽出する(--exclude-deck で除外可能)。
3. 各デッキについて、MTG統率者DBの既存デッキと完全一致で照合する
   (一致しないデッキは要確認として扱い、新規作成はしない)。
4. カードDBは呼び出し側が渡す1つの CardRepository を全デッキで共有する
   (CardRepository.load() は初回のみ実際に取得するため、記事全体で1回だけになる)。
5. 各デッキのカードリストを既存の import_cards.build_import_cards_plan() で解析・照合する
   (ロジックの二重化を避けるため、ロジック自体は再利用する)。
6. 曖昧一致など未解決の判定が1件でもあるデッキは、そのデッキだけ要確認として処理を止め、
   他の安全なデッキの計画作成・適用は継続する。
7. --apply時はデッキ単位で個別に適用し、1デッキの失敗が他デッキの結果を失わせないようにする。

日本語カード名の設計方針(今回は設計のみ、実装しない):
- magic.wizards.com の記事は英語名しか取得できない。カード名が新規(create)になる場合、
  日本語名が不明なまま英語名だけでカードDBへ登録される(既存のimport-cards挙動を踏襲)。
- 将来、Scryfall API (`GET /cards/named?exact=<英語名>` → `prints_search_uri` で日本語版の
  印刷を検索し `printed_name` を取得)を使って日本語名を自動補完する設計を想定する。
  ただし、対応する日本語印刷が存在しない・複数候補がある場合は自動選択せず、
  英語名のまま登録するか要確認として止めるべき(誤訳防止を優先するため)。
- 今回はこの設計案の提示のみとし、英語名からの推測・機械翻訳は一切行わない。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.fetchers import get_fetcher
from mtg_notion_manager.fetchers.base import download
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.services.import_cards import (
    BLOCKING_ACTIONS,
    ImportCardsPlan,
    ImportCardsResult,
    build_import_cards_plan,
    execute_import_cards,
)

STATUS_READY = "ready"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_ERROR = "error"

STATUS_LABELS: dict[str, str] = {
    STATUS_READY: "処理可能",
    STATUS_NEEDS_REVIEW: "要確認",
    STATUS_ERROR: "エラー",
}


@dataclass(frozen=True)
class DeckArticleEntry:
    deck_name: str
    status: str
    reason: str = ""
    deck_page_id: str | None = None
    deck_page_url: str | None = None
    cards_plan: ImportCardsPlan | None = None
    apply_result: ImportCardsResult | None = None


@dataclass(frozen=True)
class ArticleImportPlan:
    source_url: str
    all_deck_names: list[str]
    excluded_deck_names: list[str]
    entries: list[DeckArticleEntry]

    @property
    def counts(self) -> dict[str, int]:
        counts = {STATUS_READY: 0, STATUS_NEEDS_REVIEW: 0, STATUS_ERROR: 0}
        for entry in self.entries:
            counts[entry.status] += 1
        return counts


def build_article_import_plan(
    url: str,
    writer: NotionWriter,
    card_repo: CardRepository,
    exclude_deck_names: list[str] | None = None,
    allow_count_mismatch: bool = False,
) -> ArticleImportPlan:
    """記事HTMLを1回だけ取得し、記事内の全デッキを解析・検証する(Notionへの書き込みなし)。"""
    exclude_deck_names = exclude_deck_names or []
    html = download(url)
    fetcher = get_fetcher(url)
    all_deck_names = fetcher.list_deck_names(html, url)

    target_names = [name for name in all_deck_names if name not in exclude_deck_names]

    card_repo.load()

    entries = [
        _build_one_deck_entry(url, html, name, writer, card_repo, allow_count_mismatch)
        for name in target_names
    ]

    return ArticleImportPlan(
        source_url=url,
        all_deck_names=all_deck_names,
        excluded_deck_names=[n for n in exclude_deck_names if n in all_deck_names],
        entries=entries,
    )


def _build_one_deck_entry(
    url: str,
    html: str,
    deck_name: str,
    writer: NotionWriter,
    card_repo: CardRepository,
    allow_count_mismatch: bool,
) -> DeckArticleEntry:
    existing_deck = writer.find_existing_deck(deck_name)
    if existing_deck is None:
        return DeckArticleEntry(
            deck_name=deck_name,
            status=STATUS_NEEDS_REVIEW,
            reason=(
                "MTG統率者DBに一致するデッキが見つかりません"
                "(先に import コマンドで登録してください)"
            ),
        )

    try:
        cards_plan = build_import_cards_plan(
            url,
            existing_deck.page_id,
            card_repo,
            deck_name=deck_name,
            allow_count_mismatch=allow_count_mismatch,
            html=html,
        )
    except MtgNotionManagerError as exc:
        return DeckArticleEntry(
            deck_name=deck_name,
            status=STATUS_ERROR,
            reason=str(exc),
            deck_page_id=existing_deck.page_id,
            deck_page_url=existing_deck.page_url,
        )

    if cards_plan.has_blocking_issues:
        blocking = [d for d in cards_plan.decisions if d.action in BLOCKING_ACTIONS]
        details = "; ".join(f"{d.card.display_name}: {d.detail}" for d in blocking)
        return DeckArticleEntry(
            deck_name=deck_name,
            status=STATUS_NEEDS_REVIEW,
            reason=f"曖昧一致または未解決のカードが{len(blocking)}件あります: {details}",
            deck_page_id=existing_deck.page_id,
            deck_page_url=existing_deck.page_url,
            cards_plan=cards_plan,
        )

    return DeckArticleEntry(
        deck_name=deck_name,
        status=STATUS_READY,
        deck_page_id=existing_deck.page_id,
        deck_page_url=existing_deck.page_url,
        cards_plan=cards_plan,
    )


def execute_article_import(
    plan: ArticleImportPlan, card_repo: CardRepository, note: str = ""
) -> ArticleImportPlan:
    """処理可能(ready)なデッキのみ、デッキ単位で個別に適用する。

    1デッキの失敗が他デッキの結果を失わせないよう、デッキごとに独立して例外を捕捉する。
    """
    new_entries = []
    for entry in plan.entries:
        if entry.status != STATUS_READY or entry.cards_plan is None:
            new_entries.append(entry)
            continue
        try:
            result = execute_import_cards(entry.cards_plan, card_repo, note=note)
        except MtgNotionManagerError as exc:
            new_entries.append(
                DeckArticleEntry(
                    deck_name=entry.deck_name,
                    status=STATUS_ERROR,
                    reason=str(exc),
                    deck_page_id=entry.deck_page_id,
                    deck_page_url=entry.deck_page_url,
                    cards_plan=entry.cards_plan,
                )
            )
            continue
        new_entries.append(
            DeckArticleEntry(
                deck_name=entry.deck_name,
                status=entry.status,
                deck_page_id=entry.deck_page_id,
                deck_page_url=entry.deck_page_url,
                cards_plan=entry.cards_plan,
                apply_result=result,
            )
        )

    return ArticleImportPlan(
        source_url=plan.source_url,
        all_deck_names=plan.all_deck_names,
        excluded_deck_names=plan.excluded_deck_names,
        entries=new_entries,
    )


# --- ログ出力 -----------------------------------------------------------


@dataclass(frozen=True)
class ArticleImportLogPaths:
    json_path: Path


def write_article_import_log(
    plan: ArticleImportPlan,
    output_dir: Path,
    applied: bool,
    timestamp: str | None = None,
) -> ArticleImportLogPaths:
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"import-article-{timestamp}.json"

    log = {
        "executed_at": datetime.datetime.now().isoformat(),
        "source_url": plan.source_url,
        "applied": applied,
        "all_deck_names": plan.all_deck_names,
        "excluded_deck_names": plan.excluded_deck_names,
        "summary": plan.counts,
        "delete_count": 0,
        "decks": [_entry_to_dict(entry) for entry in plan.entries],
    }

    json_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return ArticleImportLogPaths(json_path=json_path)


def _entry_to_dict(entry: DeckArticleEntry) -> dict:
    result: dict = {
        "deck_name": entry.deck_name,
        "status": entry.status,
        "status_label": STATUS_LABELS[entry.status],
        "reason": entry.reason,
        "deck_page_id": entry.deck_page_id,
        "deck_page_url": entry.deck_page_url,
    }
    if entry.cards_plan is not None:
        parsed = entry.cards_plan.parsed
        counts = entry.cards_plan.summary
        result["extracted_quantity"] = parsed.total_quantity
        result["unique_card_count"] = len(parsed.cards)
        result["existing_card_count"] = counts.get("relation_update", 0) + counts.get(
            "unchanged", 0
        )
        result["new_card_count"] = counts.get("create", 0)
        result["relation_added_count"] = counts.get("relation_update", 0)
        result["unchanged_count"] = counts.get("unchanged", 0)
        result["ambiguous_count"] = counts.get("ambiguous", 0)
        result["error_count"] = counts.get("error", 0)
    if entry.apply_result is not None:
        result["apply"] = {
            "created": sum(1 for r in entry.apply_result.results if r.action == "created"),
            "relation_updated": sum(
                1 for r in entry.apply_result.results if r.action == "relation_updated"
            ),
            "unchanged": sum(1 for r in entry.apply_result.results if r.action == "unchanged"),
            "failed": sum(1 for r in entry.apply_result.results if r.action == "failed"),
            "failures": [
                {"card": r.card.display_name, "error": r.error}
                for r in entry.apply_result.results
                if r.action == "failed"
            ],
        }
    return result
