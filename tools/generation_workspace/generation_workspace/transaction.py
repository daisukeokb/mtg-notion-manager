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

from . import generation_source as gs
from . import mutation_guard as guard
from .digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS, compute_generation_digest
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
        # `writes` already counts the successful lock creation above; the
        # lock deletion below is itself a second successful logical
        # mutation whenever it actually happens (release.released), and
        # must be counted too -- a mutation later cleaned up is not the
        # same as a mutation that never happened.
        release_writes = writes + (1 if release.released else 0)
        if release.error_code == guard.LOCK_OWNERSHIP_MISMATCH:
            return BeginResult(
                ok=False,
                reason=f"{rej.message} (lock ownership mismatch during cleanup)",
                error_code=guard.LOCK_OWNERSHIP_MISMATCH,
                filesystem_writes=release_writes,
            )
        error_code = rej.error_code if release.released else guard.FAILED_REQUIRES_RECOVERY
        return BeginResult(
            ok=False, reason=rej.message, error_code=error_code, filesystem_writes=release_writes
        )

    staging = _staging_path(workspace_root, target_id)
    if staging.exists():
        release = _release_owned_lock()
        release_writes = writes + (1 if release.released else 0)
        return BeginResult(
            ok=False,
            reason=f"staging directory already exists: {staging}",
            error_code=guard.TARGET_GENERATION_ALREADY_EXISTS,
            filesystem_writes=release_writes,
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

    # Actual successful-mutation counter (WP-OGR-02 filesystem_writes
    # repair): incremented only after each logical filesystem mutation
    # below actually succeeds, never set from the expected happy-path
    # shape. Reads/stat/fsync/hashing/comparison are never counted.
    writes = 0

    # --- Operational Commit Mutation Boundary --------------------------
    #
    # Everything below this point is the accepted commit-phase filesystem
    # mutation sequence (WP-OGR-02 Repair-3: commit-phase operational
    # exception containment). All preceding validation/typed-return
    # processing above (control-transaction read, transaction_id match,
    # authorization/transaction binding) is unchanged and stays outside
    # this boundary. Existing explicit typed failures inside this
    # boundary (InventoryMismatchError, post-commit verification failure)
    # are preserved exactly as before and are not reclassified.
    #
    # An expected operational OSError from any filesystem operation in
    # this sequence (write, rename, fsync, unlink, mkdir) -- not just the
    # one that happens to sit between publication and pointer commit --
    # is contained here rather than escaping uncaught. The handler
    # performs zero workspace mutations of its own: no rollback, no
    # cleanup, no retry, no automatic recovery. The last successfully
    # persisted physical/control state is left exactly as the mutations
    # that already completed left it; only `writes` (already-accurate
    # successful-mutation count) and a FAILED_REQUIRES_RECOVERY
    # CommitResult are produced. Only OSError is caught -- arbitrary
    # programming exceptions (TypeError, KeyError, assertion failures,
    # etc.) are not swallowed and continue to propagate.
    try:
        target_id = txn["target_generation"]
        staging = _staging_path(workspace_root, target_id)
        generations_dir = workspace_root / GENERATIONS_DIRNAME
        if not generations_dir.exists():
            generations_dir.mkdir(exist_ok=True)
            writes += 1
        published_path = generations_dir / generation_directory_name(target_id)

        assert_same_filesystem(workspace_root, staging, generations_dir)

        # Step: verify Staging content is complete and internally
        # consistent (Manifest/State must already carry their final
        # values at this point).
        try:
            verify_physical_inventory(staging)
        except InventoryMismatchError as exc:
            return CommitResult(
                ok=False, reason=f"staging inventory invalid: {exc}", filesystem_writes=writes
            )

        digest = compute_generation_digest(staging, target_id, digest_schema_version)
        txn["state"] = STATE_VERIFIED
        txn["target_generation_digest"] = digest
        txn["updated_at_utc"] = _now_iso()
        _write_control_transaction(workspace_root, txn)
        writes += 1

        # fsync every staging file, then the staging directory itself.
        for entry in staging.iterdir():
            fsync_file(entry)
        fsync_dir(staging)

        # Publish: rename Staging -> canonical Generation name. Content is
        # immutable from this point on.
        os.rename(staging, published_path)
        writes += 1
        fsync_dir(generations_dir)

        txn["state"] = STATE_GENERATION_PUBLISHED
        txn["updated_at_utc"] = _now_iso()
        _write_control_transaction(workspace_root, txn)
        writes += 1

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
        writes += 1
        fsync_file(pointer_tmp)

        txn["state"] = STATE_COMMITTING_POINTER
        txn["updated_at_utc"] = _now_iso()
        _write_control_transaction(workspace_root, txn)
        writes += 1

        # Pointer commit: the single atomic rename that switches the
        # Active Generation. This is the actual commit point.
        os.rename(pointer_tmp, pointer_path)
        writes += 1
        fsync_dir(workspace_root)

        verified = verify_active_generation(workspace_root)
        if not verified.ok or verified.generation_id != target_id:
            return CommitResult(
                ok=False,
                reason=f"post-commit verification failed: {verified.reason}",
                filesystem_writes=writes,
            )

        txn["state"] = STATE_COMMITTED
        txn["updated_at_utc"] = _now_iso()
        _write_control_transaction(workspace_root, txn)
        writes += 1

        # Transient cleanup only; Generation content is never touched again.
        (workspace_root / CONTROL_TRANSACTION_FILENAME).unlink()
        writes += 1
        lock_path = workspace_root / LOCK_FILENAME
        if lock_path.exists():
            lock_path.unlink()
            writes += 1
    except OSError as exc:
        return CommitResult(
            ok=False,
            reason=f"commit-phase operational failure: {exc}",
            error_code=guard.FAILED_REQUIRES_RECOVERY,
            filesystem_writes=writes,
        )

    return CommitResult(
        ok=True, generation_id=target_id, generation_digest=digest, filesystem_writes=writes
    )


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


@dataclass(frozen=True)
class ApplyTransactionResult:
    ok: bool
    source_generation_id: str | None = None
    source_generation_digest: str | None = None
    target_generation_id: str | None = None
    target_generation_digest: str | None = None
    reason: str = ""
    error_code: str | None = None
    mutation_started: bool = False
    filesystem_writes: int = 0
    recovery_required: bool = False


def apply_generation_transaction(
    workspace_root: Path,
    generation_source_directory: Path,
    mutation_authorization: guard.MutationAuthorization | None,
) -> ApplyTransactionResult:
    """Frozen WP-OGR-02 Generation Transaction Runtime orchestrator.

    Human-frozen Generation Source error precedence: GS-1 (root safety) ->
    GS-2 (physical entry safety) -> GS-3 (authorized inventory set) -> GS-4
    (authorized content) -> begin_generation_transaction. GS-1..GS-4 are
    pure reads and always complete before begin_generation_transaction is
    called, so the transaction lock acquired there remains the first
    authorized workspace mutation (Generation Source preflight failure
    precedes transaction-begin failure, and preflight filesystem_writes
    stays 0). begin_generation_transaction, commit_generation_transaction,
    and recover_generation_transaction are reused unchanged; only
    Generation Source validation and materialization are new here.
    """
    if mutation_authorization is None:
        return ApplyTransactionResult(
            ok=False,
            reason="mutation_authorization is required",
            error_code=guard.MUTATION_AUTHORIZATION_REQUIRED,
        )
    if mutation_authorization.operation_scope != guard.OPERATION_SCOPE_TRANSACTION:
        return ApplyTransactionResult(
            ok=False,
            reason=(
                f"authorization operation_scope "
                f"{mutation_authorization.operation_scope!r} != "
                f"{guard.OPERATION_SCOPE_TRANSACTION!r}"
            ),
            error_code=guard.AUTHORIZATION_OPERATION_MISMATCH,
        )
    if mutation_authorization.generation_source_expected_files is None:
        return ApplyTransactionResult(
            ok=False,
            reason=(
                "apply_generation_transaction requires a v2 authorization with "
                "generation_source_expected_files"
            ),
            error_code=guard.AUTHORIZATION_SCHEMA_INVALID,
        )

    source_generation_id = mutation_authorization.source_generation_id
    source_generation_digest = mutation_authorization.source_generation_digest
    target_generation_id = mutation_authorization.target_generation_id

    # --- GS-1..GS-4: Generation Source preflight (pure reads, 0 writes). ---
    try:
        real_workspace_root = Path(workspace_root).resolve()
        real_source = gs.validate_generation_source_preflight(
            generation_source_directory, real_workspace_root, mutation_authorization
        )
    except gs.GenerationSourceRejection as rej:
        return ApplyTransactionResult(
            ok=False,
            source_generation_id=source_generation_id,
            source_generation_digest=source_generation_digest,
            target_generation_id=target_generation_id,
            reason=rej.message,
            error_code=rej.error_code,
            mutation_started=False,
            filesystem_writes=0,
            recovery_required=False,
        )

    # --- Transaction begin: first authorized workspace mutation. ---
    begin_result = begin_generation_transaction(
        workspace_root, mutation_authorization.transaction_id, mutation_authorization
    )
    if not begin_result.ok:
        # Human-frozen: filesystem_writes > 0 => mutation_started = true,
        # even when begin's own cleanup (e.g. releasing a lock it just
        # created) fully reverted the observable workspace state. A
        # mutation later cleaned up is not the same as a mutation that
        # never happened. recovery_required stays false here: every begin
        # failure path returns before the control-transaction file is
        # ever created, so recover_generation_transaction never has a
        # PREPARING transaction to act on -- the active pointer remains
        # untouched regardless of which begin failure branch was taken.
        return ApplyTransactionResult(
            ok=False,
            source_generation_id=source_generation_id,
            source_generation_digest=source_generation_digest,
            target_generation_id=target_generation_id,
            reason=begin_result.reason,
            error_code=begin_result.error_code,
            mutation_started=begin_result.filesystem_writes > 0,
            filesystem_writes=begin_result.filesystem_writes,
            recovery_required=False,
        )
    assert begin_result.staging_path is not None

    # --- Materialization: TOCTOU-safe verified-byte staging write. ---
    materialize_result = gs.materialize_generation_source(
        real_source, begin_result.staging_path, mutation_authorization
    )
    if not materialize_result.ok:
        # Frozen failure-preservation contract: lock, control transaction
        # (still PREPARING), and partial staging are left exactly as
        # begin_generation_transaction created them. No cleanup, no
        # rollback, no retry, no deletion -- recover_generation_transaction
        # Case A (NOT_COMMITTED / RESTART_FROM_STAGING) already covers this
        # state unchanged.
        return ApplyTransactionResult(
            ok=False,
            source_generation_id=source_generation_id,
            source_generation_digest=source_generation_digest,
            target_generation_id=target_generation_id,
            reason=materialize_result.reason,
            error_code=guard.FAILED_REQUIRES_RECOVERY,
            mutation_started=True,
            filesystem_writes=begin_result.filesystem_writes + materialize_result.filesystem_writes,
            recovery_required=True,
        )

    # --- Existing Phase B / publication / pointer commit (unchanged). ---
    digest_schema_version = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))
    commit_result = commit_generation_transaction(
        workspace_root,
        mutation_authorization.transaction_id,
        digest_schema_version,
        mutation_authorization,
    )
    total_writes = (
        begin_result.filesystem_writes
        + materialize_result.filesystem_writes
        + commit_result.filesystem_writes
    )
    if not commit_result.ok:
        # Human-frozen fallback: any recovery-required Generation
        # Transaction failure with no more-specific already-accepted
        # error code must report FAILED_REQUIRES_RECOVERY, never null.
        # commit_generation_transaction's own CommitResult.error_code is
        # already-accepted and specific for authorization/binding
        # failures (e.g. AUTHORIZATION_DIGEST_MISMATCH); it is None only
        # for its two internal post-mutation-start failure branches
        # (staging inventory invalid, post-commit verification failed),
        # both of which leave a transaction requiring recovery, exactly
        # the same failure class already classified as
        # FAILED_REQUIRES_RECOVERY for materialization failures above.
        error_code = (
            commit_result.error_code
            if commit_result.error_code is not None
            else guard.FAILED_REQUIRES_RECOVERY
        )
        return ApplyTransactionResult(
            ok=False,
            source_generation_id=source_generation_id,
            source_generation_digest=source_generation_digest,
            target_generation_id=target_generation_id,
            reason=commit_result.reason,
            error_code=error_code,
            mutation_started=True,
            filesystem_writes=total_writes,
            recovery_required=True,
        )

    return ApplyTransactionResult(
        ok=True,
        source_generation_id=source_generation_id,
        source_generation_digest=source_generation_digest,
        target_generation_id=commit_result.generation_id,
        target_generation_digest=commit_result.generation_digest,
        mutation_started=True,
        filesystem_writes=total_writes,
        recovery_required=False,
    )
