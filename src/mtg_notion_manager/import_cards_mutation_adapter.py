"""import-cards の実行結果を Error Contract v2 の MutationSummary へ変換する adapter。

責務の境界:
- サービス層(services/import_cards.py)は「何を試みたか」「既知の失敗か完了状態
  不明かの事実」だけを保持する(CardApplyResult.failed_operation/failed_completion、
  action=="failed"のときのみ設定)。message文字列の解析には一切依存しない。
- このモジュールはその事実を、公開contractの語彙(MutationState/RecoveryAction/
  MutationSummary、mutation_contract.py)へ変換する、import-cards専用のadapter。
  dedupe-family等への再利用を想定した汎用フレームワークではない。

execute_import_cards()の正常return(ImportCardsResult.results)と、
PartialImportAbortedError.completed_results のどちらに対しても同じ
build_import_cards_mutation_summary()を使う(どちらも同じCardApplyResultの列であり、
数え方・recovery_action導出ロジックを分岐させる理由がないため)。
"""

from __future__ import annotations

from collections.abc import Sequence

from mtg_notion_manager.mutation_contract import (
    MutationOperation,
    MutationSummary,
    RecoveryAction,
)
from mtg_notion_manager.services.import_cards import (
    CardApplyResult,
    FailedWriteOperation,
    WriteCompletion,
)

_SUCCESSFUL_WRITE_ACTIONS = frozenset({"created", "relation_updated"})

_MUTATION_OPERATION_ACTION = {
    FailedWriteOperation.CREATE: "create",
    FailedWriteOperation.RELATION_UPDATE: "relation_update",
}


class ResultFidelityViolationError(RuntimeError):
    """action=="failed"のCardApplyResultに、期待される構造化failure metadata
    (failed_operation/failed_completion)のいずれかが欠落していた。

    これはユーザー入力やNotion APIの問題ではなく、このコードベース自身の
    データ整合性が壊れていることを示す内部プログラミングエラーである
    (MtgNotionManagerError系ではない――CLIの既存except節へ意図せず吸収されない
    ようにするため)。メッセージ文字列からの推測で穴埋めせず、fail closedする。
    """


def build_import_cards_mutation_summary(results: Sequence[CardApplyResult]) -> MutationSummary:
    """CardApplyResultの列から、実際に書き込みを試みた件数だけを数えたMutationSummaryを
    構築する(unchanged等、Notionへ書き込みを試みていない結果はattemptedへ含めない)。

    recovery_actionはunknown/failedの件数だけから導出する(RETRY_ALLOWEDは
    rerun-safetyが別Work Unitで証明されるまでemitしない――MUTATION_FAILEDであっても
    MANUAL_REVIEW_REQUIREDに留める)。この導出方法はmutation_contract.pyの
    state導出(unknown優先)と同じ優先順位を使うため、生成するMutationSummaryが
    _ALLOWED_RECOVERY_BY_STATEの不変条件に違反することはない。
    """
    succeeded = 0
    failed = 0
    unknown = 0
    operations: list[MutationOperation] = []

    for result in results:
        if result.action in _SUCCESSFUL_WRITE_ACTIONS:
            succeeded += 1
            continue
        if result.action != "failed":
            continue  # unchanged 等、書き込みを試みていない結果はcountしない

        if result.failed_operation is None or result.failed_completion is None:
            raise ResultFidelityViolationError(
                "CardApplyResult(action='failed') is missing structured failure metadata "
                f"(failed_operation={result.failed_operation!r}, "
                f"failed_completion={result.failed_completion!r}, "
                f"card={result.card.display_name!r})."
            )

        mutation_action = _MUTATION_OPERATION_ACTION.get(result.failed_operation)
        if mutation_action is None:
            raise ResultFidelityViolationError(
                f"Unknown failed_operation value: {result.failed_operation!r} "
                f"(card={result.card.display_name!r})."
            )

        if result.failed_completion == WriteCompletion.KNOWN_FAILED:
            failed += 1
            operations.append(
                MutationOperation(
                    key=result.card.display_name, action=mutation_action, state="failed"
                )
            )
        elif result.failed_completion == WriteCompletion.UNKNOWN:
            unknown += 1
            operations.append(
                MutationOperation(
                    key=result.card.display_name, action=mutation_action, state="unknown"
                )
            )
        else:
            raise ResultFidelityViolationError(
                f"Unknown failed_completion value: {result.failed_completion!r} "
                f"(card={result.card.display_name!r})."
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
    """mutation自体の状態だけからrecovery_actionを決める(top-levelの
    error_category/error_codeは一切参照しない――mutation.recovery_actionは
    「このmutation stateを解消するには何が必要か」だけを表し、command自体の
    domain error解消を代替しない。mutation_contract.pyのRecoveryAction参照)。
    """
    if unknown > 0:
        return RecoveryAction.RECONCILE_BEFORE_RETRY
    if failed > 0:
        return RecoveryAction.MANUAL_REVIEW_REQUIRED
    return RecoveryAction.NONE
