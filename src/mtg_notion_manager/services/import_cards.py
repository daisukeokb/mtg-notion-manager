"""デッキのカード100枚をMTGカードDBへ取り込むオーケストレーション。

build_import_cards_plan() は読み取りのみ(Notionへの書き込みなし)で、
dry-run/apply共通のルートとして計画(ImportCardsPlan)を作る。
execute_import_cards() が実際の書き込みを行う。

冪等性の方針:
- 各カードの状態判定(create/relation_update/unchanged)は毎回Notionの現在値を
  読み直して決めるため、同じコマンドを再実行しても重複作成・重複リレーションは起きない。
- 1件の失敗が他のカードの処理を止めない(1件ずつ記録し、続行する)。
- 既存カードへのリレーション追記は、計画時のスナップショットではなく書き込み直前に
  Notionから読み直した現在値へ追記する(採用デッキはPATCHで全件置換されるため)。
- 1回の実行で複数デッキを適用する場合(import-article)、RunWriteState で
  この実行中に作成したカードを追跡し、後続デッキの同一カードの create は
  作成済みページへの relation_update に読み替える(同一カードの重複作成防止)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from mtg_notion_manager.exceptions import AmbiguousCardMatchError, NotionAPIError
from mtg_notion_manager.models import (
    BLOCKED_CREATION_ACTIONS,
    CardDecision,
    DeckCard,
    ExistingCard,
    ParsedDeckList,
    VerifiedNewCard,
)
from mtg_notion_manager.notion.card_repository import CardRepository
from mtg_notion_manager.notion.client import AMBIGUOUS_WRITE_STATUS_CODES
from mtg_notion_manager.parsers.card_names import normalize_card_name
from mtg_notion_manager.parsers.decklist import parse_decklist, validate_deck_count
from mtg_notion_manager.services.card_resolution import (
    ConfirmedCardMapping,
    UnverifiedNewCardError,
    resolve_new_card,
)

BLOCKING_ACTIONS = {"ambiguous", "error"} | BLOCKED_CREATION_ACTIONS


class FailedWriteOperation:
    """action="failed"のCardApplyResultで、実際に試行されたNotion書き込み種別。

    decision.actionから決定論的に決まる(_apply_one()の呼び出し側は
    どちらを試みたか既に知っているため、NotionAPIError自体から
    推測する必要はない)。
    """

    CREATE = "create"
    RELATION_UPDATE = "relation_update"


class WriteCompletion:
    """action="failed"のCardApplyResultで、書き込みの完了状態についてわかっている事実。

    NotionAPIError.__cause__ の型だけから判定する(str(exc)のメッセージ文字列は
    一切見ない)。サーバーが明示的なHTTPエラー応答を返した場合(NotionClientの
    再試行方針上、リトライ対象外か、リトライを使い切った後)だけをKNOWN_FAILEDとし、
    それ以外(タイムアウト・その他の接続エラー・原因不明)はすべて安全側の
    UNKNOWNへ倒す。update_page()はタイムアウト時に自動リトライされるが、
    「べき等なので再試行対象」であることと「完了状態が確定している」ことは
    同義ではないため、リトライを使い切った後のタイムアウトも同様にUNKNOWNとする。

    例外: 新規カード作成(POST /pages、べき等でない)では、HTTPエラー応答のうち
    500/502/503/504(notion.client.AMBIGUOUS_WRITE_STATUS_CODES)はサーバー側で
    実際には作成が完了していた可能性を排除できないため、KNOWN_FAILEDではなく
    UNKNOWNとする(_create_completion_from_notion_api_error参照)。
    """

    KNOWN_FAILED = "known_failed"
    UNKNOWN = "unknown"


def _completion_from_notion_api_error(exc: NotionAPIError) -> str:
    if isinstance(exc.__cause__, httpx.HTTPStatusError):
        return WriteCompletion.KNOWN_FAILED
    return WriteCompletion.UNKNOWN


def _create_completion_from_notion_api_error(exc: NotionAPIError) -> str:
    """新規カード作成(create_card())失敗時の完了状態。

    HTTPエラー応答のstatus code(構造化データ)だけから判定する(メッセージ文字列は
    見ない)。500/502/503/504は作成が実際には完了していた可能性があるためUNKNOWN、
    それ以外のHTTPエラー応答(400・リトライを使い切った429等)はKNOWN_FAILED、
    HTTPエラー応答以外(タイムアウト・接続エラー等)は従来通りUNKNOWN。
    relation_update(べき等なPATCH)は対象外で、_completion_from_notion_api_error()
    の既存分類のまま。
    """
    cause = exc.__cause__
    if (
        isinstance(cause, httpx.HTTPStatusError)
        and cause.response.status_code not in AMBIGUOUS_WRITE_STATUS_CODES
    ):
        return WriteCompletion.KNOWN_FAILED
    return WriteCompletion.UNKNOWN


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
    #: action=="failed"のときのみ設定される(FailedWriteOperationの値)。
    #: human向けerror文字列と異なり、将来のError Contract v2 adapterが
    #: メッセージ解析なしで機械可読に参照できるようにするための補助情報。
    failed_operation: str | None = None
    #: action=="failed"のときのみ設定される(WriteCompletionの値)。
    failed_completion: str | None = None


@dataclass(frozen=True)
class ImportCardsResult:
    results: list[CardApplyResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[CardApplyResult]:
        return [r for r in self.results if r.action != "failed"]

    @property
    def failed(self) -> list[CardApplyResult]:
        return [r for r in self.results if r.action == "failed"]


@dataclass
class RunWriteState:
    """1回の実行(import-article全体など、複数デッキにまたがる適用)で
    作成したカードページを追跡する。

    計画(build_import_cards_plan)はデッキごとに実行前のスナップショットから作られるため、
    DBに無いカードを複数デッキが使う場合、各デッキの計画にそれぞれ create が入る。
    execute_import_cards() はこの状態を参照し、既に作成済みのカードは新規作成せず
    作成済みページへの relation_update に読み替える。

    同一カードの判定はまず stable_key、次に正規化英語名→正規化日本語名
    (CardRepository.find_match() と同じ優先順位。次回実行時の照合結果と一致させるため)。
    作成結果が不明(UNKNOWN)に終わったカードは、後続デッキでも作成を試みない
    (サーバー側で作成済みの可能性があり、再作成すると重複しうるため)。
    """

    created_by_key: dict[str, tuple[str, str]] = field(default_factory=dict)
    created_by_name_en: dict[str, tuple[str, str]] = field(default_factory=dict)
    created_by_name_ja: dict[str, tuple[str, str]] = field(default_factory=dict)
    unknown_create_keys: set[str] = field(default_factory=set)
    unknown_create_names_en: set[str] = field(default_factory=set)
    unknown_create_names_ja: set[str] = field(default_factory=set)

    def find_created(self, card: VerifiedNewCard) -> tuple[str, str] | None:
        """作成済みなら (page_id, page_url) を返す。"""
        if card.stable_key in self.created_by_key:
            return self.created_by_key[card.stable_key]
        if card.name_en and normalize_card_name(card.name_en) in self.created_by_name_en:
            return self.created_by_name_en[normalize_card_name(card.name_en)]
        if normalize_card_name(card.name_ja) in self.created_by_name_ja:
            return self.created_by_name_ja[normalize_card_name(card.name_ja)]
        return None

    def is_create_unknown(self, card: VerifiedNewCard) -> bool:
        if card.stable_key in self.unknown_create_keys:
            return True
        if card.name_en and normalize_card_name(card.name_en) in self.unknown_create_names_en:
            return True
        return normalize_card_name(card.name_ja) in self.unknown_create_names_ja

    def record_created(self, card: VerifiedNewCard, page_id: str, page_url: str) -> None:
        entry = (page_id, page_url)
        self.created_by_key[card.stable_key] = entry
        if card.name_en:
            self.created_by_name_en.setdefault(normalize_card_name(card.name_en), entry)
        self.created_by_name_ja.setdefault(normalize_card_name(card.name_ja), entry)

    def record_create_unknown(self, card: VerifiedNewCard) -> None:
        self.unknown_create_keys.add(card.stable_key)
        if card.name_en:
            self.unknown_create_names_en.add(normalize_card_name(card.name_en))
        self.unknown_create_names_ja.add(normalize_card_name(card.name_ja))


class PartialImportAbortedError(UnverifiedNewCardError):
    """UnverifiedNewCardErrorによるバッチ中断時、それより前に完了していた
    CardApplyResult(成功・NotionAPIError由来のfailedを含む)を保持して運ぶ。

    UnverifiedNewCardErrorのsubclassのため、isinstance(exc, UnverifiedNewCardError)
    は引き続きTrueになり、既存のMtgNotionManagerError系catchとの互換性を壊さない。
    str(self)は元の例外メッセージと同一(コンストラクタへ渡した message そのまま)。

    中断semantics自体は変更しない(このカードおよびそれ以降のカードへは
    引き続き一切書き込みを行わない)。completed_resultsは、中断より前に
    _apply_one()が正常returnした結果だけを、処理順のimmutable snapshotとして持つ。
    """

    def __init__(self, message: str, *, completed_results: tuple[CardApplyResult, ...]) -> None:
        super().__init__(message)
        self.completed_results = completed_results


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
    plan: ImportCardsPlan,
    card_repo: CardRepository,
    note: str = "",
    run_state: RunWriteState | None = None,
) -> ImportCardsResult:
    """計画をNotionへ適用する。

    run_state は複数デッキを1回の実行で適用する場合(import-article)に、全デッキで
    同じインスタンスを渡す(RunWriteState参照)。省略時はこの呼び出し専用の状態を使う。

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

    if run_state is None:
        run_state = RunWriteState()

    results: list[CardApplyResult] = []
    for decision in plan.decisions:
        try:
            result = _apply_one(
                decision, plan.deck_page_id, card_repo, note, plan.parsed, run_state
            )
        except UnverifiedNewCardError as exc:
            # 中断semantics自体は変更しない(このカード・以降のカードへは書き込まない)。
            # それより前に完了していた結果だけを、外部から回収可能なsnapshotとして運ぶ。
            raise PartialImportAbortedError(
                str(exc), completed_results=tuple(results)
            ) from exc
        results.append(result)

    return ImportCardsResult(results=results)


def _apply_one(
    decision: CardDecision,
    deck_page_id: str,
    card_repo: CardRepository,
    note: str,
    parsed: ParsedDeckList,
    run_state: RunWriteState,
) -> CardApplyResult:
    #: except節で、実際にどちらの書き込みを試みたかを判定するために使う
    #: (create計画でも、作成済みカードへのrelation_updateに読み替える場合がある)。
    attempted = (
        FailedWriteOperation.CREATE
        if decision.action == "create"
        else FailedWriteOperation.RELATION_UPDATE
    )
    verified: VerifiedNewCard | None = None
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

            created = run_state.find_created(verified)
            if created is not None:
                # 同一実行内の先行デッキで作成済み: 重複作成せず、作成済みページへ
                # このデッキのリレーションを追記する。
                attempted = FailedWriteOperation.RELATION_UPDATE
                return _apply_relation_update_fresh(
                    decision, created[0], created[1], deck_page_id, card_repo
                )
            if run_state.is_create_unknown(verified):
                # 先行デッキでの作成結果が不明。作成済みの可能性があるため、
                # 再作成(重複の恐れ)もリレーション追記(対象不明)も行わない。
                return CardApplyResult(
                    card=decision.card,
                    action="failed",
                    error=(
                        f"カード '{decision.card.display_name}' は同一実行内の先行デッキで"
                        "作成結果が不明になったため、重複作成を避けて作成を見送りました"
                        "(Notion側で作成済みか確認のうえ再実行してください)。"
                    ),
                    failed_operation=FailedWriteOperation.CREATE,
                    failed_completion=WriteCompletion.KNOWN_FAILED,
                )

            page = card_repo.create_card(verified, deck_page_id, note=note)
            page_id = page.get("id")
            page_url = page.get("url")
            if page_id:
                run_state.record_created(verified, page_id, page_url or "")
            else:
                run_state.record_create_unknown(verified)
            return CardApplyResult(
                card=decision.card,
                action="created",
                page_id=page_id,
                page_url=page_url,
            )

        if decision.action == "relation_update":
            assert decision.existing is not None
            return _apply_relation_update_fresh(
                decision,
                decision.existing.page_id,
                decision.existing.page_url,
                deck_page_id,
                card_repo,
            )

        # unchanged: Notionへは何も送らない
        return CardApplyResult(
            card=decision.card,
            action="unchanged",
            page_id=decision.existing.page_id if decision.existing else None,
            page_url=decision.existing.page_url if decision.existing else None,
        )
    except NotionAPIError as exc:
        # create分岐でNotionへ送るのはcreate_card()のPOST /pages 1回だけのため、
        # ここに来たCREATEの失敗は必ずそのPOSTの失敗である。べき等でないcreateに限り、
        # 曖昧な5xxをUNKNOWNとして扱う専用の分類を使う(relation_updateは従来のまま)。
        completion = (
            _create_completion_from_notion_api_error(exc)
            if attempted == FailedWriteOperation.CREATE
            else _completion_from_notion_api_error(exc)
        )
        if (
            attempted == FailedWriteOperation.CREATE
            and verified is not None
            and completion == WriteCompletion.UNKNOWN
        ):
            run_state.record_create_unknown(verified)
        return CardApplyResult(
            card=decision.card,
            action="failed",
            error=str(exc),
            failed_operation=attempted,
            failed_completion=completion,
        )


def _apply_relation_update_fresh(
    decision: CardDecision,
    page_id: str,
    page_url: str,
    deck_page_id: str,
    card_repo: CardRepository,
) -> CardApplyResult:
    """カードページの現在値を読み直し、その採用デッキへこのデッキを追記する。

    計画時のスナップショット(decision.existing.properties)は使わない。
    同一実行内の先行デッキが追加したリレーションを含まず、そのままPATCHすると
    上書きで失われるため。読み直した結果、既に追記済み・所持済みであれば書き込まない。
    """
    current: ExistingCard = card_repo.refresh_card(page_id)
    current_ids = card_repo.get_deck_relation_ids(current)
    if deck_page_id in current_ids and card_repo.is_owned(current):
        return CardApplyResult(
            card=decision.card,
            action="unchanged",
            page_id=current.page_id,
            page_url=current.page_url or page_url,
        )
    page = card_repo.apply_relation_update(current, deck_page_id, current_ids)
    return CardApplyResult(
        card=decision.card,
        action="relation_updated",
        page_id=page.get("id", current.page_id),
        page_url=page.get("url", current.page_url or page_url),
    )
