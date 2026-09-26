from __future__ import annotations

from mtg_notion_manager.dedupe_apply_mutation_adapter import build_dedupe_apply_mutation_summary
from mtg_notion_manager.dedupe_cards_mutation_adapter import (
    NO_SCHEMA_CONTRIBUTION,
    SchemaMutationContribution,
    _wrap_group_apply_results,
    build_dedupe_cards_mutation_summary,
    schema_mutation_contribution_for_failure,
    schema_mutation_contribution_for_success,
)
from mtg_notion_manager.mutation_contract import MutationOperation
from mtg_notion_manager.services.dedupe_cards import (
    DedupeApplyResult,
    FailedGroupOperation,
    GroupApplyResult,
    GroupWriteCompletion,
)
from mtg_notion_manager.services.dedupe_schema import (
    SchemaMigrationExecutionError,
    SchemaMigrationResult,
    SchemaWriteCompletion,
)


def _dedupe_success(card_name: str, marked: list[str] | None = None) -> GroupApplyResult:
    return GroupApplyResult(
        card_name=card_name,
        representative_page_id="p1",
        representative_updated=True,
        duplicate_page_ids_marked=marked or [],
    )


def _dedupe_known_failed(card_name: str, marked: list[str] | None = None) -> GroupApplyResult:
    return GroupApplyResult(
        card_name=card_name,
        representative_page_id="p1",
        representative_updated=True,
        duplicate_page_ids_marked=marked or [],
        error=f"Notion API呼び出しに失敗しました: {card_name}",
        failed_operation=FailedGroupOperation.MARK_MERGED,
        failed_completion=GroupWriteCompletion.KNOWN_FAILED,
    )


def _dedupe_unknown(card_name: str, marked: list[str] | None = None) -> GroupApplyResult:
    return GroupApplyResult(
        card_name=card_name,
        representative_page_id="p1",
        representative_updated=True,
        duplicate_page_ids_marked=marked or [],
        error=f"Notion APIへの接続がタイムアウトしました: {card_name}",
        failed_operation=FailedGroupOperation.MARK_MERGED,
        failed_completion=GroupWriteCompletion.UNKNOWN,
    )


def _schema_failure(completion: str) -> SchemaMigrationExecutionError:
    return SchemaMigrationExecutionError(
        SchemaMigrationResult(completion=completion, property_names=("所持枚数", "統合済み")),
        "Notion API呼び出しに失敗しました (400): bad request",
    )


def test_no_schema_contribution_is_all_zero() -> None:
    assert NO_SCHEMA_CONTRIBUTION == SchemaMutationContribution(
        succeeded=0, failed=0, unknown=0, operations=()
    )


def test_schema_contribution_for_success_has_no_operations() -> None:
    result = SchemaMigrationResult(
        completion=SchemaWriteCompletion.SUCCEEDED, property_names=("所持枚数", "統合済み")
    )

    contribution = schema_mutation_contribution_for_success(result)

    assert contribution == SchemaMutationContribution(
        succeeded=1, failed=0, unknown=0, operations=()
    )


def test_schema_contribution_for_known_failure() -> None:
    exc = _schema_failure(SchemaWriteCompletion.KNOWN_FAILED)

    contribution = schema_mutation_contribution_for_failure(exc)

    assert contribution.succeeded == 0
    assert contribution.failed == 1
    assert contribution.unknown == 0
    assert len(contribution.operations) == 1
    op = contribution.operations[0]
    assert op.action == "schema_update"
    assert op.state == "failed"
    assert op.key == "所持枚数、統合済み"


def test_schema_contribution_for_unknown_completion() -> None:
    exc = _schema_failure(SchemaWriteCompletion.UNKNOWN)

    contribution = schema_mutation_contribution_for_failure(exc)

    assert contribution.succeeded == 0
    assert contribution.failed == 0
    assert contribution.unknown == 1
    assert contribution.operations[0].state == "unknown"


def test_aggregate_dedupe_only_matches_shared_adapter_result() -> None:
    """T10: schema mutationなしのdedupe-only aggregateは、既存共有adapterを
    直接呼んだ場合と完全に一致する(dedupe-cards固有の集計ロジックを増やさない)。
    """
    dedupe_result = DedupeApplyResult(
        results=[_dedupe_success("沼", marked=["p2"]), _dedupe_known_failed("島", marked=[])]
    )

    aggregate = build_dedupe_cards_mutation_summary(dedupe_result, NO_SCHEMA_CONTRIBUTION)
    direct = build_dedupe_apply_mutation_summary(_wrap_group_apply_results(dedupe_result))

    assert aggregate == direct


def test_aggregate_schema_success_plus_dedupe_success() -> None:
    """§18 Case B: schema 1 success + dedupe N success。"""
    dedupe_result = DedupeApplyResult(
        results=[_dedupe_success("沼", marked=["p2", "p3"]), _dedupe_success("島", marked=[])]
    )
    schema_contribution = SchemaMutationContribution(succeeded=1)

    summary = build_dedupe_cards_mutation_summary(dedupe_result, schema_contribution)

    # dedupe分: 沼(代表+2 marked=3) + 島(代表のみ=1) = 4。schema分: 1。
    assert summary.attempted == 5
    assert summary.succeeded == 5
    assert summary.failed == 0
    assert summary.unknown == 0
    assert summary.state == "MUTATION_SUCCEEDED"
    assert summary.recovery_action == "NONE"


def test_aggregate_schema_success_plus_dedupe_partial_known_failure() -> None:
    """§18 Case E: schema success(1)、dedupeはrepresentative成功1件+known failure1件。"""
    dedupe_result = DedupeApplyResult(
        results=[_dedupe_success("沼", marked=[]), _dedupe_known_failed("島", marked=[])]
    )
    schema_contribution = SchemaMutationContribution(succeeded=1)

    summary = build_dedupe_cards_mutation_summary(dedupe_result, schema_contribution)

    # schema: succeeded=1。dedupe: 沼(代表成功=1) + 島(代表成功だがmark失敗=1)
    # = succeeded 2, failed 1。
    assert summary.attempted == 4
    assert summary.succeeded == 3
    assert summary.failed == 1
    assert summary.unknown == 0
    assert summary.state == "PARTIAL_MUTATION"
    assert summary.recovery_action == "MANUAL_REVIEW_REQUIRED"
    assert summary.operations == (
        MutationOperation(key="島", action="mark_merged", state="failed"),
    )


def test_aggregate_schema_success_plus_dedupe_unknown() -> None:
    dedupe_result = DedupeApplyResult(results=[_dedupe_unknown("島", marked=[])])
    schema_contribution = SchemaMutationContribution(succeeded=1)

    summary = build_dedupe_cards_mutation_summary(dedupe_result, schema_contribution)

    # schema: succeeded=1。dedupe: 島は代表更新まで成功(succeeded+=1)した後
    # markがunknownで失敗(unknown+=1) => dedupe分はsucceeded=1, unknown=1。
    assert summary.attempted == 3
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert summary.unknown == 1
    assert summary.state == "MUTATION_STATE_UNKNOWN"
    assert summary.recovery_action == "RECONCILE_BEFORE_RETRY"


def test_aggregate_schema_only_known_failure_dedupe_not_attempted() -> None:
    """§18 Case C: schema KNOWN_FAILED、dedupe phaseへ進まない(dedupe_result=None)。"""
    contribution = schema_mutation_contribution_for_failure(
        _schema_failure(SchemaWriteCompletion.KNOWN_FAILED)
    )

    summary = build_dedupe_cards_mutation_summary(None, contribution)

    assert summary.attempted == 1
    assert summary.succeeded == 0
    assert summary.failed == 1
    assert summary.unknown == 0
    assert summary.state == "MUTATION_FAILED"
    assert summary.recovery_action == "MANUAL_REVIEW_REQUIRED"


def test_aggregate_schema_only_unknown_dedupe_not_attempted() -> None:
    """§18 Case D: schema UNKNOWN、dedupe phaseへ進まない(dedupe_result=None)。"""
    contribution = schema_mutation_contribution_for_failure(
        _schema_failure(SchemaWriteCompletion.UNKNOWN)
    )

    summary = build_dedupe_cards_mutation_summary(None, contribution)

    assert summary.attempted == 1
    assert summary.succeeded == 0
    assert summary.failed == 0
    assert summary.unknown == 1
    assert summary.state == "MUTATION_STATE_UNKNOWN"
    assert summary.recovery_action == "RECONCILE_BEFORE_RETRY"


def test_aggregate_attempted_invariant_holds_across_combinations() -> None:
    """T16: 全v2 mutation error pathでattempted == succeeded+failed+unknown。"""
    cases = [
        (None, schema_mutation_contribution_for_failure(_schema_failure("known_failed"))),
        (None, schema_mutation_contribution_for_failure(_schema_failure("unknown"))),
        (
            DedupeApplyResult(results=[_dedupe_known_failed("島")]),
            SchemaMutationContribution(succeeded=1),
        ),
        (
            DedupeApplyResult(results=[_dedupe_unknown("島")]),
            SchemaMutationContribution(succeeded=1),
        ),
        (DedupeApplyResult(results=[_dedupe_success("沼")]), NO_SCHEMA_CONTRIBUTION),
    ]
    for dedupe_result, contribution in cases:
        summary = build_dedupe_cards_mutation_summary(dedupe_result, contribution)
        assert summary.attempted == summary.succeeded + summary.failed + summary.unknown
