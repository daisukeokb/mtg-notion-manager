"""import(デッキ新規作成)の書き込み失敗を Error Contract v2 の MutationSummary へ
変換する adapter(Phase 3E-R1)。

責務の境界:
- サービス層(services/import_deck.py)は「作成の完了状態についてわかっている事実」
  (DeckCreateCompletion: KNOWN_FAILED/UNKNOWN)だけを判定する。
- このモジュールはその事実を、公開contractの語彙(MutationOperation/MutationSummary/
  RecoveryAction、mutation_contract.py)へ変換する、import専用のadapter。

importの書き込みは確認後の create_page() 1回だけであり、MutationSummaryは論理操作
単位(attempted=1)で数える。HTTPの試行回数(429のリトライ等)は数えない。
成功時はError JSONを出力しないため、ここでは失敗(KNOWN_FAILED/UNKNOWN)だけを扱う。
"""

from __future__ import annotations

from mtg_notion_manager.mutation_contract import (
    MutationOperation,
    MutationSummary,
    RecoveryAction,
)
from mtg_notion_manager.services.import_deck import DeckCreateCompletion

_CREATE_ACTION = "create"


def build_deck_create_mutation_summary(deck_name: str, completion: str) -> MutationSummary:
    """失敗した1回のデッキ作成についてのMutationSummaryを構築する。

    - KNOWN_FAILED: attempted=1/failed=1、MUTATION_FAILED/MANUAL_REVIEW_REQUIRED
    - UNKNOWN: attempted=1/unknown=1、MUTATION_STATE_UNKNOWN/RECONCILE_BEFORE_RETRY

    RETRY_ALLOWEDは出力しない(作成はべき等でなく、rerun-safetyが証明されていない)。
    想定外のcompletion値は推測で穴埋めせずValueErrorでfail closedする。
    """
    if completion == DeckCreateCompletion.KNOWN_FAILED:
        return MutationSummary(
            attempted=1,
            succeeded=0,
            failed=1,
            unknown=0,
            recovery_action=RecoveryAction.MANUAL_REVIEW_REQUIRED,
            operations=(MutationOperation(key=deck_name, action=_CREATE_ACTION, state="failed"),),
        )
    if completion == DeckCreateCompletion.UNKNOWN:
        return MutationSummary(
            attempted=1,
            succeeded=0,
            failed=0,
            unknown=1,
            recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
            operations=(
                MutationOperation(key=deck_name, action=_CREATE_ACTION, state="unknown"),
            ),
        )
    raise ValueError(f"Unknown DeckCreateCompletion value: {completion!r} (deck={deck_name!r}).")
