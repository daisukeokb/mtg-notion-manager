import dataclasses
import hashlib
import os
import shutil

from generation_workspace.model import (
    ACTIVE_GENERATION_ABSENT,
    ACTIVE_GENERATION_MALFORMED,
    ACTIVE_GENERATION_SYMLINK,
    GENERATION_DIGEST_MISMATCH,
    GENERATION_INVENTORY_MISMATCH,
    GENERATION_MISSING,
    GENERATION_SYMLINK,
    generation_directory_name,
    parse_pointer,
)
from generation_workspace.resolver import (
    read_verified_active_generation,
    read_verified_file,
    resolve_active_generation,
    verify_active_generation,
)


def test_T_015_non_canonical_generations_id_path_rejected(bootstrapped_workspace):
    """T-015: a Pointer/tooling combination that would resolve to
    generations/<generation_id> (missing the gen- prefix) must not be
    accepted as the canonical path; generation_directory_name always
    injects the prefix, so no code path can produce the bare form."""
    gen_id = "0000000001"
    assert generation_directory_name(gen_id) == f"gen-{gen_id}"
    non_canonical = bootstrapped_workspace / "generations" / gen_id
    assert not non_canonical.exists()  # only the gen-prefixed directory was created


def test_T_016_only_gen_prefixed_path_resolved(bootstrapped_workspace):
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok
    assert resolved.generation_path.name == "gen-0000000001"


def test_T_017_resolver_fail_closed_when_pointer_absent(tmp_path):
    result = resolve_active_generation(tmp_path)
    assert not result.ok
    assert result.generation_id is None
    assert result.generation_path is None


def test_T_020_symlinked_generation_directory_rejected(bootstrapped_workspace):
    real_dir = bootstrapped_workspace / "generations" / "gen-0000000001"
    decoy = bootstrapped_workspace / "generations" / "gen-0000000099"
    os.symlink(real_dir, decoy, target_is_directory=True)

    # Point active_generation at the symlinked decoy by rewriting the
    # pointer's generation_id (content otherwise unchanged/still valid).
    from generation_workspace.model import parse_pointer

    pointer_path = bootstrapped_workspace / "active_generation"
    pointer = parse_pointer(pointer_path.read_bytes())
    import dataclasses

    decoy_pointer = dataclasses.replace(pointer, generation_id="0000000099")
    pointer_path.write_bytes(decoy_pointer.to_canonical_bytes())

    result = resolve_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert "symlink" in result.reason


def test_T_021_symlinked_component_inside_generation_rejected(bootstrapped_workspace):
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok
    real_source = resolved.generation_path / "source_01_layer_1.jsonl"
    decoy_target = bootstrapped_workspace / "outside_file.jsonl"
    decoy_target.write_text("{}", encoding="utf-8")
    real_source.unlink()
    os.symlink(decoy_target, real_source)

    verified = verify_active_generation(bootstrapped_workspace)
    assert not verified.ok
    assert "symlink" in verified.reason


# --- Canonical Operational Reader (WP-OGR-01) --------------------------------


def test_OGR01_01_valid_verified_generation_succeeds(bootstrapped_workspace):
    result = read_verified_active_generation(bootstrapped_workspace)
    assert result.ok
    assert result.verified.generation_id == "0000000001"
    assert result.verified.generation_path.name == "gen-0000000001"
    assert result.verified.components


def test_OGR01_02_active_generation_absent(tmp_path):
    result = read_verified_active_generation(tmp_path)
    assert not result.ok
    assert result.error_code == ACTIVE_GENERATION_ABSENT


def test_OGR01_03_active_generation_malformed(bootstrapped_workspace):
    pointer_path = bootstrapped_workspace / "active_generation"
    pointer_path.write_text("not valid json", encoding="utf-8")

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == ACTIVE_GENERATION_MALFORMED


def test_OGR01_04_active_generation_symlink(bootstrapped_workspace):
    pointer_path = bootstrapped_workspace / "active_generation"
    real_target = bootstrapped_workspace / "active_generation.real"
    pointer_path.rename(real_target)
    os.symlink(real_target, pointer_path)

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == ACTIVE_GENERATION_SYMLINK


def test_OGR01_05_generation_missing(bootstrapped_workspace):
    shutil.rmtree(bootstrapped_workspace / "generations" / "gen-0000000001")

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == GENERATION_MISSING


def test_OGR01_06_generation_symlink(bootstrapped_workspace):
    real_dir = bootstrapped_workspace / "generations" / "gen-0000000001"
    decoy = bootstrapped_workspace / "generations" / "gen-0000000099"
    os.symlink(real_dir, decoy, target_is_directory=True)

    pointer_path = bootstrapped_workspace / "active_generation"
    pointer = parse_pointer(pointer_path.read_bytes())
    decoy_pointer = dataclasses.replace(pointer, generation_id="0000000099")
    pointer_path.write_bytes(decoy_pointer.to_canonical_bytes())

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == GENERATION_SYMLINK


def test_OGR01_07_generation_digest_mismatch(bootstrapped_workspace):
    target = (
        bootstrapped_workspace / "generations" / "gen-0000000001" / "source_01_layer_1.jsonl"
    )
    target.write_text('{"record_type": "TAMPERED"}\n', encoding="utf-8")

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == GENERATION_DIGEST_MISMATCH


def test_OGR01_08_generation_inventory_mismatch(bootstrapped_workspace):
    gen_dir = bootstrapped_workspace / "generations" / "gen-0000000001"
    (gen_dir / "source_01_layer_1.jsonl").unlink()

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == GENERATION_INVENTORY_MISMATCH


def test_OGR01_09_flat_baseline_fallback_prohibited(bootstrapped_workspace):
    # Corrupt the Generation-side copy while the Flat Baseline original (same
    # filename, valid readable content) remains untouched at workspace_root
    # — FROZEN_EVIDENCE per WP-OGR-04. The canonical read must still fail
    # rather than silently succeeding from the Flat Baseline.
    gen_dir = bootstrapped_workspace / "generations" / "gen-0000000001"
    flat_original = bootstrapped_workspace / "source_01_layer_1.jsonl"
    assert flat_original.is_file()
    assert flat_original.read_text(encoding="utf-8") == '{"record_type": "CHECKPOINT_METADATA"}\n'
    (gen_dir / "source_01_layer_1.jsonl").write_text('{"tampered": true}\n', encoding="utf-8")

    result = read_verified_active_generation(bootstrapped_workspace)
    assert not result.ok
    assert result.error_code == GENERATION_DIGEST_MISMATCH


def test_OGR01_10_verified_file_successful_read(bootstrapped_workspace):
    result = read_verified_active_generation(bootstrapped_workspace)
    assert result.ok

    verified_file = read_verified_file(result.verified, "workspace_manifest.json")
    assert verified_file is not None
    on_disk = (
        bootstrapped_workspace / "generations" / "gen-0000000001" / "workspace_manifest.json"
    ).read_bytes()
    assert verified_file.byte_count == len(on_disk)
    assert verified_file.sha256 == hashlib.sha256(on_disk).hexdigest()
    assert verified_file.content == on_disk


def test_OGR01_11_per_file_byte_count_mutation_detected(bootstrapped_workspace):
    result = read_verified_active_generation(bootstrapped_workspace)
    assert result.ok

    target = (
        bootstrapped_workspace / "generations" / "gen-0000000001" / "source_01_layer_1.jsonl"
    )
    target.write_bytes(target.read_bytes() + b"extra bytes appended after verification\n")

    assert read_verified_file(result.verified, "source_01_layer_1.jsonl") is None


def test_OGR01_12_same_size_content_mutation_detected(bootstrapped_workspace):
    result = read_verified_active_generation(bootstrapped_workspace)
    assert result.ok

    target = (
        bootstrapped_workspace / "generations" / "gen-0000000001" / "source_01_layer_1.jsonl"
    )
    original = target.read_bytes()
    mutated = bytearray(original)
    flip_index = len(mutated) // 2
    mutated[flip_index] = (mutated[flip_index] + 1) % 256
    target.write_bytes(bytes(mutated))
    assert len(mutated) == len(original)

    assert read_verified_file(result.verified, "source_01_layer_1.jsonl") is None
