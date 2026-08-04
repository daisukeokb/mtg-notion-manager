"""Inventory Derivation Algorithm.

Expected Inventory is derived from the persistent contract (Manifest's
declared Stable File set + State's declared Source Checkpoint set + the
three mandatory Candidate/Manifest/State files), never hardcoded from the
numeric value of generation_id.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_FILENAME = "workspace_manifest.json"
STATE_FILENAME = "workspace_state.json"


class InventoryMismatchError(ValueError):
    """Declared inventory (Manifest/State) does not match physical files."""


def _source_checkpoint_prefix_order(filename: str) -> str:
    # source_01_layer_1.jsonl -> sortable key "01"
    stem = filename.split("_")[1] if filename.startswith("source_") else filename
    return stem


def derive_expected_inventory(generation_dir: Path) -> list[str]:
    """Return the expected filename set for a generation, per the persistent
    contract in its own Manifest and State files.

    Component ordering: SRC-01..SRC-0N (numeric ascending, whichever are
    declared), then CANDIDATE, then MANIFEST, then STATE.
    """
    manifest_path = generation_dir / MANIFEST_FILENAME
    state_path = generation_dir / STATE_FILENAME
    if not manifest_path.is_file():
        raise InventoryMismatchError(f"manifest not found: {manifest_path}")
    if not state_path.is_file():
        raise InventoryMismatchError(f"state not found: {state_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))

    manifest_sources = sorted(
        (
            name
            for name, kind in manifest.get("file_classification", {}).items()
            if kind == "SOURCE_CHECKPOINT"
        ),
        key=_source_checkpoint_prefix_order,
    )
    state_sources = sorted(
        state.get("source_checkpoint_files", []),
        key=_source_checkpoint_prefix_order,
    )

    if manifest_sources != state_sources:
        raise InventoryMismatchError(
            "Manifest source_checkpoint declarations differ from State "
            f"declarations: manifest={manifest_sources!r} state={state_sources!r}"
        )

    candidate_files = [
        name
        for name, kind in manifest.get("file_classification", {}).items()
        if kind == "CANDIDATE_PART" and name in manifest.get("completed_stage_files", [])
    ]
    if len(candidate_files) != 1:
        raise InventoryMismatchError(
            f"expected exactly one completed Candidate file, found {candidate_files!r}"
        )

    expected = list(manifest_sources) + candidate_files + [MANIFEST_FILENAME, STATE_FILENAME]
    return expected


def verify_physical_inventory(generation_dir: Path) -> list[str]:
    """Verify that the physical file set matches the declared inventory
    exactly (no extra files, no missing files). Returns the ordered
    expected filename list on success.
    """
    expected = derive_expected_inventory(generation_dir)
    physical = sorted(p.name for p in generation_dir.iterdir() if p.is_file() or p.is_symlink())
    expected_sorted = sorted(expected)
    if physical != expected_sorted:
        missing = sorted(set(expected_sorted) - set(physical))
        extra = sorted(set(physical) - set(expected_sorted))
        raise InventoryMismatchError(
            f"physical inventory mismatch: missing={missing!r} extra={extra!r}"
        )
    return expected
