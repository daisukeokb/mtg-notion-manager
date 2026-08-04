"""Reader-side resolution and verification.

Both functions take only ``workspace_root`` as input (never a Generation
Path directly) and internally resolve the Canonical Generation Path via the
Active Generation Pointer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .digest import (
    SUPPORTED_DIGEST_SCHEMA_VERSIONS,
    UnsupportedDigestSchemaError,
    compute_generation_digest,
)
from .inventory import InventoryMismatchError, verify_physical_inventory
from .model import (
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


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    generation_id: str | None = None
    reason: str | None = None
    expected_digest: str | None = None
    actual_digest: str | None = None


def _pointer_path(workspace_root: Path) -> Path:
    return workspace_root / ACTIVE_GENERATION_FILENAME


def resolve_active_generation(workspace_root: Path) -> ResolveResult:
    """Fail-closed pointer resolution. Never guesses a generation."""
    pointer_path = _pointer_path(workspace_root)
    if not pointer_path.is_file():
        return ResolveResult(ok=False, reason="active_generation is absent")
    if pointer_path.is_symlink():
        return ResolveResult(ok=False, reason="active_generation must not be a symlink")

    try:
        pointer = parse_pointer(pointer_path.read_bytes())
    except MalformedPointerError as exc:
        return ResolveResult(ok=False, reason=str(exc))

    if pointer.generation_digest_schema_version not in SUPPORTED_DIGEST_SCHEMA_VERSIONS:
        return ResolveResult(
            ok=False,
            reason=(
                "unsupported generation_digest_schema_version: "
                f"{pointer.generation_digest_schema_version!r}"
            ),
        )

    generations_root = (workspace_root / GENERATIONS_DIRNAME).resolve()
    dir_name = generation_directory_name(pointer.generation_id)
    generation_path = (workspace_root / GENERATIONS_DIRNAME / dir_name).resolve()

    # Path escape rejection: resolved path must remain under generations_root.
    try:
        generation_path.relative_to(generations_root)
    except ValueError:
        return ResolveResult(ok=False, reason="generation path escapes generations/ root")

    if not generation_path.is_dir():
        return ResolveResult(ok=False, reason=f"generation directory missing: {generation_path}")
    if (workspace_root / GENERATIONS_DIRNAME / dir_name).is_symlink():
        return ResolveResult(ok=False, reason="generation directory must not be a symlink")

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
        return VerifyResult(ok=False, reason=resolved.reason)

    generation_path = resolved.generation_path
    pointer = resolved.pointer
    assert generation_path is not None and pointer is not None

    # Reject symlink components.
    for entry in generation_path.iterdir():
        if entry.is_symlink():
            return VerifyResult(ok=False, reason=f"component must not be a symlink: {entry.name}")

    try:
        verify_physical_inventory(generation_path)
    except InventoryMismatchError as exc:
        return VerifyResult(ok=False, reason=str(exc))

    try:
        actual_digest = compute_generation_digest(
            generation_path, pointer.generation_id, pointer.generation_digest_schema_version
        )
    except UnsupportedDigestSchemaError as exc:
        return VerifyResult(ok=False, reason=str(exc))

    if actual_digest != pointer.generation_digest:
        return VerifyResult(
            ok=False,
            reason="generation digest mismatch",
            expected_digest=pointer.generation_digest,
            actual_digest=actual_digest,
        )

    candidate_files = [
        p
        for p in generation_path.iterdir()
        if p.name.startswith("candidate_part_") and p.suffix == ".md"
    ]
    if len(candidate_files) != 1:
        return VerifyResult(ok=False, reason="expected exactly one candidate file")
    candidate_text = candidate_files[0].read_text(encoding="utf-8")
    non_empty_lines = [line for line in candidate_text.splitlines() if line.strip()]
    if not non_empty_lines or non_empty_lines[-1].strip() != CANDIDATE_STATUS_LINE:
        return VerifyResult(ok=False, reason="candidate final status line mismatch")

    manifest = json.loads((generation_path / "workspace_manifest.json").read_text("utf-8"))
    state = json.loads((generation_path / "workspace_state.json").read_text("utf-8"))
    if manifest.get("last_control_action") != state.get("last_control_action"):
        return VerifyResult(ok=False, reason="manifest/state last_control_action mismatch")
    if manifest.get("last_control_transaction_id") != state.get("last_control_transaction_id"):
        return VerifyResult(ok=False, reason="manifest/state transaction id mismatch")

    return VerifyResult(
        ok=True,
        generation_id=pointer.generation_id,
        expected_digest=pointer.generation_digest,
        actual_digest=actual_digest,
    )
