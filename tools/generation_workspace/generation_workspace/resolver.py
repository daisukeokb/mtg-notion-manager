"""Reader-side resolution and verification.

Both functions take only ``workspace_root`` as input (never a Generation
Path directly) and internally resolve the Canonical Generation Path via the
Active Generation Pointer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .digest import (
    SUPPORTED_DIGEST_SCHEMA_VERSIONS,
    UnsupportedDigestSchemaError,
    build_components,
    canonical_relative_path,
    compute_generation_digest,
)
from .inventory import InventoryMismatchError, verify_physical_inventory
from .model import (
    ACTIVE_GENERATION_ABSENT,
    ACTIVE_GENERATION_MALFORMED,
    ACTIVE_GENERATION_SYMLINK,
    GENERATION_DIGEST_MISMATCH,
    GENERATION_INVENTORY_MISMATCH,
    GENERATION_MISSING,
    GENERATION_SYMLINK,
    Component,
    MalformedPointerError,
    Pointer,
    generation_directory_name,
    parse_pointer,
)

ACTIVE_GENERATION_FILENAME = "active_generation"
GENERATIONS_DIRNAME = "generations"
CANDIDATE_STATUS_LINE = "Candidate Part 1 Status: NON-CANONICAL / UNREVIEWED / NOT ACCEPTED"


@dataclass(frozen=True)
class ResolveResult:
    ok: bool
    generation_id: str | None = None
    generation_path: Path | None = None
    pointer: Pointer | None = None
    reason: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    generation_id: str | None = None
    reason: str | None = None
    expected_digest: str | None = None
    actual_digest: str | None = None
    error_code: str | None = None


def _pointer_path(workspace_root: Path) -> Path:
    return workspace_root / ACTIVE_GENERATION_FILENAME


def resolve_active_generation(workspace_root: Path) -> ResolveResult:
    """Fail-closed pointer resolution. Never guesses a generation."""
    pointer_path = _pointer_path(workspace_root)
    if not pointer_path.is_file():
        return ResolveResult(
            ok=False, reason="active_generation is absent", error_code=ACTIVE_GENERATION_ABSENT
        )
    if pointer_path.is_symlink():
        return ResolveResult(
            ok=False,
            reason="active_generation must not be a symlink",
            error_code=ACTIVE_GENERATION_SYMLINK,
        )

    try:
        pointer = parse_pointer(pointer_path.read_bytes())
    except MalformedPointerError as exc:
        return ResolveResult(ok=False, reason=str(exc), error_code=ACTIVE_GENERATION_MALFORMED)

    if pointer.generation_digest_schema_version not in SUPPORTED_DIGEST_SCHEMA_VERSIONS:
        return ResolveResult(
            ok=False,
            reason=(
                "unsupported generation_digest_schema_version: "
                f"{pointer.generation_digest_schema_version!r}"
            ),
            error_code=ACTIVE_GENERATION_MALFORMED,
        )

    generations_root = (workspace_root / GENERATIONS_DIRNAME).resolve()
    dir_name = generation_directory_name(pointer.generation_id)
    generation_path = (workspace_root / GENERATIONS_DIRNAME / dir_name).resolve()

    # Path escape rejection: resolved path must remain under generations_root.
    try:
        generation_path.relative_to(generations_root)
    except ValueError:
        return ResolveResult(
            ok=False,
            reason="generation path escapes generations/ root",
            error_code=GENERATION_MISSING,
        )

    if not generation_path.is_dir():
        return ResolveResult(
            ok=False,
            reason=f"generation directory missing: {generation_path}",
            error_code=GENERATION_MISSING,
        )
    if (workspace_root / GENERATIONS_DIRNAME / dir_name).is_symlink():
        return ResolveResult(
            ok=False,
            reason="generation directory must not be a symlink",
            error_code=GENERATION_SYMLINK,
        )

    return ResolveResult(
        ok=True,
        generation_id=pointer.generation_id,
        generation_path=generation_path,
        pointer=pointer,
    )


def verify_active_generation(workspace_root: Path) -> VerifyResult:
    """Full integrity verification. Depends only on workspace_root,
    active_generation, and the physical files of the Generation it points
    to. Never depends on .control_transaction.json.
    """
    resolved = resolve_active_generation(workspace_root)
    if not resolved.ok:
        return VerifyResult(ok=False, reason=resolved.reason, error_code=resolved.error_code)

    generation_path = resolved.generation_path
    pointer = resolved.pointer
    assert generation_path is not None and pointer is not None

    # Reject symlink components.
    for entry in generation_path.iterdir():
        if entry.is_symlink():
            return VerifyResult(
                ok=False,
                reason=f"component must not be a symlink: {entry.name}",
                error_code=GENERATION_SYMLINK,
            )

    try:
        verify_physical_inventory(generation_path)
    except InventoryMismatchError as exc:
        return VerifyResult(ok=False, reason=str(exc), error_code=GENERATION_INVENTORY_MISMATCH)

    try:
        actual_digest = compute_generation_digest(
            generation_path, pointer.generation_id, pointer.generation_digest_schema_version
        )
    except UnsupportedDigestSchemaError as exc:
        return VerifyResult(ok=False, reason=str(exc), error_code=GENERATION_DIGEST_MISMATCH)

    if actual_digest != pointer.generation_digest:
        return VerifyResult(
            ok=False,
            reason="generation digest mismatch",
            expected_digest=pointer.generation_digest,
            actual_digest=actual_digest,
            error_code=GENERATION_DIGEST_MISMATCH,
        )

    candidate_files = [
        p
        for p in generation_path.iterdir()
        if p.name.startswith("candidate_part_") and p.suffix == ".md"
    ]
    if len(candidate_files) != 1:
        return VerifyResult(
            ok=False,
            reason="expected exactly one candidate file",
            error_code=GENERATION_INVENTORY_MISMATCH,
        )
    candidate_text = candidate_files[0].read_text(encoding="utf-8")
    non_empty_lines = [line for line in candidate_text.splitlines() if line.strip()]
    if not non_empty_lines or non_empty_lines[-1].strip() != CANDIDATE_STATUS_LINE:
        return VerifyResult(
            ok=False,
            reason="candidate final status line mismatch",
            error_code=GENERATION_INVENTORY_MISMATCH,
        )

    manifest = json.loads((generation_path / "workspace_manifest.json").read_text("utf-8"))
    state = json.loads((generation_path / "workspace_state.json").read_text("utf-8"))
    if manifest.get("last_control_action") != state.get("last_control_action"):
        return VerifyResult(
            ok=False,
            reason="manifest/state last_control_action mismatch",
            error_code=GENERATION_INVENTORY_MISMATCH,
        )
    if manifest.get("last_control_transaction_id") != state.get("last_control_transaction_id"):
        return VerifyResult(
            ok=False,
            reason="manifest/state transaction id mismatch",
            error_code=GENERATION_INVENTORY_MISMATCH,
        )

    return VerifyResult(
        ok=True,
        generation_id=pointer.generation_id,
        expected_digest=pointer.generation_digest,
        actual_digest=actual_digest,
    )


# --- Canonical Operational Reader (WP-OGR-01) --------------------------------
#
# read_verified_active_generation() is the only sanctioned entry point for an
# operational consumer that needs the current Generation's content: it always
# resolves through the Active Generation Pointer (never a caller-supplied
# generation path) and always requires full verify_active_generation()
# success (digest + inventory + manifest/state integrity) first. It never
# reads the Flat Baseline. read_verified_file() then re-validates byte count
# and SHA-256 against the Generation Digest's per-file metadata on every
# call, because Published Generation Immutability is DETECTIVE_ONLY, not
# filesystem-enforced: a prior successful verification is never treated as
# proof that the file is still unchanged.


@dataclass(frozen=True)
class VerifiedActiveGeneration:
    generation_id: str
    generation_path: Path
    generation_digest: str
    components: tuple[Component, ...]


@dataclass(frozen=True)
class VerifiedActiveGenerationResult:
    ok: bool
    verified: VerifiedActiveGeneration | None = None
    reason: str | None = None
    error_code: str | None = None


def read_verified_active_generation(workspace_root: Path) -> VerifiedActiveGenerationResult:
    """Canonical Operational Read Path entry point.

    Depends only on ``workspace_root``. Resolves the Active Generation via
    resolve_active_generation() and requires the full verify_active_generation()
    contract (digest + inventory integrity, among the other checks it already
    performs) to succeed before any file becomes readable through
    read_verified_file(). Never falls back to the Flat Baseline and never
    accepts a caller-supplied generation path as canonical.
    """
    verified = verify_active_generation(workspace_root)
    if not verified.ok:
        return VerifiedActiveGenerationResult(
            ok=False, reason=verified.reason, error_code=verified.error_code
        )

    resolved = resolve_active_generation(workspace_root)
    assert resolved.ok and resolved.generation_path is not None
    assert verified.generation_id is not None and verified.actual_digest is not None

    components = tuple(build_components(resolved.generation_path, verified.generation_id))

    return VerifiedActiveGenerationResult(
        ok=True,
        verified=VerifiedActiveGeneration(
            generation_id=verified.generation_id,
            generation_path=resolved.generation_path,
            generation_digest=verified.actual_digest,
            components=components,
        ),
    )


@dataclass(frozen=True)
class VerifiedFile:
    relative_path: str
    byte_count: int
    sha256: str
    content: bytes


def _is_safe_relative_filename(relative_path: str) -> bool:
    if not relative_path or "/" in relative_path or "\\" in relative_path:
        return False
    if relative_path in (".", ".."):
        return False
    return True


def read_verified_file(
    verified: VerifiedActiveGeneration, relative_path: str
) -> VerifiedFile | None:
    """Read one file from an already-verified Active Generation.

    ``relative_path`` must be a bare filename within the Generation (no
    subdirectories, no traversal) — this is never a caller-supplied
    generation path, only a component name within the Generation that
    read_verified_active_generation() already verified. On every call, the
    file's current byte count and SHA-256 are recomputed from disk and
    checked against the canonical per-file metadata recorded in
    ``verified.components``; any mismatch, missing component, missing file,
    or symlink component fails the read. There is no fallback, no repair,
    and no mutation on failure — this function returns ``None``.
    """
    if not _is_safe_relative_filename(relative_path):
        return None

    expected_canonical_path = canonical_relative_path(verified.generation_id, relative_path)
    component = next(
        (c for c in verified.components if c.relative_path == expected_canonical_path), None
    )
    if component is None:
        return None

    file_path = verified.generation_path / relative_path
    if file_path.is_symlink() or not file_path.is_file():
        return None

    data = file_path.read_bytes()
    if len(data) != component.byte_count:
        return None
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != component.sha256:
        return None

    return VerifiedFile(
        relative_path=relative_path,
        byte_count=len(data),
        sha256=actual_sha256,
        content=data,
    )
