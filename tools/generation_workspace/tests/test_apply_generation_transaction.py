"""AGT-001..AGT-045: apply_generation_transaction (WP-OGR-02) Runtime tests.

Every test in this file uses only tmp_path-rooted synthetic workspaces and
synthetic Generation Source directories; the production WP-CLAIM-EXIT
workspace is never referenced. Generation Source content for positive
tests is built by copying the bootstrapped workspace's own current active
generation into a fresh directory (mirroring
tests/test_recovery.py::_populate_staging_from_current) -- this is
already a self-consistent Manifest/State/Candidate/source-checkpoint set
because it is a byte-for-byte copy of a real, previously published
generation, so it satisfies both the new generation_source_expected_files
inventory and the existing (unchanged) commit-time
verify_physical_inventory check.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest
from generation_workspace import cli
from generation_workspace import generation_source as gs
from generation_workspace.digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS
from generation_workspace.mutation_guard import (
    AUTHORIZATION_SCHEMA_VERSION_V2,
    FAILED_REQUIRES_RECOVERY,
    ExpectedFile,
    GenerationSourceExpectedFile,
    MutationAuthorization,
)
from generation_workspace.resolver import resolve_active_generation
from generation_workspace.transaction import (
    _next_generation_id,
    apply_generation_transaction,
    begin_generation_transaction,
    recover_generation_transaction,
)

from .conftest import new_uuid

DIGEST_SCHEMA = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _generation_source_from_current(bootstrapped_workspace: Path, dest: Path) -> list:
    dest.mkdir(parents=True)
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok, resolved.reason
    entries = []
    for entry in sorted(resolved.generation_path.iterdir(), key=lambda p: p.name):
        data = entry.read_bytes()
        (dest / entry.name).write_bytes(data)
        entries.append(
            GenerationSourceExpectedFile(
                relative_path=entry.name, byte_count=len(data), sha256=_sha256_bytes(data)
            )
        )
    return entries


def _build_v2_transaction_authorization(
    workspace_root: Path,
    transaction_id: str,
    generation_source_expected_files: list,
    *,
    apply: bool = True,
) -> MutationAuthorization:
    resolved = resolve_active_generation(workspace_root)
    assert resolved.ok, resolved.reason
    expected_files = [
        ExpectedFile(relative_path=p.name, sha256=_sha256_of(p))
        for p in sorted(resolved.generation_path.iterdir(), key=lambda p: p.name)
    ]
    return MutationAuthorization(
        authorization_schema_version=AUTHORIZATION_SCHEMA_VERSION_V2,
        authorization_id=new_uuid(),
        operation_scope="GENERATION_TRANSACTION",
        transaction_id=transaction_id,
        workspace_root=str(workspace_root.resolve()),
        apply=apply,
        source_generation_id=resolved.generation_id,
        source_generation_digest=resolved.pointer.generation_digest,
        target_generation_id=_next_generation_id(resolved.generation_id),
        expected_stable_file_count=len(expected_files),
        expected_files=expected_files,
        generation_source_expected_files=generation_source_expected_files,
    )


def _write_v2_authorization_file(path: Path, authorization: MutationAuthorization) -> None:
    obj = {
        "authorization_schema_version": authorization.authorization_schema_version,
        "authorization_id": authorization.authorization_id,
        "operation_scope": authorization.operation_scope,
        "transaction_id": authorization.transaction_id,
        "workspace_root": authorization.workspace_root,
        "apply": authorization.apply,
        "source_generation_id": authorization.source_generation_id,
        "source_generation_digest": authorization.source_generation_digest,
        "target_generation_id": authorization.target_generation_id,
        "expected_stable_file_count": authorization.expected_stable_file_count,
        "expected_files": [
            {"relative_path": f.relative_path, "sha256": f.sha256}
            for f in authorization.expected_files
        ],
        "generation_source_expected_files": [
            {"relative_path": e.relative_path, "byte_count": e.byte_count, "sha256": e.sha256}
            for e in authorization.generation_source_expected_files
        ],
    }
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


EXPECTED_RESULT_FIELDS = {
    "schema_version",
    "mode",
    "operation",
    "workspace_root_verified",
    "result",
    "error_code",
    "mutation_started",
    "filesystem_writes",
    "recovery_required",
    "authorization_id",
    "authorization_digest",
    "transaction_id",
    "source_generation_id",
    "source_generation_digest",
    "target_generation_id",
    "target_generation_digest",
    "preflight_status",
    "apply_requested",
}


# --- AGT-001: Success ------------------------------------------------------


def test_AGT_001_success_end_to_end(bootstrapped_workspace, tmp_path):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert result.ok, result.reason
    assert result.target_generation_id == authorization.target_generation_id
    assert result.target_generation_digest is not None
    assert result.mutation_started is True
    assert result.filesystem_writes > 0
    assert result.recovery_required is False

    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok
    assert resolved.generation_id == authorization.target_generation_id

    # Generation Source itself is never mutated.
    for entry in entries:
        assert (source_dir / entry.relative_path).read_bytes()


# --- GS-1: Root Safety -------------------------------------------------


def test_AGT_002_gs1_source_not_found(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "does_not_exist"
    entries = [GenerationSourceExpectedFile(relative_path="a.txt", byte_count=1, sha256="0" * 64)]
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_NOT_FOUND
    assert result.mutation_started is False
    assert result.filesystem_writes == 0


def test_AGT_003_gs1_source_not_directory(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_file = tmp_path / "not_a_dir.txt"
    source_file.write_text("x", encoding="utf-8")
    entries = [GenerationSourceExpectedFile(relative_path="a.txt", byte_count=1, sha256="0" * 64)]
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_file, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_NOT_DIRECTORY
    assert result.filesystem_writes == 0


def test_AGT_004_gs1_source_is_symlink(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    real_dir = tmp_path / "real_source"
    real_dir.mkdir()
    symlink_dir = tmp_path / "symlinked_source"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)
    entries = [GenerationSourceExpectedFile(relative_path="a.txt", byte_count=1, sha256="0" * 64)]
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, symlink_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_SYMLINK_REJECTED
    assert result.filesystem_writes == 0


def test_AGT_005_gs1_source_inside_workspace_root(bootstrapped_workspace):
    txn_id = new_uuid()
    inside = bootstrapped_workspace / "inside_source"
    inside.mkdir()
    entries = [GenerationSourceExpectedFile(relative_path="a.txt", byte_count=1, sha256="0" * 64)]
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, inside, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ROOT_POLICY_REJECTED
    assert result.filesystem_writes == 0


def test_AGT_006_gs1_source_inside_generations(bootstrapped_workspace):
    txn_id = new_uuid()
    inside = bootstrapped_workspace / "generations" / "not_a_real_generation_source"
    inside.mkdir()
    entries = [GenerationSourceExpectedFile(relative_path="a.txt", byte_count=1, sha256="0" * 64)]
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, inside, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ROOT_POLICY_REJECTED
    assert result.filesystem_writes == 0


# --- GS-2: Physical Entry Safety -----------------------------------------


def test_AGT_007_gs2_nested_directory_rejected(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    (source_dir / "nested_dir").mkdir()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ENTRY_REJECTED
    assert result.filesystem_writes == 0


def test_AGT_008_gs2_symlink_entry_rejected(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    real_extra = tmp_path / "real_extra.txt"
    real_extra.write_text("extra", encoding="utf-8")
    (source_dir / "symlinked_entry.txt").symlink_to(real_extra)
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ENTRY_REJECTED
    assert result.filesystem_writes == 0


def test_AGT_009_gs2_socket_entry_rejected(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "gsrc"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    sock_path = source_dir / "s.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
    except OSError as exc:
        pytest.skip(f"AF_UNIX socket not constructible in this environment: {exc}")
    try:
        authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
        result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)
        assert not result.ok
        assert result.error_code == gs.GENERATION_SOURCE_ENTRY_REJECTED
        assert result.filesystem_writes == 0
    finally:
        sock.close()


def test_AGT_010_gs2_fifo_entry_rejected(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    fifo_path = source_dir / "f.fifo"
    try:
        os.mkfifo(str(fifo_path))
    except (AttributeError, OSError) as exc:
        pytest.skip(f"FIFO not constructible in this environment: {exc}")
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ENTRY_REJECTED
    assert result.filesystem_writes == 0


def test_AGT_011_gs2_hardlink_nlink_rejected(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    first_entry_path = source_dir / entries[0].relative_path
    extra_link = tmp_path / "extra_hardlink_to_same_inode"
    try:
        os.link(str(first_entry_path), str(extra_link))
    except OSError as exc:
        pytest.skip(f"hard link not constructible in this environment: {exc}")
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ENTRY_REJECTED
    assert result.filesystem_writes == 0


# --- GS-3: Authorized Inventory Set ---------------------------------------


def test_AGT_012_gs3_missing_authorized_entry(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    (source_dir / entries[0].relative_path).unlink()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_INVENTORY_MISMATCH
    assert result.filesystem_writes == 0


def test_AGT_013_gs3_unexpected_entry(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    (source_dir / "unexpected_extra.txt").write_text("surprise", encoding="utf-8")
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_INVENTORY_MISMATCH
    assert result.filesystem_writes == 0


# --- GS-4: Authorized Content ---------------------------------------------


def test_AGT_014_gs4_byte_count_mismatch(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    target = source_dir / entries[0].relative_path
    target.write_bytes(target.read_bytes() + b"extra tail bytes")
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_CONTENT_MISMATCH
    assert result.filesystem_writes == 0


def test_AGT_015_gs4_sha256_mismatch_same_length(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    target = source_dir / entries[0].relative_path
    original = target.read_bytes()
    mutated = bytes((b ^ 0xFF) for b in original) if original else b"\x00"
    if len(mutated) != len(original):
        mutated = (mutated + b"\x00" * len(original))[: len(original)]
    target.write_bytes(mutated)
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_CONTENT_MISMATCH
    assert result.filesystem_writes == 0


# --- Error precedence ------------------------------------------------------


def test_AGT_016_gs2_masks_gs3(bootstrapped_workspace, tmp_path):
    """A GS-2 structural violation (nested directory) must fire even when a
    GS-3 inventory violation (an unexpected extra file) is also present."""
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    (source_dir / "nested_dir").mkdir()  # GS-2 violation
    (source_dir / "unexpected_extra.txt").write_text("x", encoding="utf-8")  # GS-3 violation
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_ENTRY_REJECTED


def test_AGT_017_gs3_masks_gs4(bootstrapped_workspace, tmp_path):
    """A GS-3 missing-entry violation must fire even when a GS-4 content
    violation is also present on a different, physically-present entry."""
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    assert len(entries) >= 2
    (source_dir / entries[0].relative_path).unlink()  # GS-3 violation: missing
    corrupted = source_dir / entries[1].relative_path
    corrupted.write_bytes(corrupted.read_bytes() + b"corruption")  # would-be GS-4 violation
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == gs.GENERATION_SOURCE_INVENTORY_MISMATCH


def test_AGT_018_gs_preflight_failure_precedes_transaction_begin(bootstrapped_workspace, tmp_path):
    """A Generation Source preflight failure must occur before
    begin_generation_transaction acquires the lock: no lock file, no
    staging directory, and no control transaction may be created."""
    txn_id = new_uuid()
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    (source_dir / entries[0].relative_path).unlink()  # guaranteed GS-3 failure
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    before = sorted(p.name for p in bootstrapped_workspace.iterdir())
    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)
    after = sorted(p.name for p in bootstrapped_workspace.iterdir())

    assert not result.ok
    assert result.filesystem_writes == 0
    assert after == before
    assert not (bootstrapped_workspace / ".execution_lock.json").exists()
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()


# --- Apply-time TOCTOU -----------------------------------------------------


def test_AGT_019_toctou_content_changed_before_materialize(bootstrapped_workspace, tmp_path):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    real_source = gs.validate_generation_source_preflight(
        source_dir, bootstrapped_workspace.resolve(), _fake_v2_auth(entries)
    )
    assert real_source == source_dir.resolve()

    # Mutate the source content *after* preflight succeeded but *before*
    # materialization -- simulating a race window.
    target = source_dir / entries[0].relative_path
    target.write_bytes(b"raced content, different length")

    staging_path = tmp_path / "fake_staging"
    staging_path.mkdir()
    materialize_result = gs.materialize_generation_source(
        real_source, staging_path, _fake_v2_auth(entries)
    )

    assert not materialize_result.ok
    assert "changed before materialization" in materialize_result.reason


def test_AGT_020_toctou_source_vanished_before_materialize(bootstrapped_workspace, tmp_path):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    real_source = gs.validate_generation_source_preflight(
        source_dir, bootstrapped_workspace.resolve(), _fake_v2_auth(entries)
    )
    (source_dir / entries[0].relative_path).unlink()

    staging_path = tmp_path / "fake_staging"
    staging_path.mkdir()
    materialize_result = gs.materialize_generation_source(
        real_source, staging_path, _fake_v2_auth(entries)
    )

    assert not materialize_result.ok
    assert "vanished before materialization" in materialize_result.reason


def test_AGT_021_verified_bytes_equal_materialized_bytes(bootstrapped_workspace, tmp_path):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    real_source = gs.validate_generation_source_preflight(
        source_dir, bootstrapped_workspace.resolve(), _fake_v2_auth(entries)
    )
    staging_path = tmp_path / "fake_staging"
    staging_path.mkdir()
    materialize_result = gs.materialize_generation_source(
        real_source, staging_path, _fake_v2_auth(entries)
    )

    assert materialize_result.ok, materialize_result.reason
    assert materialize_result.filesystem_writes == len(entries)
    for entry in entries:
        staged_bytes = (staging_path / entry.relative_path).read_bytes()
        source_bytes = (source_dir / entry.relative_path).read_bytes()
        assert staged_bytes == source_bytes
        assert _sha256_bytes(staged_bytes) == entry.sha256


def _fake_v2_auth(entries: list) -> MutationAuthorization:
    return MutationAuthorization(
        authorization_schema_version=AUTHORIZATION_SCHEMA_VERSION_V2,
        authorization_id=new_uuid(),
        operation_scope="GENERATION_TRANSACTION",
        transaction_id=new_uuid(),
        workspace_root="/irrelevant/for/this/unit-level/check",
        apply=True,
        source_generation_id="0000000001",
        source_generation_digest="a" * 64,
        target_generation_id="0000000002",
        expected_stable_file_count=0,
        expected_files=[],
        generation_source_expected_files=entries,
    )


# --- Mutation-phase failure & failure preservation ------------------------


def test_AGT_022_materialize_failure_preserves_state_for_recovery(
    bootstrapped_workspace, tmp_path, monkeypatch
):
    import generation_workspace.transaction as transaction_module

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    def _failing_materialize(real_source, staging_path, mutation_authorization):
        # Simulate one file successfully materialized, then a failure.
        first = mutation_authorization.generation_source_expected_files[0]
        source_bytes = (real_source / first.relative_path).read_bytes()
        (staging_path / first.relative_path).write_bytes(source_bytes)
        return gs.MaterializeResult(
            ok=False, filesystem_writes=1, reason="simulated materialization failure"
        )

    monkeypatch.setattr(
        transaction_module.gs, "materialize_generation_source", _failing_materialize
    )

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == FAILED_REQUIRES_RECOVERY
    assert result.mutation_started is True
    assert result.recovery_required is True
    assert result.filesystem_writes >= 1

    # Failure preservation: lock, control transaction (PREPARING), and
    # partial staging remain exactly as begin_generation_transaction and
    # the (simulated) partial materialization left them. No cleanup.
    assert (bootstrapped_workspace / ".execution_lock.json").exists()
    control = json.loads((bootstrapped_workspace / ".control_transaction.json").read_text("utf-8"))
    assert control["state"] == "PREPARING"
    staging_dirs = list(bootstrapped_workspace.glob(".staging-gen-*"))
    assert len(staging_dirs) == 1
    assert list(staging_dirs[0].iterdir())

    # Pointer must remain the source generation; target must not be published.
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.generation_id == authorization.source_generation_id
    assert not (
        bootstrapped_workspace / "generations" / f"gen-{authorization.target_generation_id}"
    ).exists()

    recovery = recover_generation_transaction(bootstrapped_workspace)
    assert recovery.case == "A"
    assert recovery.status == "NOT_COMMITTED"
    assert recovery.safe_action == "RESTART_FROM_STAGING"
    assert recovery.authoritative_generation == authorization.source_generation_id


# --- Machine Result Contract (18 fields) -----------------------------------


def test_AGT_023_error_code_not_result_value(bootstrapped_workspace, tmp_path, monkeypatch):
    """FAILED_REQUIRES_RECOVERY is an error_code, never a `result` value."""
    import generation_workspace.transaction as transaction_module

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    def _failing_materialize(real_source, staging_path, mutation_authorization):
        return gs.MaterializeResult(ok=False, filesystem_writes=0, reason="simulated")

    monkeypatch.setattr(
        transaction_module.gs, "materialize_generation_source", _failing_materialize
    )

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)
    assert result.error_code == FAILED_REQUIRES_RECOVERY
    assert not hasattr(result, "result")  # ApplyTransactionResult has no `result` field at all


def test_AGT_024_cli_recovery_required_failure_exact_profile(
    bootstrapped_workspace, tmp_path, monkeypatch, capsys
):
    import generation_workspace.transaction as transaction_module

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    auth_path = tmp_path / "auth.json"
    _write_v2_authorization_file(auth_path, authorization)

    def _failing_materialize(real_source, staging_path, mutation_authorization):
        return gs.MaterializeResult(ok=False, filesystem_writes=0, reason="simulated")

    monkeypatch.setattr(
        transaction_module.gs, "materialize_generation_source", _failing_materialize
    )

    code = cli.main(
        [
            "apply-generation-transaction",
            "--workspace-root",
            str(bootstrapped_workspace),
            "--authorization-file",
            str(auth_path),
            "--generation-source-directory",
            str(source_dir),
            "--apply",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_FAILED_REQUIRES_RECOVERY
    assert code == 8
    assert output["result"] == "FAILURE"
    assert output["error_code"] == "FAILED_REQUIRES_RECOVERY"
    assert output["preflight_status"] == "PASS"
    assert output["apply_requested"] is True
    assert output["mutation_started"] is True
    assert output["recovery_required"] is True
    assert output["target_generation_digest"] is None
    assert set(output.keys()) == EXPECTED_RESULT_FIELDS


def test_AGT_025_cli_preflight_success_exact_profile(bootstrapped_workspace, tmp_path, capsys):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    auth_path = tmp_path / "auth.json"
    _write_v2_authorization_file(auth_path, authorization)

    code = cli.main(
        [
            "apply-generation-transaction",
            "--workspace-root",
            str(bootstrapped_workspace),
            "--authorization-file",
            str(auth_path),
            "--generation-source-directory",
            str(source_dir),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_OK
    assert output["schema_version"] == "GENERATION_WORKSPACE_COMMAND_RESULT-v1"
    assert output["mode"] == "preflight"
    assert output["operation"] == "GENERATION_TRANSACTION"
    assert output["result"] == "PREFLIGHT_PASS"
    assert output["error_code"] is None
    assert output["preflight_status"] == "PASS"
    assert output["apply_requested"] is False
    assert output["mutation_started"] is False
    assert output["filesystem_writes"] == 0
    assert output["recovery_required"] is False
    assert output["target_generation_digest"] is None
    assert set(output.keys()) == EXPECTED_RESULT_FIELDS
    # Preflight must create zero filesystem artifacts.
    assert not (bootstrapped_workspace / ".execution_lock.json").exists()


def test_AGT_026_cli_apply_success_exact_profile(bootstrapped_workspace, tmp_path, capsys):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    auth_path = tmp_path / "auth.json"
    _write_v2_authorization_file(auth_path, authorization)

    code = cli.main(
        [
            "apply-generation-transaction",
            "--workspace-root",
            str(bootstrapped_workspace),
            "--authorization-file",
            str(auth_path),
            "--generation-source-directory",
            str(source_dir),
            "--apply",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_OK
    assert output["mode"] == "apply"
    assert output["operation"] == "GENERATION_TRANSACTION"
    assert output["result"] == "APPLY_COMPLETE"
    assert output["error_code"] is None
    assert output["preflight_status"] == "PASS"
    assert output["apply_requested"] is True
    assert output["mutation_started"] is True
    assert output["recovery_required"] is False
    assert output["filesystem_writes"] > 0
    assert output["target_generation_id"] == authorization.target_generation_id
    assert output["target_generation_digest"] is not None
    assert set(output.keys()) == EXPECTED_RESULT_FIELDS


def test_AGT_027_existing_bootstrap_output_schema_unchanged(flat_workspace, tmp_path, capsys):
    from .conftest import build_bootstrap_authorization

    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    auth_path = tmp_path / "bootstrap_auth.json"
    obj = {
        "authorization_schema_version": authorization.authorization_schema_version,
        "authorization_id": authorization.authorization_id,
        "operation_scope": authorization.operation_scope,
        "transaction_id": authorization.transaction_id,
        "workspace_root": authorization.workspace_root,
        "apply": authorization.apply,
        "source_generation_id": authorization.source_generation_id,
        "source_generation_digest": authorization.source_generation_digest,
        "target_generation_id": authorization.target_generation_id,
        "expected_stable_file_count": authorization.expected_stable_file_count,
        "expected_files": [
            {"relative_path": f.relative_path, "sha256": f.sha256}
            for f in authorization.expected_files
        ],
    }
    auth_path.write_text(json.dumps(obj), encoding="utf-8")

    cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    # Existing Bootstrap result schema is untouched by WP-OGR-02: still the
    # original 15 keys, no `schema_version`, no `recovery_required`.
    assert "schema_version" not in output
    assert "recovery_required" not in output
    assert set(output.keys()) == {
        "mode",
        "operation",
        "authorization_id",
        "authorization_digest",
        "transaction_id",
        "workspace_root_verified",
        "baseline_file_count",
        "baseline_digest_status",
        "planned_generation_id",
        "preflight_status",
        "apply_requested",
        "mutation_started",
        "result",
        "error_code",
        "filesystem_writes",
    }


def test_AGT_028_cli_help_lists_new_subcommand():
    code = cli.main(["apply-generation-transaction", "--help"])
    # argparse --help exits via SystemExit inside build_parser's own
    # try/except, returning the parser's own exit code (0 for --help).
    assert code == 0


# --- Compatibility ----------------------------------------------------------


def test_AGT_029_v1_bootstrap_still_works(flat_workspace):
    from generation_workspace.bootstrap import bootstrap_generation_workspace

    from .conftest import build_bootstrap_authorization

    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert result.ok, result.reason


def test_AGT_030_existing_transaction_apis_unchanged_signature_usable(bootstrapped_workspace):
    from .conftest import build_transaction_authorization

    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    # Crash before staging population; existing recover_generation_transaction
    # unchanged behavior (Case A) still holds.
    recovery = recover_generation_transaction(bootstrapped_workspace)
    assert recovery.case == "A"


# --- Manifest/State compatibility (readiness-review observation) ----------


def test_AGT_031_generation_source_matches_but_manifest_state_disagree(
    bootstrapped_workspace, tmp_path
):
    """generation_source_expected_files can be an exact, verified match for
    the physical Generation Source directory while the Manifest/State
    content declares a different inventory. Existing (unchanged)
    verify_physical_inventory at commit time must still reject this."""
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)

    manifest_path = source_dir / "workspace_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    # Declare an extra SOURCE_CHECKPOINT file that does not physically exist.
    manifest["file_classification"]["source_99_layer_99.jsonl"] = "SOURCE_CHECKPOINT"
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)

    # generation_source_expected_files must be recomputed to match the
    # rewritten manifest file's new bytes/hash (GS-4 checks against this).
    is_manifest = lambda rel: rel == "workspace_manifest.json"  # noqa: E731
    entries = [
        GenerationSourceExpectedFile(
            relative_path=e.relative_path,
            byte_count=len(manifest_bytes) if is_manifest(e.relative_path) else e.byte_count,
            sha256=(_sha256_bytes(manifest_bytes) if is_manifest(e.relative_path) else e.sha256),
        )
        for e in entries
    ]

    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    # GS-1..GS-4 all pass (exact match against generation_source_expected_files).
    # The existing, unchanged Manifest/State-driven verify_physical_inventory
    # at commit time is what actually rejects this transaction, *before* any
    # commit-phase write (verify_physical_inventory(staging) runs before the
    # first control-transaction rewrite), so the commit phase contributes 0.
    assert not result.ok
    assert result.mutation_started is True
    assert result.recovery_required is True
    # CF-1: pre-publication commit failure -- Human-frozen fallback:
    # recovery_required=true with no more-specific accepted code from
    # commit_generation_transaction (this branch's CommitResult.error_code
    # is None) must surface as FAILED_REQUIRES_RECOVERY, never null.
    assert result.error_code == FAILED_REQUIRES_RECOVERY
    assert result.filesystem_writes == BEGIN_TRANSACTION_WRITES + len(entries) + 0

    # Preservation: pointer unchanged, target never published, staging
    # (fully materialized, just failing the separate Manifest/State check)
    # still present untouched.
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.generation_id == authorization.source_generation_id
    assert not (
        bootstrapped_workspace / "generations" / f"gen-{authorization.target_generation_id}"
    ).exists()
    staging_dirs = list(bootstrapped_workspace.glob(".staging-gen-*"))
    assert len(staging_dirs) == 1


# --- filesystem_writes accounting & source read-only behavior -------------
#
# BEGIN_TRANSACTION_WRITES / COMMIT_PHASE_WRITES document the exact,
# frozen operation sequence (begin_generation_transaction and
# commit_generation_transaction respectively) so the expected totals below
# are calculated from that sequence, not copied from whatever the
# implementation currently returns (WP-OGR-02 Runtime Implementation
# Review finding: the previous version of this test asserted `3 +
# len(entries) + 0`, silently encoding commit_generation_transaction's
# then-defective always-0 accounting instead of the frozen "successful
# logical filesystem mutations inside workspace_root" semantic).

# begin_generation_transaction: lock create, staging mkdir, control
# transaction create (PREPARING) = 3.
BEGIN_TRANSACTION_WRITES = 3

# commit_generation_transaction, happy path, assuming generations/ already
# exists (true for every bootstrapped_workspace fixture in this file):
#   1. control-transaction rewrite -> VERIFIED
#   2. staging -> published rename
#   3. control-transaction rewrite -> GENERATION_PUBLISHED
#   4. pointer .tmp file write
#   5. control-transaction rewrite -> COMMITTING_POINTER
#   6. pointer rename (the actual commit point)
#   7. control-transaction rewrite -> COMMITTED
#   8. control-transaction file unlink
#   9. execution-lock file unlink
COMMIT_PHASE_WRITES = 9

# Up to and including the pointer rename (the commit point) but before the
# COMMITTED rewrite / cleanup unlinks -- used by the post-pointer-commit
# failure test below.
COMMIT_PHASE_WRITES_THROUGH_POINTER_COMMIT = 6


def test_AGT_032_filesystem_writes_composed_from_subresults(bootstrapped_workspace, tmp_path):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert result.ok, result.reason
    assert result.filesystem_writes == (
        BEGIN_TRANSACTION_WRITES + len(entries) + COMMIT_PHASE_WRITES
    )

    # Cross-check the count against independently observed filesystem
    # state, rather than trusting the number in isolation.
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok
    assert resolved.generation_id == authorization.target_generation_id
    assert (
        bootstrapped_workspace / "generations" / f"gen-{authorization.target_generation_id}"
    ).is_dir()
    assert not list(bootstrapped_workspace.glob(".staging-gen-*"))
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()
    assert not (bootstrapped_workspace / ".execution_lock.json").exists()


def test_AGT_034_post_pointer_commit_failure_accounting(
    bootstrapped_workspace, tmp_path, monkeypatch
):
    """A failure reported by the *existing, unmodified* post-commit
    verification step (verify_active_generation) after the pointer has
    already been switched must still report the commit-phase writes that
    genuinely happened, including the pointer rename itself -- not silently
    drop them because the overall command result is FAILURE."""
    import generation_workspace.transaction as transaction_module
    from generation_workspace.resolver import VerifyResult

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    def _failing_verify(workspace_root):
        return VerifyResult(ok=False, reason="simulated post-commit failure")

    # verify_active_generation is called by commit_generation_transaction
    # exactly once, strictly after the real pointer rename has already
    # happened; nothing earlier in the flow (begin, GS preflight, Phase B)
    # calls it, so this monkeypatch cannot mask an earlier stage.
    monkeypatch.setattr(transaction_module, "verify_active_generation", _failing_verify)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.mutation_started is True
    assert result.recovery_required is True
    # CF-2: post-pointer-commit failure -- same Human-frozen fallback as
    # CF-1 (this branch's CommitResult.error_code is also None).
    assert result.error_code == FAILED_REQUIRES_RECOVERY
    assert result.filesystem_writes == (
        BEGIN_TRANSACTION_WRITES + len(entries) + COMMIT_PHASE_WRITES_THROUGH_POINTER_COMMIT
    )

    # Independently prove the pointer really did switch (not inferred from
    # the count alone): read the real, unmocked active_generation pointer.
    from generation_workspace.model import parse_pointer

    pointer_bytes = (bootstrapped_workspace / "active_generation").read_bytes()
    pointer = parse_pointer(pointer_bytes)
    assert pointer.generation_id == authorization.target_generation_id

    # The later, not-executed operations (COMMITTED rewrite, control-txn
    # unlink, lock unlink) correctly did not happen.
    assert (bootstrapped_workspace / ".control_transaction.json").exists()
    assert (bootstrapped_workspace / ".execution_lock.json").exists()


def test_AGT_033_generation_source_never_mutated(bootstrapped_workspace, tmp_path):
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    before = {e.relative_path: (source_dir / e.relative_path).read_bytes() for e in entries}
    before_mode = {e.relative_path: (source_dir / e.relative_path).stat().st_mode for e in entries}

    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)
    assert result.ok, result.reason

    for e in entries:
        assert (source_dir / e.relative_path).read_bytes() == before[e.relative_path]
        assert (source_dir / e.relative_path).stat().st_mode == before_mode[e.relative_path]
    assert sorted(p.name for p in source_dir.iterdir()) == sorted(before.keys())


# --- Source-object TOCTOU safety (WP-OGR-02 Runtime Implementation Review
# finding: lstat()-then-read_bytes() on a path is not a proof that the
# bytes read came from the object that was checked). These tests exercise
# generation_source.materialize_generation_source's *trusted-open* path
# directly (gs._secure_read_trusted_bytes), proving the fix by behavior
# rather than by asserting the helper merely called itself. ---------------


def test_AGT_035_toctou_symlink_substitution_after_preflight_rejected(
    bootstrapped_workspace, tmp_path
):
    """An entry that was a safe regular file at GS-1..GS-4 preflight time,
    then replaced with a symlink before materialization, must not have its
    symlink target followed and read -- it must be safely rejected."""
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    real_source = gs.validate_generation_source_preflight(
        source_dir, bootstrapped_workspace.resolve(), _fake_v2_auth(entries)
    )

    target_entry = entries[0]
    target_path = source_dir / target_entry.relative_path
    secret_path = tmp_path / "outside_secret.txt"
    secret_path.write_bytes(b"attacker-controlled content that must never be read")
    target_path.unlink()
    target_path.symlink_to(secret_path)

    staging_path = tmp_path / "fake_staging"
    staging_path.mkdir()
    materialize_result = gs.materialize_generation_source(
        real_source, staging_path, _fake_v2_auth(entries)
    )

    assert not materialize_result.ok
    assert "changed or became unsafe" in materialize_result.reason
    # The forbidden symlink target's content must never reach staging.
    assert not (staging_path / target_entry.relative_path).exists()


def test_AGT_036_toctou_hardlink_substitution_at_materialize_rejected(
    bootstrapped_workspace, tmp_path
):
    """An entry that was nlink==1 at GS-1..GS-4 preflight time, then given
    an extra hard link (nlink becomes 2) before materialization, must be
    rejected by the fstat check on the actually-opened descriptor."""
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    real_source = gs.validate_generation_source_preflight(
        source_dir, bootstrapped_workspace.resolve(), _fake_v2_auth(entries)
    )

    target_entry = entries[0]
    target_path = source_dir / target_entry.relative_path
    extra_link = tmp_path / "extra_hardlink_created_after_preflight"
    try:
        os.link(str(target_path), str(extra_link))
    except OSError as exc:
        pytest.skip(f"hard link not constructible in this environment: {exc}")

    staging_path = tmp_path / "fake_staging"
    staging_path.mkdir()
    materialize_result = gs.materialize_generation_source(
        real_source, staging_path, _fake_v2_auth(entries)
    )

    assert not materialize_result.ok
    assert "changed or became unsafe" in materialize_result.reason
    assert not (staging_path / target_entry.relative_path).exists()


def test_AGT_037_toctou_secure_read_rejects_symlink_via_no_follow_open(tmp_path):
    """Directly exercise the trusted-open primitive: opening a path whose
    final component is currently a symlink must fail closed (ELOOP via
    O_NOFOLLOW), never silently follow it and return the target's bytes."""
    real_target = tmp_path / "real_target.txt"
    real_target.write_bytes(b"the target file's real content")
    symlink_path = tmp_path / "entry_that_is_a_symlink"
    symlink_path.symlink_to(real_target)

    with pytest.raises(OSError):
        gs._secure_read_trusted_bytes(symlink_path)


def test_AGT_038_secure_read_returns_bytes_from_the_verified_descriptor(tmp_path):
    """Positive case: for a genuinely safe regular, single-link file, the
    trusted-open path must return exactly that file's bytes -- proving the
    descriptor that was fstat-verified is the same one supplying bytes."""
    real_file = tmp_path / "regular_file.txt"
    content = b"exact bytes that must come back unchanged"
    real_file.write_bytes(content)

    data = gs._secure_read_trusted_bytes(real_file)

    assert data == content


# --- begin_generation_transaction failure accounting (WP-OGR-02 Runtime
# Implementation Repair Review 2nd-pass finding: begin's own failure
# branches after lock acquisition unconditionally reported
# filesystem_writes=0 even though lock-create-then-release is two real
# successful mutations, not zero -- "a mutation later cleaned up is not
# the same as a mutation that never happened"). -----------------------

# begin_generation_transaction, Phase-B-failure-after-lock-acquisition
# path: lock create (1) + lock release (1) = 2, when release succeeds.
BEGIN_FAILURE_AFTER_LOCK_WRITES = 2


def test_AGT_039_bf1_phase_b_failure_after_lock_acquisition(bootstrapped_workspace, tmp_path):
    """A Phase B failure (source_generation_digest mismatch) occurring
    after the lock has already been successfully created and cleanly
    released must report both successful mutations, report
    mutation_started=true, and leave the workspace in a state that
    genuinely requires no recovery (no control-transaction file was ever
    created on this path)."""
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    # Corrupt source_generation_digest only: passes Phase A (workspace
    # root / apply / transaction_id / target_generation_id are all still
    # valid), so the lock is genuinely acquired, then fails inside
    # validate_phase_b_transaction's digest comparison.
    bad_authorization = dataclasses.replace(
        authorization, source_generation_digest="0" * 64
    )

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, bad_authorization)

    assert not result.ok
    assert result.mutation_started is True
    assert result.recovery_required is False
    assert result.filesystem_writes == BEGIN_FAILURE_AFTER_LOCK_WRITES

    # Cross-check against real filesystem state, not just the counter.
    assert not (bootstrapped_workspace / ".execution_lock.json").exists()
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()
    assert not list(bootstrapped_workspace.glob(".staging-gen-*"))
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.generation_id == authorization.source_generation_id


def test_AGT_040_bf2_staging_target_collision_after_lock_acquisition(
    bootstrapped_workspace, tmp_path
):
    """A pre-existing staging directory for the computed target
    generation (TARGET_GENERATION_ALREADY_EXISTS), discovered only after
    the lock has already been acquired and Phase B has already passed,
    must also report both successful mutations (lock create + lock
    release), not zero."""
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok
    target_id = _next_generation_id(resolved.generation_id)
    colliding_staging = bootstrapped_workspace / f".staging-gen-{target_id}"
    colliding_staging.mkdir()

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    assert authorization.target_generation_id == target_id

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.mutation_started is True
    assert result.recovery_required is False
    assert result.filesystem_writes == BEGIN_FAILURE_AFTER_LOCK_WRITES

    assert not (bootstrapped_workspace / ".execution_lock.json").exists()
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()
    # The pre-existing colliding directory itself is untouched (begin
    # never deletes anything it did not itself create).
    assert colliding_staging.is_dir()


def test_AGT_041_bf3_zero_mutation_begin_failure(bootstrapped_workspace, tmp_path):
    """A begin failure occurring entirely before lock acquisition
    (mismatched target_generation_id, detected during Phase A) must be a
    true zero-mutation failure."""
    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)
    bad_authorization = dataclasses.replace(authorization, target_generation_id="9999999999")

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, bad_authorization)

    assert not result.ok
    assert result.mutation_started is False
    assert result.recovery_required is False
    assert result.filesystem_writes == 0
    assert not (bootstrapped_workspace / ".execution_lock.json").exists()


# --- F2: failure after publication, before pointer commit --------------
#
# WP-OGR-02 Repair-3: commit_generation_transaction now contains expected
# operational OSError raised anywhere in its accepted commit-phase
# filesystem-mutation sequence (a try/except OSError boundary placed
# immediately after existing validation/typed-return processing), rather
# than letting it escape uncaught as it previously did. F2 (publication
# already succeeded, pointer not yet switched) is exercised via the same
# monkeypatchable seam (fsync_file, called immediately after
# `pointer_tmp.write_bytes(...)`) the prior Repair Review identified, but
# now asserts the frozen structured-failure contract instead of a raw
# escaping exception.

# commit_generation_transaction, F2 window: VERIFIED write, publish
# rename, GENERATION_PUBLISHED write, pointer .tmp write have all
# already succeeded by the time fsync_file(active_generation.tmp) is
# called and fails -- exactly the worked example in the Repair-3
# instruction (Section 17).
COMMIT_PHASE_WRITES_THROUGH_POINTER_TMP_WRITE = 4


def _install_failing_fsync_for_pointer_tmp(monkeypatch, transaction_module):
    orig_fsync_file = transaction_module.fsync_file

    def _failing_fsync_file(path):
        if Path(path).name == "active_generation.tmp":
            raise OSError("simulated fsync failure between publication and pointer commit")
        return orig_fsync_file(path)

    monkeypatch.setattr(transaction_module, "fsync_file", _failing_fsync_file)


def test_AGT_042_f2_post_publication_pre_pointer_failure_is_contained(
    bootstrapped_workspace, tmp_path, monkeypatch
):
    """F2 acceptance matrix: the operational OSError must not escape: the
    command must resolve to the frozen structured recovery-required
    failure, with exact accounting and every applicable state item
    preserved untouched by the handler itself."""
    import generation_workspace.transaction as transaction_module

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    _install_failing_fsync_for_pointer_tmp(monkeypatch, transaction_module)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == FAILED_REQUIRES_RECOVERY
    assert result.mutation_started is True
    assert result.recovery_required is True
    assert result.target_generation_digest is None
    assert result.filesystem_writes == (
        BEGIN_TRANSACTION_WRITES + len(entries) + COMMIT_PHASE_WRITES_THROUGH_POINTER_TMP_WRITE
    )

    # F2 Handler-Induced State Delta = 0: everything genuinely completed
    # before the injected failure remains exactly as it was left; nothing
    # the handler itself might have done (rollback/cleanup/retry) has
    # occurred.
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.generation_id == authorization.source_generation_id  # pointer NOT switched
    assert (
        bootstrapped_workspace / "generations" / f"gen-{authorization.target_generation_id}"
    ).is_dir()  # target genuinely published
    assert (bootstrapped_workspace / ".execution_lock.json").exists()  # lock preserved
    control = json.loads((bootstrapped_workspace / ".control_transaction.json").read_text("utf-8"))
    assert control["state"] == "GENERATION_PUBLISHED"  # last successfully written state, untouched
    # pointer .tmp was written before the injected fsync failure, so it
    # must still be present (no cleanup of it occurred).
    assert (bootstrapped_workspace / "active_generation.tmp").exists()
    # Already renamed away to `generations/`; untouched by the handler.
    assert not list(bootstrapped_workspace.glob(".staging-gen-*"))


def test_AGT_043_generic_operational_containment_at_publish_rename(
    bootstrapped_workspace, tmp_path, monkeypatch
):
    """Containment is not tied to the exact F2 helper/location: an OSError
    from a *different* operation (the staging -> published rename, much
    earlier in the same accepted commit-phase mutation boundary) must be
    contained the same way."""
    import generation_workspace.transaction as transaction_module

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    orig_rename = transaction_module.os.rename

    def _failing_rename(src, dst):
        if ".staging-gen-" in str(src):
            raise OSError("simulated rename failure at staging publish")
        return orig_rename(src, dst)

    monkeypatch.setattr(transaction_module.os, "rename", _failing_rename)

    result = apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)

    assert not result.ok
    assert result.error_code == FAILED_REQUIRES_RECOVERY
    assert result.mutation_started is True
    assert result.recovery_required is True
    assert result.target_generation_digest is None
    # Only the VERIFIED control-transaction write succeeded before the
    # (now-failing) publish rename itself.
    assert result.filesystem_writes == BEGIN_TRANSACTION_WRITES + len(entries) + 1
    assert not (
        bootstrapped_workspace / "generations" / f"gen-{authorization.target_generation_id}"
    ).exists()
    assert list(bootstrapped_workspace.glob(".staging-gen-*"))  # staging never renamed away


def test_AGT_044_programming_error_not_swallowed(bootstrapped_workspace, tmp_path, monkeypatch):
    """The containment boundary catches only OSError. An unrelated
    programming defect (TypeError) raised inside the same boundary must
    continue to propagate, not be silently reclassified as
    FAILED_REQUIRES_RECOVERY."""
    import generation_workspace.transaction as transaction_module

    source_dir = tmp_path / "generation_source"
    entries = _generation_source_from_current(bootstrapped_workspace, source_dir)
    txn_id = new_uuid()
    authorization = _build_v2_transaction_authorization(bootstrapped_workspace, txn_id, entries)

    def _raising_compute_digest(*args, **kwargs):
        raise TypeError("simulated programming defect, must not be swallowed")

    monkeypatch.setattr(transaction_module, "compute_generation_digest", _raising_compute_digest)

    with pytest.raises(TypeError, match="simulated programming defect"):
        apply_generation_transaction(bootstrapped_workspace, source_dir, authorization)


def test_AGT_045_direct_commit_result_contains_operational_failure(
    bootstrapped_workspace, tmp_path, monkeypatch
):
    """Containment happens inside commit_generation_transaction itself,
    not only as an artifact of the new orchestrator: calling it directly
    (mirroring tests/test_recovery.py's style) with the same injected
    fsync failure must return a structured CommitResult, never raise."""
    import shutil

    import generation_workspace.transaction as transaction_module

    from .conftest import build_transaction_authorization

    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    resolved = resolve_active_generation(bootstrapped_workspace)
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, begin.staging_path / entry.name)

    _install_failing_fsync_for_pointer_tmp(monkeypatch, transaction_module)

    from generation_workspace.transaction import commit_generation_transaction

    commit_result = commit_generation_transaction(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, authorization
    )

    assert commit_result.ok is False
    assert commit_result.error_code == FAILED_REQUIRES_RECOVERY
    assert commit_result.filesystem_writes == COMMIT_PHASE_WRITES_THROUGH_POINTER_TMP_WRITE
