import dataclasses
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from generation_workspace import mutation_guard as guard
from generation_workspace.digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS, compute_generation_digest
from generation_workspace.durability import fsync_dir, fsync_file
from generation_workspace.model import (
    LOCK_WITHOUT_CONTROL_TRANSACTION,
    POINTER_SCHEMA_VERSION,
    RECOVERY_CONTEXT_UNCLASSIFIED,
    Pointer,
    generation_directory_name,
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
    TRANSACTION_COMMIT_STATUS_COMMITTED,
    TRANSACTION_COMMIT_STATUS_COMMITTED_OR_EFFECTIVELY_COMMITTED,
    TRANSACTION_COMMIT_STATUS_NOT_COMMITTED,
    TRANSACTION_COMMIT_STATUS_UNCLASSIFIED,
    inspect_generation_recovery,
)
from generation_workspace.resolver import resolve_active_generation
from generation_workspace.transaction import (
    CONTROL_TRANSACTION_FILENAME,
    LOCK_FILENAME,
    begin_generation_transaction,
    commit_generation_transaction,
)

from .conftest import (
    build_transaction_authorization,
    new_uuid,
)

DIGEST_SCHEMA = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))


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


# --- WP-OGR-03-CG-02: Layer2/3 required inspection coverage for Cases -----
# A/B/D/E, plus Layer3 commit-status branch coverage (NOT_COMMITTED /
# COMMITTED_OR_EFFECTIVELY_COMMITTED / COMMITTED). None of these tests use
# the Human-frozen Runtime fixture root; they build ordinary pytest-managed
# tmp_path workspaces and do not substitute for the separate, not-yet-
# authorized V14 targeted Runtime Gate against that frozen fixture. -------


def _populate_staging_from_current(workspace_root, staging_path):
    resolved = resolve_active_generation(workspace_root)
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, staging_path / entry.name)


def _publish_target_generation(workspace_root, begin, digest):
    """Mirrors commit_generation_transaction()'s own publish step: rename
    Staging to its canonical Generation name, then advance
    .control_transaction.json to GENERATION_PUBLISHED. Pointer is untouched."""
    generations_dir = workspace_root / "generations"
    published = generations_dir / f"gen-{begin.target_generation}"
    for entry in begin.staging_path.iterdir():
        fsync_file(entry)
    fsync_dir(begin.staging_path)
    os.rename(begin.staging_path, published)
    fsync_dir(generations_dir)
    txn_path = workspace_root / CONTROL_TRANSACTION_FILENAME
    txn = json.loads(txn_path.read_text(encoding="utf-8"))
    txn["state"] = "GENERATION_PUBLISHED"
    txn["target_generation_digest"] = digest
    txn_path.write_text(json.dumps(txn), encoding="utf-8")
    return published


def _switch_pointer_to_target_through_committing_pointer(workspace_root, begin, digest, txn_id):
    """WP-OGR-03-CG-04-consistent construction: pointer.tmp write ->
    .control_transaction.json advanced to COMMITTING_POINTER -> pointer
    rename, matching commit_generation_transaction()'s own real ordering.
    Never GENERATION_PUBLISHED + pointer-at-target, which is unreachable."""
    pointer = Pointer(
        pointer_schema_version=POINTER_SCHEMA_VERSION,
        generation_id=begin.target_generation,
        generation_digest_schema_version=DIGEST_SCHEMA,
        generation_digest=digest,
        transaction_id=txn_id,
    )
    pointer_path = workspace_root / "active_generation"
    tmp_path = workspace_root / "active_generation.tmp"
    tmp_path.write_bytes(pointer.to_canonical_bytes())
    fsync_file(tmp_path)

    txn_path = workspace_root / CONTROL_TRANSACTION_FILENAME
    txn = json.loads(txn_path.read_text(encoding="utf-8"))
    txn["state"] = "COMMITTING_POINTER"
    txn_path.write_text(json.dumps(txn), encoding="utf-8")

    os.rename(tmp_path, pointer_path)
    fsync_dir(workspace_root)


def test_RI_13_case_a_layer123(bootstrapped_workspace):
    """CG-02 Case A. Also exercises the reachable
    TRANSACTION_COMMIT_STATUS_NOT_COMMITTED branch. This is ordinary
    Repository test coverage against a pytest-managed tmp_path workspace --
    it does not claim to substitute for the separate, not-yet-authorized
    V14 targeted Runtime Gate against the Human-frozen Case-A fixture."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "A"
    assert result.underlying_recovery_status == "NOT_COMMITTED"
    assert result.underlying_safe_action == "RESTART_FROM_STAGING"
    assert result.lock_present is True
    assert result.control_transaction_present is True
    assert result.control_transaction_tmp_present is False
    assert result.pointer_generation_id == begin.source_generation
    assert result.staging_present is True
    assert result.staging_entry_count == 0
    assert result.inspection_classification == INSPECTION_CLASSIFICATION_NONE
    assert result.transaction_binding_status == TRANSACTION_BINDING_MISMATCH
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_NOT_COMMITTED
    assert result.safe_action == "RESTART_FROM_STAGING"
    assert result.automatic_mutation == AUTOMATIC_MUTATION_PROHIBITED


def test_RI_14_case_b_layer123(bootstrapped_workspace):
    """CG-02 Case B."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_target_generation(bootstrapped_workspace, begin, digest)

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "B"
    assert result.underlying_recovery_status == "PREPARED_NOT_COMMITTED"
    assert result.underlying_safe_action == "RETRY_POINTER_SWITCH_AFTER_VERIFICATION"
    assert result.pointer_generation_id == begin.source_generation
    assert result.inspection_classification == INSPECTION_CLASSIFICATION_NONE
    assert result.transaction_binding_status == TRANSACTION_BINDING_MISMATCH
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_NOT_COMMITTED
    assert result.safe_action == "RETRY_POINTER_SWITCH_AFTER_VERIFICATION"


def test_RI_15_case_d_layer123(bootstrapped_workspace):
    """CG-02 Case D (D1 construction: pointer retargeted at an unrelated,
    physically real third generation; see test_recovery.py's T-012/T-013
    for both Case-D decision mechanisms at the Layer1 level)."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason

    unrelated_id = "0000000003"
    generations_dir = bootstrapped_workspace / "generations"
    (generations_dir / generation_directory_name(unrelated_id)).mkdir()

    pointer_path = bootstrapped_workspace / "active_generation"
    original_pointer = parse_pointer(pointer_path.read_bytes())
    retargeted = dataclasses.replace(original_pointer, generation_id=unrelated_id)
    pointer_path.write_bytes(retargeted.to_canonical_bytes())

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "D"
    assert result.underlying_recovery_status == "FAILED_REQUIRES_RECOVERY"
    assert result.underlying_safe_action == "manual_disposition_required"
    assert result.lock_present is True
    assert result.control_transaction_present is True
    assert result.pointer_generation_id == unrelated_id
    assert result.inspection_classification == RECOVERY_CONTEXT_UNCLASSIFIED
    assert result.transaction_binding_status == TRANSACTION_BINDING_MISMATCH
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED
    assert result.safe_action == "manual_disposition_required"


def test_RI_16_case_e_layer123(bootstrapped_workspace):
    """CG-02 Case E: no in-progress transaction, but the active generation's
    content has been tampered with post-commit, so integrity verification
    fails."""
    resolved = resolve_active_generation(bootstrapped_workspace)
    tampered = resolved.generation_path / "source_01_layer_1.jsonl"
    tampered.write_text('{"record_type": "TAMPERED"}\n', encoding="utf-8")

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "E"
    assert result.underlying_recovery_status == "FAILED_REQUIRES_RECOVERY"
    assert result.underlying_safe_action == "manual_disposition_required"
    assert result.lock_present is False
    assert result.control_transaction_present is False
    assert result.inspection_classification == RECOVERY_CONTEXT_UNCLASSIFIED
    assert result.transaction_binding_status == TRANSACTION_BINDING_LOCK_ID_UNAVAILABLE
    assert result.phase_origin == PHASE_ORIGIN_UNCLASSIFIED
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_UNCLASSIFIED
    assert result.safe_action == "manual_disposition_required"
    assert result.automatic_mutation == AUTOMATIC_MUTATION_PROHIBITED


def test_RI_17_committing_pointer_target_commit_status_effectively_committed(
    bootstrapped_workspace,
):
    """Commit-status branch coverage:
    TRANSACTION_COMMIT_STATUS_COMMITTED_OR_EFFECTIVELY_COMMITTED, exercised
    through the real reachable COMMITTING_POINTER + pointer-at-target state."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_target_generation(bootstrapped_workspace, begin, digest)
    _switch_pointer_to_target_through_committing_pointer(
        bootstrapped_workspace, begin, digest, txn_id
    )

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "C"
    assert result.pointer_generation_id == begin.target_generation
    assert (
        result.transaction_commit_status
        == TRANSACTION_COMMIT_STATUS_COMMITTED_OR_EFFECTIVELY_COMMITTED
    )


def test_RI_18_committed_control_present_commit_status_committed(
    bootstrapped_workspace, monkeypatch
):
    """Commit-status branch coverage: TRANSACTION_COMMIT_STATUS_COMMITTED,
    exercised through the same real reachable window as NBF-06
    (test_recovery.py): STATE_COMMITTED persisted, control transaction not
    yet cleaned up. Constructed by interrupting only the real commit path's
    own transient cleanup unlink, not by hand-assembling bytes."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)

    control_path = bootstrapped_workspace / CONTROL_TRANSACTION_FILENAME
    real_unlink = Path.unlink

    def _fail_only_for_control_transaction_unlink(self, *args, **kwargs):
        if self == control_path:
            raise OSError("simulated crash during control-transaction cleanup unlink")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _fail_only_for_control_transaction_unlink)
    commit_result = commit_generation_transaction(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, authorization
    )
    monkeypatch.undo()

    assert not commit_result.ok
    assert commit_result.error_code == guard.FAILED_REQUIRES_RECOVERY
    assert control_path.exists()

    result = inspect_generation_recovery(bootstrapped_workspace)

    assert result.underlying_recovery_case == "C"
    assert result.control_transaction_present is True
    assert result.pointer_generation_id == begin.target_generation
    assert result.transaction_commit_status == TRANSACTION_COMMIT_STATUS_COMMITTED


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
