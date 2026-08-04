"""Bootstrap: create gen-0000000001 and active_generation from a flat
(pre-Generation) workspace. Precondition: active_generation absent.

Copy is used, never hard link (Generation Immutability boundary must not
depend on shared inodes with the original flat files).

Guarded by mutation_guard: Phase A (no-write validation) -> exclusive Lock
-> Phase B (validation under Lock) -> mutation. See mutation_guard.py.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import mutation_guard as guard
from .digest import compute_generation_digest
from .durability import assert_same_filesystem, fsync_dir, fsync_file
from .inventory import derive_expected_inventory
from .model import POINTER_SCHEMA_VERSION, Pointer, generation_directory_name, is_valid_uuid
from .resolver import ACTIVE_GENERATION_FILENAME, GENERATIONS_DIRNAME, resolve_active_generation

BOOTSTRAP_GENERATION_ID = "0000000001"
LOCK_FILENAME = ".execution_lock.json"


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    ok: bool
    generation_id: str | None = None
    generation_digest: str | None = None
    reason: str = ""
    error_code: str | None = None
    filesystem_writes: int = 0


def bootstrap_generation_workspace(
    workspace_root: Path,
    transaction_id: str,
    digest_schema_version: str,
    mutation_authorization: guard.MutationAuthorization | None = None,
) -> BootstrapResult:
    # --- Phase A: no filesystem writes may occur before this returns. ---
    try:
        real_root = guard.validate_phase_a(
            mutation_authorization,
            expected_operation_scope=guard.OPERATION_SCOPE_BOOTSTRAP,
            workspace_root_arg=str(workspace_root),
            transaction_id_arg=transaction_id,
        )
    except guard.GuardRejection as rej:
        return BootstrapResult(
            ok=False, reason=rej.message, error_code=rej.error_code, filesystem_writes=0
        )
    assert mutation_authorization is not None
    workspace_root = real_root

    if not is_valid_uuid(transaction_id):
        return BootstrapResult(
            ok=False,
            reason=f"invalid transaction_id: {transaction_id!r}",
            error_code=guard.AUTHORIZATION_SCHEMA_INVALID,
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
        return BootstrapResult(
            ok=False, reason=rej.message, error_code=rej.error_code, filesystem_writes=0
        )
    writes = 1  # the lock file itself

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
        guard.validate_phase_b_bootstrap(workspace_root, mutation_authorization)
    except guard.GuardRejection as rej:
        release = _release_owned_lock()
        if release.error_code == guard.LOCK_OWNERSHIP_MISMATCH:
            return BootstrapResult(
                ok=False,
                reason=f"{rej.message} (lock ownership mismatch during cleanup)",
                error_code=guard.LOCK_OWNERSHIP_MISMATCH,
                filesystem_writes=writes,
            )
        if not release.released:
            return BootstrapResult(
                ok=False,
                reason=f"{rej.message} (lock cleanup also failed)",
                error_code=guard.FAILED_REQUIRES_RECOVERY,
                filesystem_writes=writes,
            )
        return BootstrapResult(
            ok=False, reason=rej.message, error_code=rej.error_code, filesystem_writes=0
        )

    published_path = (
        workspace_root / GENERATIONS_DIRNAME / generation_directory_name(BOOTSTRAP_GENERATION_ID)
    )
    staging = workspace_root / f".staging-gen-{BOOTSTRAP_GENERATION_ID}"
    if staging.exists() or published_path.exists():
        _release_owned_lock()
        return BootstrapResult(
            ok=False,
            reason="staging or published directory already exists",
            error_code=guard.TARGET_GENERATION_ALREADY_EXISTS,
            filesystem_writes=writes,
        )

    expected = derive_expected_inventory(workspace_root)

    staging.mkdir(parents=True)
    writes += 1
    assert_same_filesystem(workspace_root, staging)

    try:
        source_shas_before = {}
        for filename in expected:
            src_file = workspace_root / filename
            h = hashlib.sha256()
            with src_file.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            source_shas_before[filename] = h.hexdigest()
            shutil.copy2(src_file, staging / filename, follow_symlinks=False)
            writes += 1

        for filename in expected:
            h = hashlib.sha256()
            with (staging / filename).open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest() != source_shas_before[filename]:
                raise BootstrapError(f"copy verification failed for {filename}")

        digest = compute_generation_digest(staging, BOOTSTRAP_GENERATION_ID, digest_schema_version)

        for entry in staging.iterdir():
            fsync_file(entry)
        fsync_dir(staging)

        generations_dir = workspace_root / GENERATIONS_DIRNAME
        generations_dir.mkdir(exist_ok=True)
        os.rename(staging, published_path)
        fsync_dir(generations_dir)
        writes += 1

        pointer = Pointer(
            pointer_schema_version=POINTER_SCHEMA_VERSION,
            generation_id=BOOTSTRAP_GENERATION_ID,
            generation_digest_schema_version=digest_schema_version,
            generation_digest=digest,
            transaction_id=transaction_id,
        )
        pointer_path = workspace_root / ACTIVE_GENERATION_FILENAME
        pointer_tmp = workspace_root / (ACTIVE_GENERATION_FILENAME + ".tmp")
        pointer_tmp.write_bytes(pointer.to_canonical_bytes())
        fsync_file(pointer_tmp)
        os.rename(pointer_tmp, pointer_path)
        fsync_dir(workspace_root)
        writes += 1
    except Exception as exc:  # noqa: BLE001 - surface any failure as BootstrapResult
        return BootstrapResult(
            ok=False,
            reason=f"bootstrap failed: {exc}",
            error_code=guard.FAILED_REQUIRES_RECOVERY,
            filesystem_writes=writes,
        )

    verified = resolve_active_generation(workspace_root)
    if not verified.ok:
        return BootstrapResult(
            ok=False,
            reason=f"post-bootstrap verification failed: {verified.reason}",
            error_code=guard.FAILED_REQUIRES_RECOVERY,
            filesystem_writes=writes,
        )

    _release_owned_lock()

    return BootstrapResult(
        ok=True,
        generation_id=BOOTSTRAP_GENERATION_ID,
        generation_digest=digest,
        filesystem_writes=writes,
    )
