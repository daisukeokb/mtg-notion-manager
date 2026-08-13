"""BLD-01..BLD-23, plus supplementary materialize/validate/fingerprint tests:
WP-OGR-03 static Case-A fixture builder tests.

Scope: the builder tooling only (generation_workspace.wp_ogr_03_case_a_fixture).
NOT WP-OGR-03-CG-01/CG-02 Recovery Inspection acceptance tests -- those remain a
separate, not-yet-authorized Human Gate.

These tests use only pytest-managed ephemeral tmp_path directories. None of them
reference, create, or inspect the Human-frozen Runtime fixture root
(/private/tmp/mtg-notion-manager-wp-ogr-03-case-a-runtime-fixture-01) -- see
BLD-11, which proves the production entry point rejects a non-frozen root before
touching the filesystem at all, without ever passing the real frozen path.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

from generation_workspace import wp_ogr_03_case_a_fixture as builder
from generation_workspace.digest import compute_generation_digest
from generation_workspace.model import parse_pointer
from generation_workspace.mutation_guard import (
    AUTHORIZATION_SCHEMA_VERSION,
    OPERATION_SCOPE_TRANSACTION,
    ExpectedFile,
    MutationAuthorization,
)
from generation_workspace.recovery_inspection import _LOCK_REQUIRED_KEYS
from generation_workspace.resolver import resolve_active_generation, verify_active_generation


def _called_names(source: str) -> set[str]:
    """Names actually invoked as function calls in `source` (module- or
    attribute-style, e.g. both `f()` and `mod.f()` register as "f") -- distinct
    from names merely mentioned in a docstring, comment, or import statement.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_BLD_01_deterministic_plan():
    plan_a = builder.build_case_a_fixture_plan()
    plan_b = builder.build_case_a_fixture_plan()
    assert plan_a == plan_b


def test_BLD_02_five_file_generation_inventory():
    plan = builder.build_case_a_fixture_plan()
    names = [name for name, _content in plan.generation_files]
    assert names == [
        "source_01_layer_1.jsonl",
        "source_02_layer_2.jsonl",
        "candidate_part_01_deliverables_01_06.md",
        "workspace_manifest.json",
        "workspace_state.json",
    ]
    assert len(plan.generation_files) == 5


def test_BLD_03_generation_digest_matches_independent_production_recomputation(tmp_path):
    plan = builder.build_case_a_fixture_plan()
    result = builder._materialize_plan_at(tmp_path / "root", plan)
    assert result.ok, result.reason

    gen_dir = result.output_root / "generations" / "gen-0000000001"
    recomputed = compute_generation_digest(
        gen_dir, builder.BOOTSTRAP_GENERATION_ID, builder.DIGEST_SCHEMA_VERSION
    )
    assert recomputed == plan.generation_digest


def test_BLD_04_authorization_digest_matches_real_MutationAuthorization_digest():
    plan = builder.build_case_a_fixture_plan()

    expected_files = [
        ExpectedFile(relative_path=name, sha256=hashlib.sha256(content).hexdigest())
        for name, content in plan.generation_files
    ]
    independently_built = MutationAuthorization(
        authorization_schema_version=AUTHORIZATION_SCHEMA_VERSION,
        authorization_id=builder.CASE_A_AUTHORIZATION_ID,
        operation_scope=OPERATION_SCOPE_TRANSACTION,
        transaction_id=builder.CASE_A_TRANSACTION_ID,
        workspace_root=str(builder.FROZEN_FIXTURE_ROOT),
        apply=True,
        source_generation_id=builder.BOOTSTRAP_GENERATION_ID,
        source_generation_digest=plan.generation_digest,
        target_generation_id=builder.CASE_A_TARGET_GENERATION_ID,
        expected_stable_file_count=len(expected_files),
        expected_files=expected_files,
    )
    assert independently_built.digest() == plan.authorization_digest


def test_BLD_05_case_a_id_relationship():
    assert builder.BOOTSTRAP_TRANSACTION_ID != builder.CASE_A_TRANSACTION_ID


def test_BLD_06_pointer_contract():
    plan = builder.build_case_a_fixture_plan()
    pointer = parse_pointer(plan.pointer_bytes)  # raises if malformed; also validates
    assert pointer.generation_id == "0000000001"
    assert pointer.generation_digest_schema_version == builder.DIGEST_SCHEMA_VERSION
    assert pointer.generation_digest == plan.generation_digest
    assert pointer.transaction_id == builder.BOOTSTRAP_TRANSACTION_ID


def test_BLD_07_control_transaction_contract():
    plan = builder.build_case_a_fixture_plan()
    obj = json.loads(plan.control_transaction_bytes)
    assert obj == {
        "protocol_version": "WP-CLAIM-EXIT-CONTROL-TRANSACTION-v1",
        "transaction_id": builder.CASE_A_TRANSACTION_ID,
        "state": "PREPARING",
        "source_generation": "0000000001",
        "target_generation": "0000000002",
        "target_generation_digest": None,
        "authorization_id": builder.CASE_A_AUTHORIZATION_ID,
        "authorization_digest": plan.authorization_digest,
        "operation_scope": "GENERATION_TRANSACTION",
        "created_at_utc": "2026-01-01T00:00:00Z",
        "updated_at_utc": "2026-01-01T00:00:00Z",
    }


def test_BLD_08_lock_contract():
    plan = builder.build_case_a_fixture_plan()
    obj = json.loads(plan.lock_bytes)
    assert obj == {
        "lock_schema_version": "WP-CLAIM-EXIT-LOCK-v1",
        "lock_id": builder.CASE_A_AUTHORIZATION_ID,
        "transaction_id": builder.CASE_A_TRANSACTION_ID,
        "authorization_id": builder.CASE_A_AUTHORIZATION_ID,
        "authorization_digest": plan.authorization_digest,
        "created_at_utc": "2026-01-01T00:00:00Z",
    }
    for key in _LOCK_REQUIRED_KEYS:
        assert key in obj and isinstance(obj[key], str)


def test_BLD_09_required_absence_contract(tmp_path):
    plan = builder.build_case_a_fixture_plan()
    result = builder._materialize_plan_at(tmp_path / "root", plan)
    assert result.ok, result.reason

    assert not (result.output_root / "generations" / "gen-0000000002").exists()
    assert not (result.output_root / ".control_transaction.json.tmp").exists()
    assert not (result.output_root / "active_generation.tmp").exists()


def test_BLD_10_frozen_timestamp_contract_and_no_dynamic_clock():
    plan = builder.build_case_a_fixture_plan()
    control = json.loads(plan.control_transaction_bytes)
    lock = json.loads(plan.lock_bytes)
    assert control["created_at_utc"] == "2026-01-01T00:00:00Z"
    assert control["updated_at_utc"] == "2026-01-01T00:00:00Z"
    assert lock["created_at_utc"] == "2026-01-01T00:00:00Z"

    source = Path(builder.__file__).read_text(encoding="utf-8")
    called = _called_names(source)
    assert "now" not in called
    assert "_now_iso" not in called
    assert "utcnow" not in called


def test_BLD_11_wrong_root_rejected_without_touching_frozen_path(tmp_path):
    wrong_root = tmp_path / "definitely-not-the-frozen-root"
    result = builder.materialize_case_a_fixture(wrong_root)
    assert result.ok is False
    assert "materialization root must be exactly" in result.reason
    assert not wrong_root.exists()  # nothing was written -- rejection precedes all I/O
    # The real frozen path is never referenced by this test at all.


def test_BLD_12_existing_root_rejected(tmp_path):
    already_exists = tmp_path / "already-exists"
    already_exists.mkdir()
    plan = builder.build_case_a_fixture_plan()

    result = builder._materialize_plan_at(already_exists, plan)
    assert result.ok is False
    assert "already exists" in result.reason


def test_BLD_13_symlink_root_rejected(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    symlink_root = tmp_path / "symlinked-root"
    symlink_root.symlink_to(real_dir)
    plan = builder.build_case_a_fixture_plan()

    result = builder._materialize_plan_at(symlink_root, plan)
    assert result.ok is False
    assert "symlink" in result.reason


def test_BLD_14_no_recovery_inspection_call_path():
    source = Path(builder.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "recovery_inspection" not in imported_modules
    assert not any(m and m.endswith("recovery_inspection") for m in imported_from)
    assert "inspect_generation_recovery" not in _called_names(source)


def test_BLD_15_no_generation_transaction_mutation_call_path():
    source = Path(builder.__file__).read_text(encoding="utf-8")
    called = _called_names(source)
    for forbidden in (
        "bootstrap_generation_workspace",
        "begin_generation_transaction",
        "commit_generation_transaction",
        "apply_generation_transaction",
        "recover_generation_transaction",
    ):
        assert forbidden not in called


def test_BLD_16_plan_path_safety_rejects_dotdot_escape_in_generation_file(tmp_path):
    """WP-OGR-03-BLD-FINDING-01 repair: a plan whose generation_files contains a
    ".."-escaping name must be rejected before any filesystem mutation."""
    plan = builder.build_case_a_fixture_plan()
    malicious = dataclasses.replace(
        plan, generation_files=plan.generation_files + (("../escape.txt", b"evil"),)
    )
    root = tmp_path / "root"

    result = builder._materialize_plan_at(root, malicious)

    assert result.ok is False
    assert result.reason_code == builder.REASON_UNSAFE_PLAN_PATH
    assert not root.exists()
    assert not any(tmp_path.rglob("escape.txt"))  # outside target not created


def test_BLD_17_plan_path_safety_rejects_absolute_path_in_generation_file(tmp_path):
    """WP-OGR-03-BLD-FINDING-01 repair: an absolute-path generation file name must
    be rejected. Uses an absolute path still confined to the test's own tmp_path
    (never a real system path) so that even a repair bug cannot write outside the
    test sandbox."""
    plan = builder.build_case_a_fixture_plan()
    absolute_escape = str(tmp_path / "escaped-abs" / "evil.txt")
    malicious = dataclasses.replace(
        plan, generation_files=plan.generation_files + ((absolute_escape, b"evil"),)
    )
    root = tmp_path / "root"

    result = builder._materialize_plan_at(root, malicious)

    assert result.ok is False
    assert result.reason_code == builder.REASON_UNSAFE_PLAN_PATH
    assert not root.exists()
    assert not Path(absolute_escape).exists()


def test_BLD_18_plan_path_safety_rejects_dotdot_escape_in_empty_directory(tmp_path):
    """WP-OGR-03-BLD-FINDING-01 repair: the directory-path vector, distinct from
    the file-path vector above."""
    plan = builder.build_case_a_fixture_plan()
    malicious = dataclasses.replace(plan, empty_directories=("../escape-dir",))
    root = tmp_path / "root"

    result = builder._materialize_plan_at(root, malicious)

    assert result.ok is False
    assert result.reason_code == builder.REASON_UNSAFE_PLAN_PATH
    assert not root.exists()
    assert not any(tmp_path.rglob("escape-dir"))


def test_BLD_19_mid_write_oserror_becomes_typed_failure_with_partial_state_retained(
    tmp_path, monkeypatch
):
    """WP-OGR-03-BLD-FINDING-02 repair: a filesystem failure partway through
    materialization must surface as a typed MaterializeResult(ok=False, ...), not
    a raw exception, must leave whatever was already written in place (no
    automatic cleanup/rollback), and a retry against the same root must then fail
    closed because the root now already exists. Uses a narrowly-scoped monkeypatch
    of `open` inside the builder module's own namespace -- not a global patch, and
    not a production-visible bypass hook of any kind.
    """
    import builtins

    plan = builder.build_case_a_fixture_plan()
    root = tmp_path / "root"
    first_written_name = plan.generation_files[0][0]
    failing_target_name = plan.generation_files[1][0]
    real_open = builtins.open

    def flaky_open(file, mode="r", *args, **kwargs):
        if Path(file).name == failing_target_name and "b" in mode and "x" in mode:
            raise OSError("simulated disk failure")
        return real_open(file, mode, *args, **kwargs)

    # builder.py never assigns its own `open` name -- it resolves the call via the
    # builtins fallback -- so this attribute does not pre-exist on the module and
    # `raising=False` is required. Setting it here shadows the builtin only within
    # this module's namespace, only for the duration of this test.
    monkeypatch.setattr(builder, "open", flaky_open, raising=False)

    result = builder._materialize_plan_at(root, plan)

    assert result.ok is False
    assert result.reason_code == builder.REASON_MATERIALIZATION_IO_ERROR
    assert "simulated disk failure" in result.reason

    # Partial state retained -- the first file (written before the injected
    # failure) is present; the failing one is not; no cleanup occurred.
    gen_dir = root / "generations" / "gen-0000000001"
    assert (gen_dir / first_written_name).is_file()
    assert not (gen_dir / failing_target_name).exists()
    assert root.exists()  # root itself was not rolled back

    # Retry against the same (now partially-populated) root fails closed.
    monkeypatch.undo()
    retry = builder._materialize_plan_at(root, plan)
    assert retry.ok is False
    assert retry.reason_code == builder.REASON_ROOT_ALREADY_EXISTS


def test_BLD_20_independent_verifier_and_full_validation_succeed(tmp_path):
    """Mandatory test closing WP-OGR-03-BLD-FINDING-03: materializes the accepted
    plan, independently invokes the real read-only resolve_active_generation()/
    verify_active_generation() production verifiers against it, then confirms the
    strengthened validate_case_a_fixture() (which internally does the same) also
    succeeds."""
    plan = builder.build_case_a_fixture_plan()
    root = tmp_path / "root"
    result = builder._materialize_plan_at(root, plan)
    assert result.ok, result.reason

    resolved = resolve_active_generation(root)
    assert resolved.ok, resolved.reason
    assert resolved.generation_id == "0000000001"

    verified = verify_active_generation(root)
    assert verified.ok, verified.reason
    assert verified.actual_digest == plan.generation_digest

    validation = builder.validate_case_a_fixture(root)
    assert validation.ok, validation.problems


def test_BLD_21_independent_validation_catches_defect_plan_equality_alone_would_miss(tmp_path):
    """Negative test closing WP-OGR-03-BLD-FINDING-03: constructs a plan that is
    internally self-consistent (materializing it and comparing disk against that
    same plan byte-for-byte -- the old, pre-repair validation -- would trivially
    pass) but semantically broken: the candidate file's content no longer matches
    what the recorded generation_digest was computed from. Only the real
    verify_active_generation() re-derivation (added by this repair) can detect
    this, proving the independent validation path adds real value beyond plan
    self-consistency."""
    plan = builder.build_case_a_fixture_plan()
    corrupted_candidate_content = (
        b"# corrupted\n\nCandidate Part 1 Status: NON-CANONICAL / UNREVIEWED / NOT ACCEPTED\n"
    )
    corrupted_files = tuple(
        (name, corrupted_candidate_content) if name == builder.CANDIDATE_NAME else (name, content)
        for name, content in plan.generation_files
    )
    corrupted_plan = dataclasses.replace(plan, generation_files=corrupted_files)

    root = tmp_path / "root"
    result = builder._materialize_plan_at(root, corrupted_plan)
    assert result.ok, result.reason  # materialization only writes bytes; it does not verify them

    # The old-style check (disk matches the very plan used to build it) would pass:
    for name, content in corrupted_plan.generation_files:
        assert (root / "generations" / "gen-0000000001" / name).read_bytes() == content

    validation = builder.validate_case_a_fixture(root, plan=corrupted_plan)
    assert not validation.ok
    assert any("verify_active_generation" in p or "digest" in p for p in validation.problems)


def test_BLD_22_control_transaction_authorization_digest_mismatch_detected(tmp_path):
    """WP-OGR-03-BLD-FINDING-04 repair: validate_case_a_fixture() must independently
    catch an authorization_digest that is syntactically valid but wrong, via its own
    field-level check -- not merely as a side effect of the earlier V2 byte-equality
    check. Achieved by materializing the real, correct plan (every file's bytes on
    disk are genuinely correct and self-consistent) and then validating against a
    *different* expected plan whose authorization_digest alone has been swapped for
    a wrong-but-syntactically-valid value -- control_transaction_bytes/lock_bytes in
    that expected plan are untouched, so V2's byte-equality still passes and cannot
    be the thing that catches this; only the new field-level check can.
    """
    plan = builder.build_case_a_fixture_plan()
    root = tmp_path / "root"
    result = builder._materialize_plan_at(root, plan)
    assert result.ok, result.reason

    wrong_digest = "0" * 64
    assert wrong_digest != plan.authorization_digest
    plan_with_wrong_expected_digest = dataclasses.replace(plan, authorization_digest=wrong_digest)

    validation = builder.validate_case_a_fixture(root, plan=plan_with_wrong_expected_digest)

    assert not validation.ok
    assert any(
        "control_transaction.json authorization_digest" in p and "does not match" in p
        for p in validation.problems
    )


def test_BLD_23_lock_authorization_digest_mismatch_detected(tmp_path):
    """WP-OGR-03-BLD-FINDING-04 repair: same independent field-level check for the
    lock's authorization_digest, isolated the same way as BLD-22 -- disk bytes stay
    genuinely correct; only the expected plan's authorization_digest is wrong."""
    plan = builder.build_case_a_fixture_plan()
    root = tmp_path / "root"
    result = builder._materialize_plan_at(root, plan)
    assert result.ok, result.reason

    wrong_digest = "0" * 64
    assert wrong_digest != plan.authorization_digest
    plan_with_wrong_expected_digest = dataclasses.replace(plan, authorization_digest=wrong_digest)

    validation = builder.validate_case_a_fixture(root, plan=plan_with_wrong_expected_digest)

    assert not validation.ok
    assert any(
        "execution_lock.json authorization_digest" in p and "does not match" in p
        for p in validation.problems
    )


def test_materialize_then_validate_succeeds(tmp_path):
    root = tmp_path / "root"
    result = builder._materialize_plan_at(root, builder.build_case_a_fixture_plan())
    assert result.ok, result.reason

    validation = builder.validate_case_a_fixture(root)
    assert validation.ok, validation.problems


def test_fingerprint_file_entries_stable_across_independent_materializations(tmp_path):
    plan = builder.build_case_a_fixture_plan()
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    r1 = builder._materialize_plan_at(root1, plan)
    r2 = builder._materialize_plan_at(root2, plan)
    assert r1.ok and r2.ok

    fp1 = builder.fingerprint_case_a_fixture(root1)
    fp2 = builder.fingerprint_case_a_fixture(root2)
    assert fp1 == fp2  # identical relative paths/types/sizes/hashes, root differs only in name


def test_validate_detects_content_tamper(tmp_path):
    root = tmp_path / "root"
    result = builder._materialize_plan_at(root, builder.build_case_a_fixture_plan())
    assert result.ok, result.reason

    (root / "generations" / "gen-0000000001" / "source_01_layer_1.jsonl").write_bytes(b"tampered")

    validation = builder.validate_case_a_fixture(root)
    assert not validation.ok
    assert any("source_01_layer_1.jsonl" in p for p in validation.problems)
