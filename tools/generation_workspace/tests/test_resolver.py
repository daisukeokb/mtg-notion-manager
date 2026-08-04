import os

from generation_workspace.model import generation_directory_name
from generation_workspace.resolver import resolve_active_generation, verify_active_generation


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
