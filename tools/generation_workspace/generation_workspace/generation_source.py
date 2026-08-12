"""Generation Source Directory validation and TOCTOU-safe materialization.

The Generation Source Directory is an external, read-only input to
apply_generation_transaction(). Runtime never modifies, renames, deletes,
or otherwise mutates it (Generation Source filesystem writes: 0).
Authorization for what content may enter the target Generation comes
exclusively from mutation_authorization.generation_source_expected_files
(GENERATION_SOURCE_CONTENT_INVENTORY, WP-CLAIM-EXIT-MUTATION-AUTHORIZATION-v2)
-- directory content is never used as implicit authorization (Path
observation != Content authorization).

Human-frozen Generation Source preflight order (WP-OGR-02):
  GS-1 Root Safety -> GS-2 Physical Entry Safety -> GS-3 Authorized
  Inventory Set -> GS-4 Authorized Content (byte count + SHA-256).
All four stages are pure reads; preflight filesystem_writes == 0. GS-1
through GS-4 always complete before begin_generation_transaction(...) is
called -- the transaction lock acquired there remains the first
authorized workspace mutation (do not acquire it merely to discover a
Generation Source preflight error).

Apply-time TOCTOU contract (separate from initial GS-1..GS-4 preflight):
for every authorized entry, immediately before materializing it, the
entry is revalidated, its exact bytes are read once, byte_count and
SHA-256 are verified against that same read, and those already-verified
bytes -- never a second independent read -- are the bytes written to
staging. The staged copy is then re-read and reverified. This makes
"Verified Source Bytes == Materialized Staging Bytes" hold by
construction rather than by a best-effort after-the-fact comparison.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import mutation_guard as guard

# --- Error codes: GS-1..GS-4 preflight only (pure reads, 0 writes). A
# post-mutation-start materialization/TOCTOU failure is reported through
# the existing guard.FAILED_REQUIRES_RECOVERY error_code instead (frozen
# Machine Result Contract), not through a code defined here. -------------

GENERATION_SOURCE_NOT_FOUND = "GENERATION_SOURCE_NOT_FOUND"
GENERATION_SOURCE_SYMLINK_REJECTED = "GENERATION_SOURCE_SYMLINK_REJECTED"
GENERATION_SOURCE_NOT_DIRECTORY = "GENERATION_SOURCE_NOT_DIRECTORY"
GENERATION_SOURCE_ROOT_POLICY_REJECTED = "GENERATION_SOURCE_ROOT_POLICY_REJECTED"
GENERATION_SOURCE_ENTRY_REJECTED = "GENERATION_SOURCE_ENTRY_REJECTED"
GENERATION_SOURCE_UNSAFE_PATH = "GENERATION_SOURCE_UNSAFE_PATH"
GENERATION_SOURCE_DUPLICATE_PATH = "GENERATION_SOURCE_DUPLICATE_PATH"
GENERATION_SOURCE_INVENTORY_MISMATCH = "GENERATION_SOURCE_INVENTORY_MISMATCH"
GENERATION_SOURCE_CONTENT_MISMATCH = "GENERATION_SOURCE_CONTENT_MISMATCH"


class GenerationSourceRejection(Exception):
    """Internal control-flow exception carrying a GS-1..GS-4 preflight failure."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _reject(error_code: str, message: str) -> None:
    raise GenerationSourceRejection(error_code, message)


# --- GS-1: Root Safety -------------------------------------------------


def _validate_root_safety(generation_source_directory: Path, real_workspace_root: Path) -> Path:
    raw = Path(generation_source_directory)
    if raw.is_symlink():
        _reject(
            GENERATION_SOURCE_SYMLINK_REJECTED,
            "generation_source_directory must not be a symlink",
        )
    if not raw.exists():
        _reject(
            GENERATION_SOURCE_NOT_FOUND,
            f"generation_source_directory does not exist: {raw}",
        )
    try:
        real_source = raw.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        _reject(
            GENERATION_SOURCE_NOT_FOUND,
            f"generation_source_directory could not be resolved: {exc}",
        )
    if not real_source.is_dir():
        _reject(GENERATION_SOURCE_NOT_DIRECTORY, "generation_source_directory is not a directory")

    real_generations_root = real_workspace_root / "generations"
    for ancestor, label in (
        (real_workspace_root, "workspace_root"),
        (real_generations_root, "workspace_root/generations"),
    ):
        try:
            real_source.relative_to(ancestor)
        except ValueError:
            continue
        _reject(
            GENERATION_SOURCE_ROOT_POLICY_REJECTED,
            f"generation_source_directory must not be inside or equal to {label}",
        )

    return real_source


# --- GS-2: Physical Entry Safety ----------------------------------------


def _validate_entry_safety(real_source: Path) -> list[str]:
    """Returns the sorted top-level filenames that passed physical entry
    safety. Raises GenerationSourceRejection on the first violation.
    """
    entries = sorted(real_source.iterdir(), key=lambda p: p.name)
    names: list[str] = []
    seen: set[str] = set()

    for entry in entries:
        name = entry.name

        if name in seen:
            _reject(GENERATION_SOURCE_DUPLICATE_PATH, f"duplicate normalized path: {name!r}")
        seen.add(name)

        if not guard._is_safe_generation_source_relative_path(name):
            _reject(GENERATION_SOURCE_UNSAFE_PATH, f"unsafe relative_path: {name!r}")

        if entry.is_symlink():
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"entry must not be a symlink: {name}")
        if entry.is_dir():
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"nested directory not permitted: {name}")

        st = entry.lstat()
        if stat.S_ISSOCK(st.st_mode):
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"socket not permitted: {name}")
        if stat.S_ISFIFO(st.st_mode):
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"FIFO not permitted: {name}")
        if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"device file not permitted: {name}")
        if not stat.S_ISREG(st.st_mode):
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"entry must be a regular file: {name}")
        if st.st_nlink != 1:
            _reject(GENERATION_SOURCE_ENTRY_REJECTED, f"entry must have st_nlink == 1: {name}")

        names.append(name)

    return names


# --- GS-3: Authorized Inventory Set -------------------------------------


def _validate_inventory_set(physical_names: list[str], expected_entries: list) -> None:
    physical_set = set(physical_names)
    expected_set = {e.relative_path for e in expected_entries}

    missing = sorted(expected_set - physical_set)
    if missing:
        _reject(
            GENERATION_SOURCE_INVENTORY_MISMATCH,
            f"missing authorized generation source entry(ies): {missing}",
        )
    unexpected = sorted(physical_set - expected_set)
    if unexpected:
        _reject(
            GENERATION_SOURCE_INVENTORY_MISMATCH,
            f"unexpected generation source entry(ies): {unexpected}",
        )


# --- GS-4: Authorized Content --------------------------------------------


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_content(real_source: Path, expected_entries: list) -> None:
    for entry in sorted(expected_entries, key=lambda e: e.relative_path):
        file_path = real_source / entry.relative_path
        st = file_path.lstat()
        if not stat.S_ISREG(st.st_mode):
            _reject(
                GENERATION_SOURCE_ENTRY_REJECTED,
                f"entry must be a regular file: {entry.relative_path}",
            )
        if st.st_size != entry.byte_count:
            _reject(
                GENERATION_SOURCE_CONTENT_MISMATCH, f"byte_count mismatch: {entry.relative_path}"
            )
        if _sha256_of_file(file_path) != entry.sha256:
            _reject(
                GENERATION_SOURCE_CONTENT_MISMATCH, f"SHA-256 mismatch: {entry.relative_path}"
            )


def validate_generation_source_preflight(
    generation_source_directory: Path,
    real_workspace_root: Path,
    mutation_authorization: guard.MutationAuthorization,
) -> Path:
    """GS-1 -> GS-2 -> GS-3 -> GS-4, in the Human-frozen order. Pure reads
    only (filesystem_writes == 0); never touches generation_source_directory
    or workspace_root. Raises GenerationSourceRejection on the first
    failing stage -- an earlier-stage error always masks a later-stage
    error, since later stages are never reached. Returns the resolved,
    policy-approved Generation Source directory on success.
    """
    real_source = _validate_root_safety(generation_source_directory, real_workspace_root)
    physical_names = _validate_entry_safety(real_source)
    expected_entries = mutation_authorization.generation_source_expected_files or []
    _validate_inventory_set(physical_names, expected_entries)
    _validate_content(real_source, expected_entries)
    return real_source


# --- Apply-time TOCTOU-safe materialization ------------------------------


@dataclass(frozen=True)
class MaterializeResult:
    ok: bool
    filesystem_writes: int = 0
    reason: str = ""


class _OpenedObjectUnsafe(Exception):
    """Raised when the descriptor actually opened for a trusted read does
    not itself satisfy the required physical-entry safety properties.
    Internal control flow only; never surfaced as a public error code (the
    frozen contract reports every apply-time materialization failure
    through guard.FAILED_REQUIRES_RECOVERY, regardless of which specific
    check inside materialize_generation_source triggered it).
    """


def _secure_read_trusted_bytes(file_path: Path) -> bytes:
    """Open `file_path` without following a final-component symlink,
    verify the *actual opened file descriptor* -- not a separate,
    independently re-resolved path -- is a regular file with
    ``st_nlink == 1``, and read its bytes from that same descriptor.

    This closes the TOCTOU window between "the entry was checked" and
    "the entry was read": lstat()-then-read_bytes() are two independent
    path lookups an attacker could race between (e.g. by substituting a
    symlink after the lstat), whereas fstat()-on-the-opened-fd can only
    ever describe the object that open() actually resolved and that
    os.read() actually reads from. O_NOFOLLOW makes open() itself fail
    (ELOOP) if the final path component is currently a symlink, so a
    symlink substitution is never silently followed.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(str(file_path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _OpenedObjectUnsafe(f"opened object is not a regular file: {file_path}")
        if st.st_nlink != 1:
            raise _OpenedObjectUnsafe(f"opened object has st_nlink != 1: {file_path}")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def materialize_generation_source(
    real_source: Path,
    staging_path: Path,
    mutation_authorization: guard.MutationAuthorization,
) -> MaterializeResult:
    """Apply-time TOCTOU-safe materialization of every authorized
    Generation Source entry into `staging_path`.

    For each entry: secure open (no-follow) -> fstat the opened
    descriptor (regular file, st_nlink == 1) -> read the exact bytes from
    that same descriptor -> byte_count verification -> SHA-256
    verification -> write of those already-verified bytes (never an
    independent second read of the source) -> staging reverification
    read. "Opened Object Verified == Object Supplying Trusted Bytes" and
    "Verified Source Bytes == Materialized Staging Bytes" both hold by
    construction. Never reads or writes anything outside the declared
    entry set; never mutates `real_source`.
    """
    writes = 0
    expected_entries = sorted(
        mutation_authorization.generation_source_expected_files or [],
        key=lambda e: e.relative_path,
    )

    for entry in expected_entries:
        file_path = real_source / entry.relative_path

        try:
            data = _secure_read_trusted_bytes(file_path)
        except FileNotFoundError as exc:
            return MaterializeResult(
                ok=False,
                filesystem_writes=writes,
                reason=(
                    "generation source entry vanished before materialization: "
                    f"{entry.relative_path}: {exc}"
                ),
            )
        except (OSError, _OpenedObjectUnsafe) as exc:
            # OSError here includes ELOOP (O_NOFOLLOW rejected a symlink
            # substituted at this path) and any other open/fstat/read
            # failure; _OpenedObjectUnsafe covers a non-regular or
            # multiply-linked object actually opened in place of the
            # originally authorized entry. Either way the entry no longer
            # provably satisfies the required physical-object safety.
            return MaterializeResult(
                ok=False,
                filesystem_writes=writes,
                reason=(
                    "generation source entry changed or became unsafe before "
                    f"materialization: {entry.relative_path}: {exc}"
                ),
            )

        if len(data) != entry.byte_count:
            return MaterializeResult(
                ok=False,
                filesystem_writes=writes,
                reason=(
                    "generation source byte_count changed before materialization: "
                    f"{entry.relative_path}"
                ),
            )
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            return MaterializeResult(
                ok=False,
                filesystem_writes=writes,
                reason=(
                    "generation source SHA-256 changed before materialization: "
                    f"{entry.relative_path}"
                ),
            )

        dest_path = staging_path / entry.relative_path
        dest_path.write_bytes(data)
        writes += 1

        restaged = dest_path.read_bytes()
        if restaged != data:
            return MaterializeResult(
                ok=False,
                filesystem_writes=writes,
                reason=f"staging reverification failed: {entry.relative_path}",
            )

    return MaterializeResult(ok=True, filesystem_writes=writes)
