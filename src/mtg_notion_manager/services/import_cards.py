"""デッキのカード100枚をMTGカードDBへ取り込むオーケストレーション。

build_import_cards_plan() は読み取りのみ(Notionへの書き込みなし)で、
dry-run/apply共通のルートとして計画(ImportCardsPlan)を作る。
execute_import_cards() が実際の書き込みを行う。

冪等性の方針:
- 各カードの状態判定(create/relation_update/unchanged)は毎回Notionの現在値を
  読み直して決めるため、同じコマンドを再実行しても重複作成・重複リレーションは起きない。
- 1件の失敗が他のカードの処理を止めない(1件ずつ記録し、続行する)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mtg_notion_manager.exceptions import AmbiguousCardMatchError, NotionAPIError
from mtg_notion_manager.models import (
    BLOCKED_CREATION_ACTIONS,
    CardDecision,
    DeckCard,
    ParsedDeckList,
)
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.parsers.decklist import parse_decklist, validate_deck_count
from mtg_notion_manager.services.card_resolution import (
    ConfirmedCardMapping,
    UnverifiedNewCardError,
    resolve_new_card,
)

BLOCKING_ACTIONS = {"ambiguous", "error"} | BLOCKED_CREATION_ACTIONS


@dataclass(frozen=True)
class ImportCardsPlan:
    parsed: ParsedDeckList
    deck_page_id: str
    decisions: list[CardDecision]

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for decision in self.decisions:
            counts[decision.action] = counts.get(decision.action, 0) + 1
        return counts

    @property
    def has_blocking_issues(self) -> bool:
        return any(decision.action in BLOCKING_ACTIONS for decision in self.decisions)


@dataclass(frozen=True)
class CardApplyResult:
    card: DeckCard
    action: str  # "created" | "relation_updated" | "unchanged" | "failed"
    page_id: str | None = None
    page_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ImportCardsResult:
    results: list[CardApplyResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[CardApplyResult]:
        return [r for r in self.results if r.action != "failed"]

    @property
    def failed(self) -> list[CardApplyResult]:
        return [r for r in self.results if r.action == "failed"]


def build_import_cards_plan(
    url: str,
    deck_page_id: str,
    card_repo: CardRepository,
    deck_name: str | None = None,
    allow_count_mismatch: bool = False,
    html: str | None = None,
    confirmed_mapping: ConfirmedCardMapping | None = None,
) -> ImportCardsPlan:
    """URLからカード100枚を抽出し、カードDBと照合して計画を作る(書き込みなし)。

    html を渡した場合はダウンロードを省略して再利用する。

    confirmed_mapping を指定した場合、カードDB内に一致がなく記事からも日本語名が
    取得できないカード(英語記事由来の新規カード)は、このマッピングで人間が
    確認済みのカードに限り新規作成対象(action="create")として扱う。
    指定しない場合、そうしたカードは action が BLOCKED_CREATION_ACTIONS のいずれかになり、
    execute_import_cards() は一切書き込みを行わない
    (services.card_resolution.resolve_new_card() 参照)。
    """
    parsed = parse_decklist(url, deck_name, html=html)
    validate_deck_count(parsed, allow_mismatch=allow_count_mismatch)

    card_repo.load()

    decisions = [
        _decide(card, deck_page_id, card_repo, url, parsed.deck_name, confirmed_mapping)
        for card in parsed.cards
    ]

    return ImportCardsPlan(parsed=parsed, deck_page_id=deck_page_id, decisions=decisions)


def _decide(
    card: DeckCard,
    deck_page_id: str,
    card_repo: CardRepository,
    article_url: str,
    deck_name: str,
    confirmed_mapping: ConfirmedCardMapping | None,
) -> CardDecision:
    match = card_repo.find_match(card)

    if match.is_ambiguous:
        candidates = ", ".join(c.page_url for c in match.ambiguous_candidates)
        return CardDecision(
            card=card,
            action="ambiguous",
            detail=f"{len(match.ambiguous_candidates)}件の候補と一致: {candidates}",
        )

    if match.card is None:
        resolution = resolve_new_card(
            card,
            article_url=article_url,
            deck_name=deck_name,
            confirmed_mapping=confirmed_mapping,
        )
        if resolution.is_blocked:
            return CardDecision(
                card=card,
                action=resolution.resolution_status,
                detail=resolution.block_reason or "",
                resolution=resolution,
            )
        return CardDecision(card=card, action="create", resolution=resolution)

    existing = match.card
    current_deck_ids = card_repo.get_deck_relation_ids(existing)
    already_related = deck_page_id in current_deck_ids
    already_owned = card_repo.is_owned(existing)

    if already_related and already_owned:
        return CardDecision(
            card=card, action="unchanged", existing=existing, override_used=match.override_reason
        )

    detail_parts = []
    if not already_related:
        detail_parts.append("採用デッキ追加")
    if not already_owned:
        detail_parts.append("所持=trueへ更新")

    return CardDecision(
        card=card,
        action="relation_update",
        existing=existing,
        detail="・".join(detail_parts),
        owned_will_change=not already_owned,
        override_used=match.override_reason,
    )


def execute_import_cards(
    plan: ImportCardsPlan, card_repo: CardRepository, note: str = ""
) -> ImportCardsResult:
    """計画をNotionへ適用する。

    曖昧一致など未解決の判定が1件でもあれば、何も書き込まずに例外を送出する
    (100枚の途中で不完全な状態になることを避けるための事前ゲート)。
    """
    if plan.has_blocking_issues:
        blocking = [d for d in plan.decisions if d.action in BLOCKING_ACTIONS]
        details = "; ".join(f"{d.card.display_name}: {d.detail}" for d in blocking)
        raise AmbiguousCardMatchError(
            f"曖昧一致または未解決のカードが{len(blocking)}件あるため書き込みを中止しました: "
            f"{details}"
        )

    results: list[CardApplyResult] = []
    for decision in plan.decisions:
        results.append(_apply_one(decision, plan.deck_page_id, card_repo, note, plan.parsed))

    return ImportCardsResult(results=results)


def _apply_one(
    decision: CardDecision,
    deck_page_id: str,
    card_repo: CardRepository,
    note: str,
    parsed: ParsedDeckList,
) -> CardApplyResult:
    try:
        if decision.action == "create":
            # 書き込み境界の防御: resolution.verified_card が無い場合(例えば
            # CardDecisionが計画外で直接組み立てられた場合)でも、未検証のDeckCardを
            # そのままcreate_card()へ渡さず、必ずresolve_new_card()を通す。
            verified = decision.resolution.verified_card if decision.resolution else None
            if verified is None:
                resolution = resolve_new_card(
                    decision.card,
                    article_url=parsed.source_url,
                    deck_name=parsed.deck_name,
                    confirmed_mapping=None,
                )
                if resolution.verified_card is None:
                    raise UnverifiedNewCardError(
                        f"カード '{decision.card.display_name}' は日本語名が未確認のため"
                        " 新規作成できません(安全機構違反)。"
                    )
                verified = resolution.verified_card
            page = card_repo.create_card(verified, deck_page_id, note=note)
            return CardApplyResult(
                card=decision.card,
                action="created",
                page_id=page.get("id"),
                page_url=page.get("url"),
            )

        if decision.action == "relation_update":
            assert decision.existing is not None
            current_ids = card_repo.get_deck_relation_ids(decision.existing)
            page = card_repo.apply_relation_update(decision.existing, deck_page_id, current_ids)
            return CardApplyResult(
                card=decision.card,
                action="relation_updated",
                page_id=page.get("id", decision.existing.page_id),
                page_url=page.get("url", decision.existing.page_url),
            )

        # unchanged: Notionへは何も送らない
        return CardApplyResult(
            card=decision.card,
            action="unchanged",
            page_id=decision.existing.page_id if decision.existing else None,
            page_url=decision.existing.page_url if decision.existing else None,
        )
    except NotionAPIError as exc:
        return CardApplyResult(card=decision.card, action="failed", error=str(exc))
