"""Transaction control: begin / commit / recover.

Commit ordering (Corrected, per Digest and Publishing Correction Addendum):
Generation content (including Manifest/State final values such as the
appended Control Action) is fully finalized *inside Staging*, before the
Staging directory is renamed to its published Generation name. After that
rename, Generation content is immutable; only the Active Generation Pointer
switch remains.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import mutation_guard as guard
from .digest import compute_generation_digest
from .durability import assert_same_filesystem, fsync_dir, fsync_file
from .inventory import InventoryMismatchError, verify_physical_inventory
from .model import (
    POINTER_SCHEMA_VERSION,
    Pointer,
    generation_directory_name,
    is_valid_uuid,
)
from .resolver import (
    ACTIVE_GENERATION_FILENAME,
    GENERATIONS_DIRNAME,
    resolve_active_generation,
    verify_active_generation,
)

CONTROL_TRANSACTION_FILENAME = ".control_transaction.json"
LOCK_FILENAME = ".execution_lock.json"
CONTROL_TRANSACTION_PROTOCOL_VERSION = "WP-CLAIM-EXIT-CONTROL-TRANSACTION-v1"

STATE_PREPARING = "PREPARING"
STATE_STAGED = "STAGED"
STATE_VERIFIED = "VERIFIED"
STATE_GENERATION_PUBLISHED = "GENERATION_PUBLISHED"
STATE_COMMITTING_POINTER = "COMMITTING_POINTER"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"


class TransactionError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _staging_path(workspace_root: Path, generation_id: str) -> Path:
    return workspace_root / f".staging-gen-{generation_id}"


def _next_generation_id(current: str) -> str:
    return str(int(current) + 1).zfill(10)


def _write_control_transaction(workspace_root: Path, payload: dict) -> None:
    path = workspace_root / CONTROL_TRANSACTION_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    fsync_file(tmp)
    os.replace(tmp, path)


def _read_control_transaction(workspace_root: Path) -> dict | None:
    path = workspace_root / CONTROL_TRANSACTION_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class BeginResult:
    ok: bool
    staging_path: Path | None = None
    source_generation: str | None = None
    target_generation: str | None = None
    transaction_id: str | None = None
    reason: str = ""
    error_code: str | None = None
    filesystem_writes: int = 0


def begin_generation_transaction(
    workspace_root: Path,
    transaction_id: str,
    mutation_authorization: guard.MutationAuthorization | None = None,
) -> BeginResult:
    # --- Phase A: no filesystem writes may occur before this returns. ---
    try:
        real_root = guard.validate_phase_a(
            mutation_authorization,
            expected_operation_scope=guard.OPERATION_SCOPE_TRANSACTION,
            workspace_root_arg=str(workspace_root),
            transaction_id_arg=transaction_id,
        )
    except guard.GuardRejection as rej:
        return BeginResult(
            ok=False, reason=rej.message, error_code=rej.error_code, filesystem_writes=0
        )
    assert mutation_authorization is not None
    workspace_root = real_root

    if not is_valid_uuid(transaction_id):
        return BeginResult(
            ok=False,
            reason=f"invalid transaction_id: {transaction_id!r}",
            error_code=guard.AUTHORIZATION_SCHEMA_INVALID,
        )

    resolved = resolve_active_generation(workspace_root)
    if not resolved.ok:
        return BeginResult(
            ok=False,
            reason=(
                "begin_generation_transaction requires an existing active_generation "
                f"(use bootstrap_generation_workspace first): {resolved.reason}"
            ),
            error_code=guard.SOURCE_GENERATION_MISMATCH,
        )
    assert resolved.generation_id is not None
    source_id = resolved.generation_id
    target_id = _next_generation_id(source_id)

    if mutation_authorization.target_generation_id != target_id:
        return BeginResult(
            ok=False,
            reason=(
                f"authorization target_generation_id "
                f"{mutation_authorization.target_generation_id!r} "
                f"does not match the expected next generation {target_id!r}"
            ),
            error_code=guard.AUTHORIZATION_OPERATION_MISMATCH,
        )

    control_path = workspace_root / CONTROL_TRANSACTION_FILENAME
    if control_path.exists():
        return BeginResult(
            ok=False,
            reason="a control transaction is already in progress",
            error_code=guard.LOCK_ALREADY_EXISTS,
        )

    authorization_digest = mutation_authorization.digest()

    # --- Lock acquisition (exclusive create). ---
    try:
        lock_payload = guard.acquire_lock(
            workspace_root,
            lock_filename=LOCK_FILENAME,
            transaction_id=transaction_id,
            authorization_id=mutation_authorization.authorization_id,
            authorization_digest=authorization_digest,
        )
    except guard.GuardRejection as rej:
        return BeginResult(
            ok=False, reason=rej.message, error_code=rej.error_code, filesystem_writes=0
        )
    writes = 1

    def _release_owned_lock() -> guard.LockReleaseResult:
        return guard.release_lock_if_owned(
            workspace_root,
            lock_filename=LOCK_FILENAME,
            lock_id=lock_payload["lock_id"],
            transaction_id=transaction_id,
            authorization_id=mutation_authorization.authorization_id,
            authorization_digest=authorization_digest,
        )

    # --- Phase B: validation under lock. ---
    try:
        guard.validate_phase_b_transaction(workspace_root, mutation_authorization, source_id)
    except guard.GuardRejection as rej:
        release = _release_owned_lock()
        if release.error_code == guard.LOCK_OWNERSHIP_MISMATCH:
            return BeginResult(
                ok=False,
                reason=f"{rej.message} (lock ownership mismatch during cleanup)",
                error_code=guard.LOCK_OWNERSHIP_MISMATCH,
                filesystem_writes=0,
            )
        error_code = rej.error_code if release.released else guard.FAILED_REQUIRES_RECOVERY
        return BeginResult(ok=False, reason=rej.message, error_code=error_code, filesystem_writes=0)

    staging = _staging_path(workspace_root, target_id)
    if staging.exists():
        _release_owned_lock()
        return BeginResult(
            ok=False,
            reason=f"staging directory already exists: {staging}",
            error_code=guard.TARGET_GENERATION_ALREADY_EXISTS,
            filesystem_writes=0,
        )
    staging.mkdir(parents=True)
    writes += 1

    now = _now_iso()
    _write_control_transaction(
        workspace_root,
        {
            "protocol_version": CONTROL_TRANSACTION_PROTOCOL_VERSION,
            "transaction_id": transaction_id,
            "state": STATE_PREPARING,
            "source_generation": source_id,
            "target_generation": target_id,
            "target_generation_digest": None,
            "authorization_id": mutation_authorization.authorization_id,
            "authorization_digest": authorization_digest,
            "operation_scope": mutation_authorization.operation_scope,
            "created_at_utc": now,
            "updated_at_utc": now,
        },
    )
    writes += 1
    return BeginResult(
        ok=True,
        staging_path=staging,
        source_generation=source_id,
        target_generation=target_id,
        transaction_id=transaction_id,
        filesystem_writes=writes,
    )


@dataclass(frozen=True)
class CommitResult:
    ok: bool
    generation_id: str | None = None
    generation_digest: str | None = None
    reason: str = ""
    error_code: str | None = None
    filesystem_writes: int = 0


def commit_generation_transaction(
    workspace_root: Path,
    transaction_id: str,
    digest_schema_version: str,
    mutation_authorization: guard.MutationAuthorization | None = None,
) -> CommitResult:
    txn = _read_control_transaction(workspace_root)
    if txn is None:
        return CommitResult(
            ok=False,
            reason="no control transaction in progress",
            error_code=guard.FAILED_REQUIRES_RECOVERY,
        )
    if txn["transaction_id"] != transaction_id:
        return CommitResult(
            ok=False,
            reason="transaction_id does not match in-progress transaction",
            error_code=guard.AUTHORIZATION_TRANSACTION_MISMATCH,
        )

    # Authorization/Transaction Binding: the runtime authorization supplied
    # here must canonicalize to exactly the digest recorded at begin time.
    try:
        guard.validate_authorization_binding(
            mutation_authorization,
            expected_operation_scope=txn.get("operation_scope", guard.OPERATION_SCOPE_TRANSACTION),
            transaction_id_arg=transaction_id,
            bound_authorization_digest=txn.get("authorization_digest", ""),
            bound_source_generation_id=txn.get("source_generation"),
            bound_target_generation_id=str(txn.get("target_generation", "")),
        )
    except guard.GuardRejection as rej:
        return CommitResult(
            ok=False, reason=rej.message, error_code=rej.error_code, filesystem_writes=0
        )

    target_id = txn["target_generation"]
    staging = _staging_path(workspace_root, target_id)
    generations_dir = workspace_root / GENERATIONS_DIRNAME
    generations_dir.mkdir(exist_ok=True)
    published_path = generations_dir / generation_directory_name(target_id)

    assert_same_filesystem(workspace_root, staging, generations_dir)

    # Step: verify Staging content is complete and internally consistent
    # (Manifest/State must already carry their final values at this point).
    try:
        verify_physical_inventory(staging)
    except InventoryMismatchError as exc:
        return CommitResult(ok=False, reason=f"staging inventory invalid: {exc}")

    digest = compute_generation_digest(staging, target_id, digest_schema_version)
    txn["state"] = STATE_VERIFIED
    txn["target_generation_digest"] = digest
    txn["updated_at_utc"] = _now_iso()
    _write_control_transaction(workspace_root, txn)

    # fsync every staging file, then the staging directory itself.
    for entry in staging.iterdir():
        fsync_file(entry)
    fsync_dir(staging)

    # Publish: rename Staging -> canonical Generation name. Content is
    # immutable from this point on.
    os.rename(staging, published_path)
    fsync_dir(generations_dir)

    txn["state"] = STATE_GENERATION_PUBLISHED
    txn["updated_at_utc"] = _now_iso()
    _write_control_transaction(workspace_root, txn)

    pointer = Pointer(
        pointer_schema_version=POINTER_SCHEMA_VERSION,
        generation_id=target_id,
        generation_digest_schema_version=digest_schema_version,
        generation_digest=digest,
        transaction_id=transaction_id,
    )
    pointer_path = workspace_root / ACTIVE_GENERATION_FILENAME
    pointer_tmp = workspace_root / (ACTIVE_GENERATION_FILENAME + ".tmp")
    pointer_tmp.write_bytes(pointer.to_canonical_bytes())
    fsync_file(pointer_tmp)

    txn["state"] = STATE_COMMITTING_POINTER
    txn["updated_at_utc"] = _now_iso()
    _write_control_transaction(workspace_root, txn)

    os.rename(pointer_tmp, pointer_path)
    fsync_dir(workspace_root)

    verified = verify_active_generation(workspace_root)
    if not verified.ok or verified.generation_id != target_id:
        return CommitResult(ok=False, reason=f"post-commit verification failed: {verified.reason}")

    txn["state"] = STATE_COMMITTED
    txn["updated_at_utc"] = _now_iso()
    _write_control_transaction(workspace_root, txn)

    # Transient cleanup only; Generation content is never touched again.
    (workspace_root / CONTROL_TRANSACTION_FILENAME).unlink()
    lock_path = workspace_root / LOCK_FILENAME
    if lock_path.exists():
        lock_path.unlink()

    return CommitResult(ok=True, generation_id=target_id, generation_digest=digest)


CASE_F_POINTER_INVALID = "CASE_F_POINTER_INVALID"


@dataclass(frozen=True)
class RecoveryResult:
    case: str
    status: str
    authoritative_generation: str | None
    safe_action: str
    automatic_mutation: str = "PROHIBITED"
    classification: str | None = None


def recover_generation_transaction(workspace_root: Path) -> RecoveryResult:
    resolved = resolve_active_generation(workspace_root)
    txn = _read_control_transaction(workspace_root)

    if not resolved.ok:
        # Case F: Active Generation Pointer missing/empty/malformed, an
        # unsupported pointer/digest schema version, an invalid
        # generation_id, or a referenced Generation Directory that does not
        # exist. resolve_active_generation() is the single fail-closed gate
        # for all of these; none of them ever select or mutate anything.
        return RecoveryResult(
            case="F",
            status="FAILED_REQUIRES_RECOVERY",
            authoritative_generation=None,
            safe_action="manual_disposition_required",
            classification=CASE_F_POINTER_INVALID,
        )

    assert resolved.pointer is not None
    ptr_generation = resolved.generation_id
    digest_schema_version = resolved.pointer.generation_digest_schema_version

    if txn is None:
        verified = verify_active_generation(workspace_root)
        if verified.ok:
            return RecoveryResult(
                case="C",
                status="COMMITTED",
                authoritative_generation=ptr_generation,
                safe_action="cleanup_check_only",
            )
        return RecoveryResult(
            case="E",
            status="FAILED_REQUIRES_RECOVERY",
            authoritative_generation=None,
            safe_action="manual_disposition_required",
        )

    src: str | None = txn.get("source_generation")
    tgt: str | None = txn.get("target_generation")
    tgt_digest = txn.get("target_generation_digest")
    generations_dir = workspace_root / GENERATIONS_DIRNAME

    tgt_path = generations_dir / generation_directory_name(tgt) if tgt else None

    if ptr_generation == src and (tgt_path is None or not tgt_path.is_dir()):
        return RecoveryResult(
            case="A",
            status="NOT_COMMITTED",
            authoritative_generation=src,
            safe_action="RESTART_FROM_STAGING",
        )

    if ptr_generation == src and tgt_path is not None and tgt_path.is_dir() and tgt is not None:
        try:
            recomputed = compute_generation_digest(tgt_path, tgt, digest_schema_version)
        except Exception:
            recomputed = None
        if recomputed is not None and recomputed == tgt_digest:
            return RecoveryResult(
                case="B",
                status="PREPARED_NOT_COMMITTED",
                authoritative_generation=src,
                safe_action="RETRY_POINTER_SWITCH_AFTER_VERIFICATION",
            )

    if ptr_generation == tgt:
        verified = verify_active_generation(workspace_root)
        if verified.ok and verified.actual_digest == tgt_digest:
            return RecoveryResult(
                case="C",
                status="COMMITTED",
                authoritative_generation=tgt,
                safe_action="POST_COMMIT_VERIFICATION_OR_CLEANUP",
            )
        return RecoveryResult(
            case="E",
            status="FAILED_REQUIRES_RECOVERY",
            authoritative_generation=None,
            safe_action="manual_disposition_required",
        )

    return RecoveryResult(
        case="D",
        status="FAILED_REQUIRES_RECOVERY",
        authoritative_generation=None,
        safe_action="manual_disposition_required",
    )
