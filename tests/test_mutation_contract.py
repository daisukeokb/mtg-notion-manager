"""Error Contract v2の`mutation`メタデータ(mutation_contract.py)のunit tests。

このモジュールはpure data + validationのため、Notion/CLIへは一切接続しない。
どのcommandもまだこれをemit_error_json()経由で使用していない(infrastructure-only)。
"""

from __future__ import annotations

import pytest

from mtg_notion_manager.mutation_contract import (
    MutationOperation,
    MutationState,
    MutationSummary,
    RecoveryAction,
)

# --- T1-T5: state derivation ------------------------------------------------------


def test_no_mutation_state() -> None:
    summary = MutationSummary(
        attempted=0, succeeded=0, failed=0, unknown=0, recovery_action=RecoveryAction.NONE
    )

    assert summary.state == MutationState.NO_MUTATION
    assert summary.to_dict()["state"] == MutationState.NO_MUTATION
    assert "operations" not in summary.to_dict()


def test_mutation_succeeded_state() -> None:
    summary = MutationSummary(
        attempted=3, succeeded=3, failed=0, unknown=0, recovery_action=RecoveryAction.NONE
    )

    assert summary.state == MutationState.MUTATION_SUCCEEDED
    assert summary.to_dict() == {
        "state": "MUTATION_SUCCEEDED",
        "attempted": 3,
        "succeeded": 3,
        "failed": 0,
        "unknown": 0,
        "recovery_action": "NONE",
    }


def test_partial_mutation_state() -> None:
    summary = MutationSummary(
        attempted=3,
        succeeded=2,
        failed=1,
        unknown=0,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
    )

    assert summary.state == MutationState.PARTIAL_MUTATION
    assert summary.to_dict()["state"] == "PARTIAL_MUTATION"
    assert summary.to_dict()["recovery_action"] == "RECONCILE_BEFORE_RETRY"


def test_mutation_failed_state() -> None:
    summary = MutationSummary(
        attempted=3,
        succeeded=0,
        failed=3,
        unknown=0,
        recovery_action=RecoveryAction.MANUAL_REVIEW_REQUIRED,
    )

    assert summary.state == MutationState.MUTATION_FAILED


def test_mutation_state_unknown() -> None:
    summary = MutationSummary(
        attempted=3,
        succeeded=2,
        failed=0,
        unknown=1,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
    )

    assert summary.state == MutationState.MUTATION_STATE_UNKNOWN


# --- Count invariant / negative-count validation ----------------------------------


@pytest.mark.parametrize("field_name", ["attempted", "succeeded", "failed", "unknown"])
def test_negative_count_is_rejected(field_name: str) -> None:
    kwargs = {"attempted": 0, "succeeded": 0, "failed": 0, "unknown": 0}
    kwargs[field_name] = -1

    with pytest.raises(ValueError, match=f"{field_name}.*>= 0"):
        MutationSummary(recovery_action=RecoveryAction.NONE, **kwargs)


def test_count_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="count invariant"):
        MutationSummary(
            attempted=3, succeeded=1, failed=1, unknown=0, recovery_action=RecoveryAction.NONE
        )


def test_attempted_zero_but_succeeded_positive_is_rejected() -> None:
    with pytest.raises(ValueError, match="count invariant"):
        MutationSummary(
            attempted=0, succeeded=1, failed=0, unknown=0, recovery_action=RecoveryAction.NONE
        )


# --- Recovery-action validation ----------------------------------------------------


def test_no_mutation_with_reconcile_before_retry_is_rejected() -> None:
    with pytest.raises(ValueError, match="not allowed for state"):
        MutationSummary(
            attempted=0,
            succeeded=0,
            failed=0,
            unknown=0,
            recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
        )


def test_unknown_state_with_none_recovery_is_rejected() -> None:
    with pytest.raises(ValueError, match="not allowed for state"):
        MutationSummary(
            attempted=1, succeeded=0, failed=0, unknown=1, recovery_action=RecoveryAction.NONE
        )


def test_unknown_state_cannot_be_marked_retry_allowed() -> None:
    """MUTATION_STATE_UNKNOWNにblind retryを許可してはいけない、という最重要不変条件。"""
    with pytest.raises(ValueError, match="not allowed for state"):
        MutationSummary(
            attempted=1,
            succeeded=0,
            failed=0,
            unknown=1,
            recovery_action=RecoveryAction.RETRY_ALLOWED,
        )


def test_unknown_state_allows_reconcile_before_retry() -> None:
    summary = MutationSummary(
        attempted=1,
        succeeded=0,
        failed=0,
        unknown=1,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
    )
    assert summary.recovery_action == RecoveryAction.RECONCILE_BEFORE_RETRY


def test_unknown_state_allows_manual_review_required() -> None:
    summary = MutationSummary(
        attempted=1,
        succeeded=0,
        failed=0,
        unknown=1,
        recovery_action=RecoveryAction.MANUAL_REVIEW_REQUIRED,
    )
    assert summary.recovery_action == RecoveryAction.MANUAL_REVIEW_REQUIRED


def test_mutation_failed_allows_retry_allowed_when_caller_asserts_it() -> None:
    """RETRY_ALLOWEDの自動推論はしないが、caller(将来のcommand contract)が
    明示すれば許容する(MUTATION_FAILEDのみ、UNKNOWNでは不可)。"""
    summary = MutationSummary(
        attempted=2, succeeded=0, failed=2, unknown=0, recovery_action=RecoveryAction.RETRY_ALLOWED
    )
    assert summary.recovery_action == RecoveryAction.RETRY_ALLOWED


def test_invalid_recovery_action_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="recovery_action"):
        MutationSummary(
            attempted=1, succeeded=1, failed=0, unknown=0, recovery_action="NOT_A_REAL_ACTION"
        )


# --- Operation validation -----------------------------------------------------------


def test_operation_with_empty_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="key must be a non-empty string"):
        MutationOperation(key="", action="create", state="failed")


def test_operation_with_empty_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="action must be a non-empty string"):
        MutationOperation(key="島", action="", state="failed")


def test_operation_with_invalid_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="state must be one of"):
        MutationOperation(key="島", action="create", state="succeeded")


def test_operation_state_must_match_nonzero_summary_count() -> None:
    """summary.unknown==0なのにoperationsへstate="unknown"を含めるのは無効。"""
    with pytest.raises(ValueError, match="MutationSummary.unknown==0"):
        MutationSummary(
            attempted=1,
            succeeded=0,
            failed=1,
            unknown=0,
            recovery_action=RecoveryAction.MANUAL_REVIEW_REQUIRED,
            operations=(MutationOperation(key="島", action="create", state="unknown"),),
        )


def test_operation_state_failed_rejected_when_summary_failed_is_zero() -> None:
    with pytest.raises(ValueError, match="MutationSummary.failed==0"):
        MutationSummary(
            attempted=1,
            succeeded=0,
            failed=0,
            unknown=1,
            recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
            operations=(MutationOperation(key="島", action="create", state="failed"),),
        )


def test_failed_operation_serialization() -> None:
    summary = MutationSummary(
        attempted=2,
        succeeded=1,
        failed=1,
        unknown=0,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
        operations=(MutationOperation(key="沼", action="create", state="failed"),),
    )

    assert summary.to_dict()["operations"] == [
        {"key": "沼", "action": "create", "state": "failed"}
    ]


def test_unknown_operation_serialization() -> None:
    summary = MutationSummary(
        attempted=2,
        succeeded=1,
        failed=0,
        unknown=1,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
        operations=(MutationOperation(key="島", action="create", state="unknown"),),
    )

    assert summary.to_dict()["operations"] == [
        {"key": "島", "action": "create", "state": "unknown"}
    ]


def test_mixed_failed_and_unknown_operations_serialization() -> None:
    summary = MutationSummary(
        attempted=4,
        succeeded=2,
        failed=1,
        unknown=1,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
        operations=(
            MutationOperation(key="沼", action="create", state="failed"),
            MutationOperation(key="島", action="create", state="unknown"),
        ),
    )

    assert summary.to_dict()["operations"] == [
        {"key": "沼", "action": "create", "state": "failed"},
        {"key": "島", "action": "create", "state": "unknown"},
    ]


def test_operations_omitted_by_default() -> None:
    summary = MutationSummary(
        attempted=3, succeeded=3, failed=0, unknown=0, recovery_action=RecoveryAction.NONE
    )
    assert summary.operations == ()
    assert "operations" not in summary.to_dict()


def test_operations_explicitly_empty_is_equivalent_to_omitted() -> None:
    summary = MutationSummary(
        attempted=1,
        succeeded=0,
        failed=1,
        unknown=0,
        recovery_action=RecoveryAction.MANUAL_REVIEW_REQUIRED,
        operations=(),
    )
    assert "operations" not in summary.to_dict()


def test_operations_are_not_required_to_enumerate_every_failed_or_unknown_item() -> None:
    """len(operations) == failed+unknown を必須にしない(payload sizeの都合で
    一部だけ、あるいは一切含めない選択を許容する)。"""
    summary = MutationSummary(
        attempted=5,
        succeeded=2,
        failed=2,
        unknown=1,
        recovery_action=RecoveryAction.RECONCILE_BEFORE_RETRY,
        operations=(MutationOperation(key="島", action="create", state="unknown"),),
    )
    assert len(summary.operations) == 1
    assert summary.failed == 2  # operationsに列挙されていないfailedが2件あってもよい


def test_summary_and_operations_are_immutable() -> None:
    summary = MutationSummary(
        attempted=1, succeeded=1, failed=0, unknown=0, recovery_action=RecoveryAction.NONE
    )
    operation = MutationOperation(key="沼", action="create", state="failed")

    with pytest.raises(AttributeError):
        summary.attempted = 99  # type: ignore[misc]
    with pytest.raises(AttributeError):
        operation.key = "島"  # type: ignore[misc]
