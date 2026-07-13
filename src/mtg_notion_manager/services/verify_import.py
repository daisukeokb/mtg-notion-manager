"""記事から抽出できるカード・relationが、Notion上の登録済み状態と完全一致するかを
検証する(verify-import)。読み取り専用(Notionへの書き込みは一切行わない)。

import-articleの取り込み前チェック(--dry-run)とは異なり、こちらは「既に取り込み済み
のはずのデッキ」が実際にその通りに登録されているかを検証する目的で使う。

既存のimport-article計画処理(build_article_import_plan)をそのまま再利用し、
verify固有の処理は以下に限定する:
- 統率者DBレコードが一意に存在するかの確認
- 統率者DBの実際の「採用カード」relationページID集合の取得
- 期待relation集合(記事から解決できた既存カードのpage_id集合)との比較
- 検証結果(DeckVerifyEntry/ArticleVerifyReport)の生成・レポート出力

Notion操作は database query / page retrieve / relation property read のみ。
create_card / apply_relation_update / execute_import_cards / execute_article_import
など書き込み系の処理へは一切到達しない。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

from mtg_notion_manager.notion.card_repository import (
    ENGLISH_NAME_PROPERTY,
    TITLE_PROPERTY,
    CardRepository,
    _plain_text,
)
from mtg_notion_manager.notion.client import NotionClient
from mtg_notion_manager.notion.writer import NotionWriter
from mtg_notion_manager.parsers.decklist import COMMANDER_DECK_SIZE
from mtg_notion_manager.services.import_article import (
    DeckArticleEntry,
    build_article_import_plan,
    overrides_used_from_decisions,
)

SCHEMA_VERSION = 1
COMMAND_NAME = "verify-import"
COMMANDER_CARDS_RELATION_PROPERTY = "採用カード"

VERIFICATION_VERIFIED = "verified"
VERIFICATION_MISMATCH = "mismatch"


@dataclass(frozen=True)
class DeckVerifyEntry:
    deck_name: str
    verification_status: str
    verification_errors: list[str]
    deck_page_id: str | None = None
    deck_page_url: str | None = None
    extracted_card_count: int | None = None
    unique_card_count: int | None = None
    existing_card_count: int | None = None
    new_card_count: int | None = None
    ambiguous_match_count: int | None = None
    error_count: int | None = None
    overrides_used: list[dict] = field(default_factory=list)
    expected_relation_page_ids: list[str] = field(default_factory=list)
    actual_relation_page_ids: list[str] = field(default_factory=list)
    missing_relation_page_ids: list[str] = field(default_factory=list)
    unexpected_relation_page_ids: list[str] = field(default_factory=list)
    missing_relation_cards: list[dict] = field(default_factory=list)
    unexpected_relation_cards: list[dict] = field(default_factory=list)
    resolution_method: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.verification_status == VERIFICATION_VERIFIED


@dataclass(frozen=True)
class ArticleVerifyReport:
    source_url: str
    all_deck_names: list[str]
    entries: list[DeckVerifyEntry]

    @property
    def verification_status(self) -> str:
        if not self.entries:
            return VERIFICATION_MISMATCH
        return (
            VERIFICATION_VERIFIED
            if all(entry.is_verified for entry in self.entries)
            else VERIFICATION_MISMATCH
        )

    @property
    def summary(self) -> dict:
        verified = sum(1 for entry in self.entries if entry.is_verified)
        return {
            "selected_deck_count": len(self.entries),
            "verified_deck_count": verified,
            "mismatch_deck_count": len(self.entries) - verified,
            "total_new_card_count": sum(entry.new_card_count or 0 for entry in self.entries),
            "total_ambiguous_match_count": sum(
                entry.ambiguous_match_count or 0 for entry in self.entries
            ),
            "total_error_count": sum(entry.error_count or 0 for entry in self.entries),
            "total_missing_relation_count": sum(
                len(entry.missing_relation_page_ids) for entry in self.entries
            ),
            "total_unexpected_relation_count": sum(
                len(entry.unexpected_relation_page_ids) for entry in self.entries
            ),
        }


def build_verify_import_plan(
    url: str,
    client: NotionClient,
    writer: NotionWriter,
    card_repo: CardRepository,
    include_deck_names: list[str] | None = None,
    deck_page_map_path: Path | None = None,
) -> ArticleVerifyReport:
    """記事から抽出できるカード・relationが、登録済みNotion状態と一致するか検証する。

    build_article_import_plan()(import-articleのdry-run計画処理)をそのまま再利用する。
    デッキページの解決(名前完全一致 or deck_page_map_pathによる明示的マッピング)は
    build_article_import_plan() 側の共通resolverが行い、ここではその結果
    (DeckArticleEntry.deck_page_id / resolution_method)をそのまま引き継ぐ
    (import-articleとverify-importが常に同じ解決結果を使うことを保証するため、
    ここで名前による再解決は行わない)。

    記事取得・Notion読取に失敗した場合は例外がそのまま呼び出し側へ伝播する
    (登録状態の差分ではなく実行エラーとして扱うため、ここでは捕捉しない)。
    """
    include_deck_names = include_deck_names or []
    article_plan = build_article_import_plan(
        url,
        writer,
        card_repo,
        include_deck_names=include_deck_names,
        allow_count_mismatch=True,
        deck_page_map_path=deck_page_map_path,
    )

    entries = [_verify_one_deck(entry, client, card_repo) for entry in article_plan.entries]

    return ArticleVerifyReport(
        source_url=article_plan.source_url,
        all_deck_names=article_plan.all_deck_names,
        entries=entries,
    )


def _verify_one_deck(
    entry: DeckArticleEntry,
    client: NotionClient,
    card_repo: CardRepository,
) -> DeckVerifyEntry:
    if entry.deck_page_id is None:
        return DeckVerifyEntry(
            deck_name=entry.deck_name,
            verification_status=VERIFICATION_MISMATCH,
            verification_errors=[
                entry.reason or "MTG統率者DBに一致するデッキレコードが見つかりません"
            ],
            resolution_method=entry.resolution_method,
        )

    if entry.cards_plan is None:
        return DeckVerifyEntry(
            deck_name=entry.deck_name,
            verification_status=VERIFICATION_MISMATCH,
            verification_errors=[entry.reason or "カード抽出・照合に失敗しました"],
            deck_page_id=entry.deck_page_id,
            deck_page_url=entry.deck_page_url,
            resolution_method=entry.resolution_method,
        )

    page = client.get_page(entry.deck_page_id)
    properties = page.get("properties", {})

    parsed = entry.cards_plan.parsed
    decisions = entry.cards_plan.decisions
    counts = entry.cards_plan.summary

    extracted_card_count = parsed.total_quantity
    unique_card_count = len(parsed.cards)
    existing_card_count = counts.get("relation_update", 0) + counts.get("unchanged", 0)
    new_card_count = counts.get("create", 0)
    ambiguous_match_count = counts.get("ambiguous", 0)
    error_count = counts.get("error", 0)

    errors: list[str] = []
    if extracted_card_count != COMMANDER_DECK_SIZE:
        errors.append(
            f"抽出枚数が{COMMANDER_DECK_SIZE}枚と一致しません(実際: {extracted_card_count}枚)"
        )
    if new_card_count:
        errors.append(f"新規カードが{new_card_count}件あります(カードDB未登録の可能性)")
    if ambiguous_match_count:
        errors.append(f"曖昧一致のカードが{ambiguous_match_count}件あります")
    if error_count:
        errors.append(f"照合エラーのカードが{error_count}件あります")

    expected_ids = sorted({d.existing.page_id for d in decisions if d.existing is not None})
    actual_ids = sorted(
        set(
            client.read_relation_ids(
                properties, entry.deck_page_id, COMMANDER_CARDS_RELATION_PROPERTY
            )
        )
    )
    missing_ids = sorted(set(expected_ids) - set(actual_ids))
    unexpected_ids = sorted(set(actual_ids) - set(expected_ids))

    if missing_ids:
        errors.append(f"統率者DBのrelationに不足があります({len(missing_ids)}件)")
    if unexpected_ids:
        errors.append(f"統率者DBのrelationに余分なページがあります({len(unexpected_ids)}件)")

    status = VERIFICATION_VERIFIED if not errors else VERIFICATION_MISMATCH

    return DeckVerifyEntry(
        deck_name=entry.deck_name,
        verification_status=status,
        verification_errors=errors,
        deck_page_id=entry.deck_page_id,
        deck_page_url=entry.deck_page_url,
        extracted_card_count=extracted_card_count,
        unique_card_count=unique_card_count,
        existing_card_count=existing_card_count,
        new_card_count=new_card_count,
        ambiguous_match_count=ambiguous_match_count,
        error_count=error_count,
        overrides_used=overrides_used_from_decisions(decisions),
        expected_relation_page_ids=expected_ids,
        actual_relation_page_ids=actual_ids,
        missing_relation_page_ids=missing_ids,
        unexpected_relation_page_ids=unexpected_ids,
        missing_relation_cards=[_relation_card_dict(pid, card_repo) for pid in missing_ids],
        unexpected_relation_cards=[_relation_card_dict(pid, card_repo) for pid in unexpected_ids],
        resolution_method=entry.resolution_method,
    )


def _relation_card_dict(page_id: str, card_repo: CardRepository) -> dict:
    """load()済みのカード索引からpage_idの表示名を引く(追加のAPI呼び出しなし)。

    英語名・日本語名は取得済みのNotionデータからそのまま表示するだけで、
    推測・機械翻訳は一切行わない(該当カードが索引にない場合はnullのまま返す)。
    """
    existing = card_repo.get_by_page_id(page_id)
    if existing is None:
        return {"page_id": page_id, "name_ja": None, "name_en": None}
    return {
        "page_id": page_id,
        "name_ja": _plain_text(existing.properties.get(TITLE_PROPERTY)),
        "name_en": _plain_text(existing.properties.get(ENGLISH_NAME_PROPERTY)),
    }


# --- レポート出力 -----------------------------------------------------------


@dataclass(frozen=True)
class VerifyReportPaths:
    json_path: Path


def write_verify_report(
    report: ArticleVerifyReport, output_dir: Path, timestamp: str | None = None
) -> VerifyReportPaths:
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"verify-import-{timestamp}.json"

    data = {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND_NAME,
        "article_url": report.source_url,
        "generated_at": datetime.datetime.now().isoformat(),
        "detected_deck_count": len(report.all_deck_names),
        "selected_deck_count": len(report.entries),
        "verification_status": report.verification_status,
        "summary": report.summary,
        "decks": [_deck_verify_to_dict(entry) for entry in report.entries],
    }
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return VerifyReportPaths(json_path=json_path)


def _deck_verify_to_dict(entry: DeckVerifyEntry) -> dict:
    return {
        "deck_name": entry.deck_name,
        "deck_page_id": entry.deck_page_id,
        "deck_page_url": entry.deck_page_url,
        "extracted_card_count": entry.extracted_card_count,
        "unique_card_count": entry.unique_card_count,
        "existing_card_count": entry.existing_card_count,
        "new_card_count": entry.new_card_count,
        "ambiguous_match_count": entry.ambiguous_match_count,
        "error_count": entry.error_count,
        "overrides_used": entry.overrides_used,
        "expected_relation_page_ids": entry.expected_relation_page_ids,
        "actual_relation_page_ids": entry.actual_relation_page_ids,
        "missing_relation_page_ids": entry.missing_relation_page_ids,
        "unexpected_relation_page_ids": entry.unexpected_relation_page_ids,
        "missing_relation_cards": entry.missing_relation_cards,
        "unexpected_relation_cards": entry.unexpected_relation_cards,
        "verification_status": entry.verification_status,
        "verification_errors": entry.verification_errors,
        "resolution_method": entry.resolution_method,
    }
