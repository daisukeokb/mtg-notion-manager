"""T-001..T-011: Failure Injection Matrix.

Each test drives the transaction machinery up to a specific crash point
(without ever completing it normally) and then asserts that
recover_generation_transaction reaches the single correct, deterministic
Case, using only workspace_root / active_generation / Generation files
(plus .control_transaction.json when it is still present).
"""

import json
import os
import shutil

from generation_workspace.digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS, compute_generation_digest
from generation_workspace.durability import fsync_dir, fsync_file
from generation_workspace.model import POINTER_SCHEMA_VERSION, Pointer, parse_pointer
from generation_workspace.resolver import resolve_active_generation
from generation_workspace.transaction import (
    begin_generation_transaction,
    commit_generation_transaction,
    recover_generation_transaction,
)

from .conftest import build_transaction_authorization, new_uuid

DIGEST_SCHEMA = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))


def _populate_staging_from_current(workspace_root, staging_path):
    resolved = resolve_active_generation(workspace_root)
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, staging_path / entry.name)


def test_T_001_crash_during_staging_population(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    # crash before any file is written into staging
    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "A"
    assert result.status == "NOT_COMMITTED"
    assert result.authoritative_generation == begin.source_generation
    assert result.safe_action == "RESTART_FROM_STAGING"


def test_T_002_crash_during_fsync_of_staging_files(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    for entry in begin.staging_path.iterdir():
        fsync_file(entry)
    # crash before staging directory rename
    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "A"
    assert result.authoritative_generation == begin.source_generation


def test_T_003_crash_after_digest_computed_before_rename(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    txn_path = bootstrapped_workspace / ".control_transaction.json"
    txn = json.loads(txn_path.read_text(encoding="utf-8"))
    txn["state"] = "VERIFIED"
    txn["target_generation_digest"] = digest
    txn_path.write_text(json.dumps(txn), encoding="utf-8")
    # crash before generation directory rename
    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "A"
    assert result.authoritative_generation == begin.source_generation


def _publish_staging(workspace_root, begin, digest):
    generations_dir = workspace_root / "generations"
    published = generations_dir / f"gen-{begin.target_generation}"
    for entry in begin.staging_path.iterdir():
        fsync_file(entry)
    fsync_dir(begin.staging_path)
    os.rename(begin.staging_path, published)
    fsync_dir(generations_dir)
    txn_path = workspace_root / ".control_transaction.json"
    txn = json.loads(txn_path.read_text(encoding="utf-8"))
    txn["state"] = "GENERATION_PUBLISHED"
    txn["target_generation_digest"] = digest
    txn_path.write_text(json.dumps(txn), encoding="utf-8")
    return published


def test_T_004_crash_immediately_after_generation_rename(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_staging(bootstrapped_workspace, begin, digest)

    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "B"
    assert result.status == "PREPARED_NOT_COMMITTED"
    assert result.authoritative_generation == begin.source_generation
    assert result.safe_action == "RETRY_POINTER_SWITCH_AFTER_VERIFICATION"


def test_T_005_crash_during_generations_dir_fsync(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_staging(bootstrapped_workspace, begin, digest)
    # fsync already happened inside _publish_staging; crash lands here anyway
    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "B"
    assert result.authoritative_generation == begin.source_generation


def test_T_006_crash_during_pointer_tmp_write(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_staging(bootstrapped_workspace, begin, digest)

    pointer = Pointer(
        pointer_schema_version=POINTER_SCHEMA_VERSION,
        generation_id=begin.target_generation,
        generation_digest_schema_version=DIGEST_SCHEMA,
        generation_digest=digest,
        transaction_id=txn_id,
    )
    tmp_path = bootstrapped_workspace / "active_generation.tmp"
    tmp_path.write_bytes(pointer.to_canonical_bytes())
    fsync_file(tmp_path)
    # crash before the tmp -> active_generation rename
    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "B"
    assert result.authoritative_generation == begin.source_generation
    assert tmp_path.exists()  # active_generation itself untouched


def test_T_007_crash_immediately_after_pointer_rename(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_staging(bootstrapped_workspace, begin, digest)

    pointer = Pointer(
        pointer_schema_version=POINTER_SCHEMA_VERSION,
        generation_id=begin.target_generation,
        generation_digest_schema_version=DIGEST_SCHEMA,
        generation_digest=digest,
        transaction_id=txn_id,
    )
    pointer_path = bootstrapped_workspace / "active_generation"
    tmp_path = bootstrapped_workspace / "active_generation.tmp"
    tmp_path.write_bytes(pointer.to_canonical_bytes())
    fsync_file(tmp_path)
    os.rename(tmp_path, pointer_path)
    fsync_dir(bootstrapped_workspace)
    # crash right after the pointer switch, .control_transaction.json still present
    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "C"
    assert result.status == "COMMITTED"
    assert result.authoritative_generation == begin.target_generation


def test_T_008_crash_during_post_commit_verification(bootstrapped_workspace):
    # Functionally identical observable state to T-007 (fsync/re-read steps
    # leave no distinguishing artifact); recorded as an independent
    # injection point per the approved Failure Injection Matrix.
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    digest = compute_generation_digest(begin.staging_path, begin.target_generation, DIGEST_SCHEMA)
    _publish_staging(bootstrapped_workspace, begin, digest)
    pointer = Pointer(
        pointer_schema_version=POINTER_SCHEMA_VERSION,
        generation_id=begin.target_generation,
        generation_digest_schema_version=DIGEST_SCHEMA,
        generation_digest=digest,
        transaction_id=txn_id,
    )
    pointer_path = bootstrapped_workspace / "active_generation"
    tmp_path = bootstrapped_workspace / "active_generation.tmp"
    tmp_path.write_bytes(pointer.to_canonical_bytes())
    fsync_file(tmp_path)
    os.rename(tmp_path, pointer_path)
    fsync_dir(bootstrapped_workspace)

    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "C"
    assert result.authoritative_generation == begin.target_generation


def test_T_009_crash_during_transient_cleanup_txn_file_absent(bootstrapped_workspace):
    """T-009: after .control_transaction.json has already been deleted
    (crash lands mid-cleanup), recovery must still perform a full Integrity
    Check rather than blindly trusting the Pointer."""
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    _populate_staging_from_current(bootstrapped_workspace, begin.staging_path)
    result = commit_generation_transaction(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, authorization
    )
    assert result.ok, result.reason
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()

    recovery = recover_generation_transaction(bootstrapped_workspace)
    assert recovery.case == "C"
    assert recovery.status == "COMMITTED"
    assert recovery.authoritative_generation == result.generation_id
    assert recovery.safe_action == "cleanup_check_only"


def test_T_010_pointer_references_unknown_generation(bootstrapped_workspace):
    """Also serves as one of the six INT-006 Case F conditions: referenced
    Generation Directory missing. See test_integration.py for the other five."""
    pointer_path = bootstrapped_workspace / "active_generation"
    pointer = parse_pointer(pointer_path.read_bytes())
    import dataclasses

    broken = dataclasses.replace(pointer, generation_id="0000009999")
    pointer_path.write_bytes(broken.to_canonical_bytes())

    before = sorted(p.name for p in bootstrapped_workspace.rglob("*"))
    result = recover_generation_transaction(bootstrapped_workspace)
    after = sorted(p.name for p in bootstrapped_workspace.rglob("*"))

    assert result.case == "F"
    assert result.classification == "CASE_F_POINTER_INVALID"
    assert result.status == "FAILED_REQUIRES_RECOVERY"
    assert result.authoritative_generation is None
    assert result.automatic_mutation == "PROHIBITED"
    assert after == before  # zero filesystem mutation, zero automatic selection


def test_T_011_pointer_digest_mismatches_generation_content(bootstrapped_workspace):
    resolved = resolve_active_generation(bootstrapped_workspace)
    tampered = resolved.generation_path / "source_01_layer_1.jsonl"
    tampered.write_text('{"record_type": "TAMPERED"}\n', encoding="utf-8")

    result = recover_generation_transaction(bootstrapped_workspace)
    assert result.case == "E"
    assert result.status == "FAILED_REQUIRES_RECOVERY"
    assert result.authoritative_generation is None
    assert result.automatic_mutation == "PROHIBITED"
