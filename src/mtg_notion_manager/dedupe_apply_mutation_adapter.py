"""dedupe-familyの各apply系commandの実行結果を Error Contract v2 の MutationSummary
へ変換する共有adapter。

対象commandは、dedupe_cards.pyの共有write engine(execute_dedupe_plan()/
_apply_one_group())をそのまま使い、その結果を自分自身のGroupApplyOutcome
(command固有のdataclass)へ変換するという同じ構造を持つ:

- apply-price-link-dedupe(services/apply_price_link_dedupe.py, Phase 2K)
- apply-dedupe-plan(services/apply_dedupe_plan.py, 本Work Unitで追加)

このモジュールは、その2つ(将来的にはdedupe-cards自身も)のGroupApplyOutcomeが
共通して持つべき最小限のfieldだけへ依存する``DedupeGroupOutcome`` Protocolを
使い、command固有dataclassをimportせず疑似的な汎用mutationフレームワークにも
拡張しない――あくまで「同じ形のcommand固有dataclassをどのcommandからでも渡せる」
という最小限の一般化だけを行う(count/state導出/recovery_action決定ロジックの
複製は一切作らない)。

重要な事実(このモジュールの実装が前提とする):
GroupApplyOutcome(status=="failed")には、実際には複数の起源がありうる。
1. execute_dedupe_plan()が実際にNotionへの書き込みを試みて失敗した
   (failed_operation/failed_completionが両方設定される――
   dedupe_cards.GroupApplyResultの__post_init__不変条件により、
   error!=Noneならこの2つは必ず両方設定されている)。
2. command固有の事前チェック(例: apply-price-link-dedupeの
   --scope manual代表未指定、build_dedupe_plan()のgroup_errors)による、
   Notion書き込みに到達する前の失敗(Notion書き込みは一切試みられていない)。

2.はfailed_operation/failed_completionが両方Noneのまま到達する、正当な
「0 mutation」ケースである(RESULT FIDELITY VIOLATIONではない)。一方だけが
設定されている状態(片方だけNone)は、上記の不変条件から見て構造的に到達
しないはずだが、そのような矛盾したデータが渡された場合は
ResultFidelityViolationErrorとしてfail closedし、誤ったmutation JSONを
生成しない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from mtg_notion_manager.mutation_contract import MutationOperation, MutationSummary, RecoveryAction
from mtg_notion_manager.services.dedupe_cards import FailedGroupOperation, GroupWriteCompletion

_STATUS_APPLIED = "applied"
_STATUS_FAILED = "failed"

_MUTATION_OPERATION_ACTION = {
    FailedGroupOperation.REPRESENTATIVE_UPDATE: "representative_update",
    FailedGroupOperation.MARK_MERGED: "mark_merged",
}


class DedupeGroupOutcome(Protocol):
    """このadapterが読み取る最小限のfieldだけを表す構造的contract。

    apply_price_link_dedupe.GroupApplyOutcome / apply_dedupe_plan.GroupApplyOutcome
    はどちらも(構造的に)このProtocolを満たす――共有write engineのfidelity fix
    (Phase 2I/2J)が両方のcommand固有dataclassへ伝播されているため。

    読み取り専用property(``@property``)として宣言する――両方のcommand固有
    dataclassが``frozen=True``であり、mypyはfrozen dataclassの属性を
    read-onlyとみなすため、Protocol側も書き込み可能なplain属性ではなく
    read-only属性として宣言しないと構造的に一致しない。
    """

    @property
    def card_name(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def merged_page_ids(self) -> list[str]: ...

    @property
    def failed_operation(self) -> str | None: ...

    @property
    def failed_completion(self) -> str | None: ...


class ResultFidelityViolationError(RuntimeError):
    """GroupApplyOutcome(status=="failed")に、failed_operationと
    failed_completionのうち片方だけが設定されていた(両方Noneまたは両方
    非Noneのはずが崩れている)。

    これはユーザー入力やNotion APIの問題ではなく、このコードベース自身の
    データ整合性が壊れていることを示す内部プログラミングエラーである
    (MtgNotionManagerError系ではない――CLIの既存except節へ意図せず吸収されない
    ようにするため)。メッセージ文字列からの推測で穴埋めせず、fail closedする。
    """


def build_dedupe_apply_mutation_summary(
    outcomes: Sequence[DedupeGroupOutcome],
    *,
    extra_succeeded: int = 0,
    extra_failed: int = 0,
    extra_unknown: int = 0,
    extra_operations: Sequence[MutationOperation] = (),
) -> MutationSummary:
    """GroupApplyOutcomeの列から、実際にNotion書き込みを試みた件数だけを数えた
    MutationSummaryを構築する。

    STATUS_PLANNED/STATUS_SKIPPED_STALE/STATUS_SKIPPED_NOT_DUPLICATEに相当する
    (status!="applied" and status!="failed"の)結果は0 mutation(Notionへ一切
    書き込みを試みていない)として数えない。status=="failed"のうち、
    failed_operation/failed_completionが両方Noneのものも同様(Notion書き込みに
    到達する前の失敗)。

    recovery_actionはunknown/failedの件数だけから導出する(RETRY_ALLOWEDは
    rerun-safetyが別途証明されるまでemitしない)。この導出方法はmutation_contract.py
    のstate導出(unknown優先)と同じ優先順位を使うため、生成するMutationSummaryが
    _ALLOWED_RECOVERY_BY_STATEの不変条件に違反することはない。

    ``extra_*`` (keyword-only) は、dedupe group outcomesとは別起源のwrite attempt
    (Phase 2P: dedupe-cardsのschema mutation、1回のPATCH)を同一のaggregate
    MutationSummaryへ合算するための拡張。呼び出し元(dedupe-cards)が
    schema_mutation_contributionを直接ここへ渡すだけで済むよう、count/state/
    recovery_action導出ロジック自体は一切複製しない。デフォルトは全て0/空の
    ままなので、これを渡さない既存呼び出し元(apply-dedupe-plan/
    apply-price-link-dedupe)の挙動は完全に不変。
    """
    succeeded = extra_succeeded
    failed = extra_failed
    unknown = extra_unknown
    operations: list[MutationOperation] = list(extra_operations)

    for outcome in outcomes:
        if outcome.status == _STATUS_APPLIED:
            succeeded += 1 + len(outcome.merged_page_ids)
            continue
        if outcome.status != _STATUS_FAILED:
            continue  # PLANNED/SKIPPED_* はNotionへ一切書き込みを試みていない

        has_failed_operation = outcome.failed_operation is not None
        has_failed_completion = outcome.failed_completion is not None
        if not has_failed_operation and not has_failed_completion:
            continue  # Notion書き込みに到達する前の失敗(正当な0 mutation)
        if has_failed_operation != has_failed_completion:
            raise ResultFidelityViolationError(
                "GroupApplyOutcome(status='failed') has inconsistent structured "
                f"failure metadata (failed_operation={outcome.failed_operation!r}, "
                f"failed_completion={outcome.failed_completion!r}, "
                f"card_name={outcome.card_name!r})."
            )

        # 実際に書き込みが試みられた失敗: それより前に成功した書き込みも数える
        # (代表更新はmark失敗の前提として既に成功している)。
        if outcome.failed_operation == FailedGroupOperation.MARK_MERGED:
            succeeded += 1
        succeeded += len(outcome.merged_page_ids)

        mutation_action = _MUTATION_OPERATION_ACTION.get(outcome.failed_operation or "")
        if mutation_action is None:
            raise ResultFidelityViolationError(
                f"Unknown failed_operation value: {outcome.failed_operation!r} "
                f"(card_name={outcome.card_name!r})."
            )

        if outcome.failed_completion == GroupWriteCompletion.KNOWN_FAILED:
            failed += 1
            operations.append(
                MutationOperation(key=outcome.card_name, action=mutation_action, state="failed")
            )
        elif outcome.failed_completion == GroupWriteCompletion.UNKNOWN:
            unknown += 1
            operations.append(
                MutationOperation(key=outcome.card_name, action=mutation_action, state="unknown")
            )
        else:
            raise ResultFidelityViolationError(
                f"Unknown failed_completion value: {outcome.failed_completion!r} "
                f"(card_name={outcome.card_name!r})."
            )

    attempted = succeeded + failed + unknown

    return MutationSummary(
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        unknown=unknown,
        recovery_action=_recovery_action_for(unknown=unknown, failed=failed),
        operations=tuple(operations),
    )


def _recovery_action_for(*, unknown: int, failed: int) -> str:
    if unknown > 0:
        return RecoveryAction.RECONCILE_BEFORE_RETRY
    if failed > 0:
        return RecoveryAction.MANUAL_REVIEW_REQUIRED
    return RecoveryAction.NONE
