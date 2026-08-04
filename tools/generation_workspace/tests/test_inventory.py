import json

import pytest
from generation_workspace.inventory import InventoryMismatchError, verify_physical_inventory

from .conftest import write_flat_workspace


def test_T_022_unexpected_extra_file_rejected(flat_workspace):
    (flat_workspace / "unexpected_extra_file.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(InventoryMismatchError) as excinfo:
        verify_physical_inventory(flat_workspace)
    assert "extra" in str(excinfo.value)


def test_T_023_missing_component_rejected(flat_workspace):
    (flat_workspace / "source_02_layer_2.jsonl").unlink()
    with pytest.raises(InventoryMismatchError) as excinfo:
        verify_physical_inventory(flat_workspace)
    assert "missing" in str(excinfo.value)


def test_T_027_expected_inventory_not_hardcoded_by_generation_id(tmp_path):
    """T-027: a Generation with a *different* source count than either of
    the two Generations the production Workspace happens to use (10 or 11
    files) must still be derived correctly and never via an if/elif keyed
    on generation_id."""
    root = tmp_path / "unusual"
    write_flat_workspace(root, source_count=5)  # neither 7 nor 8 sources
    expected = verify_physical_inventory(root)
    assert len(expected) == 5 + 1 + 2  # 5 sources + candidate + manifest + state
    assert expected[-2:] == ["workspace_manifest.json", "workspace_state.json"]


def test_T_028_manifest_declared_set_mismatches_physical_rejected(flat_workspace):
    manifest_path = flat_workspace / "workspace_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_classification"]["source_99_layer_99.jsonl"] = "SOURCE_CHECKPOINT"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InventoryMismatchError) as excinfo:
        verify_physical_inventory(flat_workspace)
    assert "Manifest" in str(excinfo.value) or "manifest" in str(excinfo.value)


def test_T_029_state_declared_set_mismatches_physical_rejected(flat_workspace):
    state_path = flat_workspace / "workspace_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["source_checkpoint_files"].append("source_99_layer_99.jsonl")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(InventoryMismatchError) as excinfo:
        verify_physical_inventory(flat_workspace)
    assert "State" in str(excinfo.value) or "state" in str(excinfo.value)
