import hashlib
import json
import os
import uuid

from generation_workspace.model import (
    LOCK_WITHOUT_CONTROL_TRANSACTION,
    RECOVERY_CONTEXT_UNCLASSIFIED,
    parse_pointer,
)
from generation_workspace.recovery_inspection import (
    AUTOMATIC_MUTATION_PROHIBITED,
    INSPECTION_CLASSIFICATION_NONE,
    LOCK_METADATA_STATUS_MALFORMED,
    PHASE_ORIGIN_UNCLASSIFIED,
    SAFE_ACTION_MANUAL_LOCK_STATE_REVIEW_REQUIRED,
    TRANSACTION_BINDING_BOTH_IDS_UNAVAILABLE,
    TRANSACTION_BINDING_LOCK_ID_UNAVAILABLE,
    TRANSACTION_BINDING_MATCH,
    TRANSACTION_BINDING_MISMATCH,
    TRANSACTION_COMMIT_STATUS_UNCLASSIFIED,
    inspect_generation_recovery,
)
from generation_workspace.transaction import LOCK_FILENAME

from .conftest import (
    build_transaction_authorization,
    new_uuid,
)


def _pointer_transaction_id(workspace_root):
    pointer_path = workspace_root / "active_generation"
    return parse_pointer(pointer_path.read_bytes()).transaction_id


def _write_lock(workspace_root, transaction_id: str) -> None:
    payload = {
        "lock_schema_version": "TEST-LOCK-v1",
        "lock_id": new_uuid(),
        "transaction_id": transaction_id,
        "authorization_id": new_uuid(),
        "authorization_digest": "0" * 64,
        "created_at_utc": "2026-01-01T00:00:00Z",
    }
    (workspace_root / LOCK_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def _write_malformed_lock(workspace_root) -> None:
    (workspace_root / LOCK_FILENAME).write_text("not valid json", encoding="utf-8")


def _corrupt_pointer(workspace_root) -> None:
    (workspace_root / "active_generation").write_text("not valid json", encoding="utf-8")


def test_RI_01_no_binding(bootstrapped_workspace):
    _write_lock(bootstrapped_workspace, new_uuid())

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.inspection_classification == LOCK_WITHOUT_CONTROL_TRANSACTION
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED
    assert result.safe_action == SAFE_ACTION_MANUAL_LOCK_STATE_REVIEW_REQUIRED


def test_RI_02_matching_identity_is_correlation_only(bootstrapped_workspace):
    pointer_txn_id = _pointer_transaction_id(bootstrapped_workspace)
    _write_lock(bootstrapped_workspace, pointer_txn_id)

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.transaction_binding_status == TRANSACTION_BINDING_MATCH
    # Critical: MATCH alone must not promote phase_origin or transaction_commit_status.
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


def test_RI_03_mismatched_identity(bootstrapped_workspace):
    _write_lock(bootstrapped_workspace, str(uuid.uuid4()))

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.transaction_binding_status == TRANSACTION_BINDING_MISMATCH
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED
    assert result.automatic_mutation == AUTOMATIC_MUTATION_PROHIBITED


def test_RI_04_malformed_lock_identity(bootstrapped_workspace):
    _write_malformed_lock(bootstrapped_workspace)

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.lock_metadata_status == LOCK_METADATA_STATUS_MALFORMED
    assert result.lock_transaction_id is None
    assert result.transaction_binding_status == TRANSACTION_BINDING_LOCK_ID_UNAVAILABLE


def test_RI_05_underlying_case_c_preserved(bootstrapped_workspace):
    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "C"


def test_RI_06_case_c_without_lock(bootstrapped_workspace):
    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "C"
    assert result.inspection_classification == INSPECTION_CLASSIFICATION_NONE
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


def test_RI_07_matching_binding_with_control_absent(bootstrapped_workspace):
    pointer_txn_id = _pointer_transaction_id(bootstrapped_workspace)
    _write_lock(bootstrapped_workspace, pointer_txn_id)

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.inspection_classification == LOCK_WITHOUT_CONTROL_TRANSACTION
    assert result.transaction_binding_status == TRANSACTION_BINDING_MATCH
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


def test_RI_08_mutation_output_type_contract(bootstrapped_workspace):
    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.automatic_mutation == "PROHIBITED"
    assert isinstance(result.automatic_mutation, str)


def test_RI_09_both_transaction_ids_unavailable(tmp_path):
    result = inspect_generation_recovery(tmp_path)

    assert result.transaction_binding_status == TRANSACTION_BINDING_BOTH_IDS_UNAVAILABLE


def test_RI_10_stable_state_does_not_reconstruct_commit(bootstrapped_workspace):
    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


def test_RI_11_no_na_in_serialized_output(bootstrapped_workspace):
    result = inspect_generation_recovery(bootstrapped_workspace)
    serialized = json.dumps(result.__dict__, ensure_ascii=False)

    assert "N/A" not in serialized


def test_RI_12_phase14_inspection_remains_unclassified(bootstrapped_workspace):
    pointer_txn_id = _pointer_transaction_id(bootstrapped_workspace)
    _write_lock(bootstrapped_workspace, pointer_txn_id)

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.transaction_binding_status == TRANSACTION_BINDING_MATCH
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


# --- Additional directly-required regression tests (Section 30) -------------


def test_recovery_context_unclassified_reachable_without_lock(bootstrapped_workspace):
    """RECOVERY_CONTEXT_UNCLASSIFIED must be reachable for a
    FAILED_REQUIRES_RECOVERY underlying case that is not itself a
    lock-without-control observation (no lock file involved at all)."""
    _corrupt_pointer(bootstrapped_workspace)

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_status == "FAILED_REQUIRES_RECOVERY"
    assert result.lock_present is False
    assert result.inspection_classification == RECOVERY_CONTEXT_UNCLASSIFIED
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


def _workspace_fingerprint(root):
    entries = []
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            entries.append((rel, "symlink", os.readlink(p)))
        elif p.is_dir():
            entries.append((rel, "dir", None))
        elif p.is_file():
            entries.append((rel, "file", hashlib.sha256(p.read_bytes()).hexdigest()))
    return entries


def test_inspection_is_side_effect_free(bootstrapped_workspace):
    """Covers lock, control transaction (+ .tmp), staging, pointer, and
    Generation files/directories: none of them may change due to
    inspection, whether or not a transaction is in flight."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)

    from generation_workspace.transaction import begin_generation_transaction

    begin_result = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin_result.ok, begin_result.reason

    before = _workspace_fingerprint(bootstrapped_workspace)
    result = inspect_generation_recovery(bootstrapped_workspace)
    after = _workspace_fingerprint(bootstrapped_workspace)

    assert before == after
    assert result.lock_present is True
    assert result.control_transaction_present is True
    assert result.staging_present is True
