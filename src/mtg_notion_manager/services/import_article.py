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
import re
from dataclasses import dataclass
from pathlib import Path

from mtg_notion_manager.exceptions import MtgNotionManagerError
from mtg_notion_manager.fetchers import get_fetcher
from mtg_notion_manager.fetchers.base import download
from mtg_notion_manager.models import CardDecision
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.services.card_resolution import (
    ConfirmedCardMapping,
    PendingCardManifest,
    build_pending_manifest,
    load_confirmed_card_mapping,
    summarize_decisions,
)
from mtg_notion_manager.services.deck_page_mapping import (
    DeckPageMapping,
    load_deck_page_mapping,
    resolve_deck_page,
)
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
    resolution_method: str | None = None


@dataclass(frozen=True)
class ArticleImportPlan:
    source_url: str
    all_deck_names: list[str]
    excluded_deck_names: list[str]
    entries: list[DeckArticleEntry]
    pending_manifest: PendingCardManifest | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts = {STATUS_READY: 0, STATUS_NEEDS_REVIEW: 0, STATUS_ERROR: 0}
        for entry in self.entries:
            counts[entry.status] += 1
        return counts

    @property
    def is_fully_applicable(self) -> bool:
        """今回の対象範囲(全デッキ・全カード)のplanが全件成功しているか。

        1件でも要確認・エラー・identity conflictがあれば、execute_article_import()は
        対象範囲のどのデッキへも一切書き込みを行わない(デッキ単位でplanとwriteを
        交互に行わないための全体ゲート)。
        """
        if not self.entries:
            return False
        if any(entry.status != STATUS_READY for entry in self.entries):
            return False
        if self.pending_manifest is not None and self.pending_manifest.conflicted_stable_keys:
            return False
        return True


def build_article_import_plan(
    url: str,
    writer: NotionWriter,
    card_repo: CardRepository,
    exclude_deck_names: list[str] | None = None,
    include_deck_names: list[str] | None = None,
    allow_count_mismatch: bool = False,
    deck_page_map_path: Path | None = None,
    confirmed_card_map_path: Path | None = None,
) -> ArticleImportPlan:
    """記事HTMLを1回だけ取得し、記事内の全デッキを解析・検証する(Notionへの書き込みなし)。

    include_deck_names を指定した場合、そのデッキ名だけを対象にする
    (exclude_deck_names と併用可能。両方指定された場合は両方の条件を満たす必要がある)。

    deck_page_map_path を指定した場合、記事側デッキ名から既存Notionページへの
    明示的な対応(config/deck_page_mapping.example.json 参照)を読み込み・検証し、
    名前完全一致より優先して使う(record_page_mapping.resolve_deck_page() 参照)。
    指定しない場合は従来どおり名前完全一致のみで解決する。

    confirmed_card_map_path を指定した場合、記事から日本語名が取得できない新規カード
    (英語記事由来)について、人間確認済みマッピング(config/confirmed_card_mapping.example.json
    参照)で確認済みのカードのみ新規作成対象として扱う。指定しない場合、そうした
    カードは全て確認待ち(blocked_missing_japanese_name)として新規作成をブロックする。
    """
    exclude_deck_names = exclude_deck_names or []
    include_deck_names = include_deck_names or []
    html = download(url)
    fetcher = get_fetcher(url)
    all_deck_names = fetcher.list_deck_names(html, url)

    mapping: DeckPageMapping | None = None
    if deck_page_map_path is not None:
        mapping = load_deck_page_mapping(deck_page_map_path, url, all_deck_names)

    confirmed_mapping: ConfirmedCardMapping | None = None
    if confirmed_card_map_path is not None:
        confirmed_mapping = load_confirmed_card_mapping(confirmed_card_map_path, url)

    target_names = [name for name in all_deck_names if name not in exclude_deck_names]
    if include_deck_names:
        target_names = [name for name in target_names if name in include_deck_names]

    card_repo.load()

    entries = [
        _build_one_deck_entry(
            url, html, name, writer, card_repo, allow_count_mismatch, mapping, confirmed_mapping
        )
        for name in target_names
    ]

    all_resolutions = [
        decision.resolution
        for entry in entries
        if entry.cards_plan is not None
        for decision in entry.cards_plan.decisions
        if decision.resolution is not None
    ]
    pending_manifest = build_pending_manifest(url, all_resolutions) if all_resolutions else None

    return ArticleImportPlan(
        source_url=url,
        all_deck_names=all_deck_names,
        excluded_deck_names=[n for n in exclude_deck_names if n in all_deck_names],
        entries=entries,
        pending_manifest=pending_manifest,
    )


def _build_one_deck_entry(
    url: str,
    html: str,
    deck_name: str,
    writer: NotionWriter,
    card_repo: CardRepository,
    allow_count_mismatch: bool,
    mapping: DeckPageMapping | None,
    confirmed_mapping: ConfirmedCardMapping | None,
) -> DeckArticleEntry:
    resolution = resolve_deck_page(deck_name, writer, mapping)
    if not resolution.resolved:
        reason = resolution.error or (
            "MTG統率者DBに一致するデッキが見つかりません"
            "(先に import コマンドで登録してください)"
        )
        return DeckArticleEntry(
            deck_name=deck_name,
            status=STATUS_NEEDS_REVIEW,
            reason=reason,
            resolution_method=resolution.resolution_method,
        )

    existing_deck = resolution.existing_deck
    assert existing_deck is not None

    try:
        cards_plan = build_import_cards_plan(
            url,
            existing_deck.page_id,
            card_repo,
            deck_name=deck_name,
            allow_count_mismatch=allow_count_mismatch,
            html=html,
            confirmed_mapping=confirmed_mapping,
        )
    except MtgNotionManagerError as exc:
        return DeckArticleEntry(
            deck_name=deck_name,
            status=STATUS_ERROR,
            reason=str(exc),
            deck_page_id=existing_deck.page_id,
            deck_page_url=existing_deck.page_url,
            resolution_method=resolution.resolution_method,
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
            resolution_method=resolution.resolution_method,
        )

    return DeckArticleEntry(
        deck_name=deck_name,
        status=STATUS_READY,
        deck_page_id=existing_deck.page_id,
        deck_page_url=existing_deck.page_url,
        cards_plan=cards_plan,
        resolution_method=resolution.resolution_method,
    )


def execute_article_import(
    plan: ArticleImportPlan, card_repo: CardRepository, note: str = ""
) -> ArticleImportPlan:
    """今回の対象範囲(全デッキ・全カード)のplanが全件成功している場合のみ適用する。

    plan.is_fully_applicable が False の場合(1件でも要確認・エラー・
    identity conflictがある場合)は、対象範囲のどのデッキへも一切書き込みを行わず、
    planをそのまま返す(カードページ作成・更新、relation追加・削除、
    統率者ページ更新のいずれも0件)。デッキ単位でplanとwriteを交互に行わない
    (安全不変条件: 全件のpreflightが成功するまでNotion書き込みを1件も開始しない)。

    全件成功している場合でも、デッキごとに独立して例外を捕捉する
    (Notion API呼び出し自体の失敗は、他デッキの結果を失わせない)。
    """
    if not plan.is_fully_applicable:
        return plan

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
                    resolution_method=entry.resolution_method,
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
                resolution_method=entry.resolution_method,
            )
        )

    return ArticleImportPlan(
        source_url=plan.source_url,
        all_deck_names=plan.all_deck_names,
        excluded_deck_names=plan.excluded_deck_names,
        entries=new_entries,
        pending_manifest=plan.pending_manifest,
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
        "resolution_method": entry.resolution_method,
    }
    if entry.cards_plan is not None:
        parsed = entry.cards_plan.parsed
        counts = entry.cards_plan.summary
        resolution_summary = summarize_decisions(entry.cards_plan.decisions)
        result["extracted_quantity"] = parsed.total_quantity
        result["unique_card_count"] = len(parsed.cards)
        result["existing_card_count"] = counts.get("relation_update", 0) + counts.get(
            "unchanged", 0
        )
        result["new_card_count"] = resolution_summary.new_card_count
        result["relation_added_count"] = counts.get("relation_update", 0)
        result["unchanged_count"] = counts.get("unchanged", 0)
        result["ambiguous_count"] = counts.get("ambiguous", 0)
        result["error_count"] = counts.get("error", 0)
        result["creatable_from_article_japanese_name_count"] = (
            resolution_summary.creatable_from_article_japanese_name_count
        )
        result["creatable_from_human_confirmation_count"] = (
            resolution_summary.creatable_from_human_confirmation_count
        )
        result["pending_confirmation_count"] = resolution_summary.pending_confirmation_count
        result["identity_conflict_count"] = resolution_summary.identity_conflict_count
        result["config_error_count"] = resolution_summary.config_error_count
        result["is_fully_applicable"] = resolution_summary.is_fully_applicable
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


# --- デッキ単位のログ出力(reports/article-deck-import-*) ---------------------
#
# セット名・記事に依存しない汎用処理(任意の統率者デッキ記事で利用可能)。
# 秘密情報(APIキー等)はここでは一切扱わないため、出力にも含まれない。


_SLUG_UNSAFE_RE = re.compile(r"[\\/:*?\"<>|\s]+")


def slugify_deck_name(name: str) -> str:
    """ファイル名として安全な形に整形する(日本語自体は変換せずそのまま使う)。"""
    slug = _SLUG_UNSAFE_RE.sub("-", name).strip("-")
    return slug or "deck"


def overrides_used_from_decisions(decisions: list[CardDecision]) -> list[dict]:
    """カード照合オーバーライドが適用されたカードの一覧を返す(ログ・レポート共通)。"""
    return [
        {"card": d.card.display_name, "reason": d.override_used}
        for d in decisions
        if d.override_used
    ]


def write_article_deck_logs(
    plan: ArticleImportPlan, output_dir: Path, timestamp: str | None = None
) -> list[Path]:
    """デッキごとに reports/article-deck-import-{timestamp}-{deck-slug}.json を出力する。

    Strixhaven等の特定セットに限らず、任意の統率者デッキ記事に利用できる。
    """
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for entry in plan.entries:
        slug = slugify_deck_name(entry.deck_name)
        path = output_dir / f"article-deck-import-{timestamp}-{slug}.json"
        log = _deck_log_dict(entry)
        path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(path)
    return paths


def _deck_log_dict(entry: DeckArticleEntry) -> dict:
    result: dict = {
        "deck_name": entry.deck_name,
        "status": entry.status,
        "reason": entry.reason,
        "deck_page_id": entry.deck_page_id,
        "delete_count": 0,
    }

    if entry.cards_plan is None:
        result.update(
            {
                "extracted_quantity": None,
                "unique_card_count": None,
                "new_card_count": None,
                "relation_added_count": None,
                "owned_updated_count": None,
                "unchanged_count": None,
                "ambiguous_count": None,
                "error_count": None,
                "overrides_used": [],
                "api_update_count": 0,
            }
        )
        return result

    parsed = entry.cards_plan.parsed
    decisions = entry.cards_plan.decisions

    result["extracted_quantity"] = parsed.total_quantity
    result["unique_card_count"] = len(parsed.cards)
    result["overrides_used"] = overrides_used_from_decisions(decisions)
    result["ambiguous_count"] = sum(1 for d in decisions if d.action == "ambiguous")
    result["owned_updated_count"] = sum(1 for d in decisions if d.owned_will_change)

    if entry.apply_result is not None:
        results = entry.apply_result.results
        result["new_card_count"] = sum(1 for r in results if r.action == "created")
        result["relation_added_count"] = sum(1 for r in results if r.action == "relation_updated")
        result["unchanged_count"] = sum(1 for r in results if r.action == "unchanged")
        result["error_count"] = sum(1 for r in results if r.action == "failed")
        result["api_update_count"] = sum(
            1 for r in results if r.action in ("created", "relation_updated")
        )
    else:
        counts = entry.cards_plan.summary
        result["new_card_count"] = summarize_decisions(decisions).new_card_count
        result["relation_added_count"] = counts.get("relation_update", 0)
        result["unchanged_count"] = counts.get("unchanged", 0)
        result["error_count"] = counts.get("error", 0)
        result["api_update_count"] = 0

    return result
