"""PG-001..PG-024: Production Mutation Guard tests.

Every test in this file uses only tmp_path-rooted synthetic workspaces; the
production WP-CLAIM-EXIT workspace is never referenced.
"""

import dataclasses
import os
import shutil

import pytest
from generation_workspace import mutation_guard as guard_module
from generation_workspace.bootstrap import bootstrap_generation_workspace
from generation_workspace.digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS
from generation_workspace.mutation_guard import (
    AUTHORIZATION_APPLY_REQUIRED,
    AUTHORIZATION_BASELINE_COUNT_MISMATCH,
    AUTHORIZATION_BASELINE_INVENTORY_MISMATCH,
    AUTHORIZATION_BASELINE_SHA_MISMATCH,
    AUTHORIZATION_DIGEST_MISMATCH,
    AUTHORIZATION_DUPLICATE_PATH,
    AUTHORIZATION_FIELD_MISSING,
    AUTHORIZATION_FILE_REJECTED,
    AUTHORIZATION_OPERATION_MISMATCH,
    AUTHORIZATION_SCHEMA_UNSUPPORTED,
    AUTHORIZATION_TRANSACTION_MISMATCH,
    AUTHORIZATION_UNKNOWN_FIELD,
    AUTHORIZATION_UNSAFE_PATH,
    AUTHORIZATION_WORKSPACE_MISMATCH,
    LOCK_OWNERSHIP_MISMATCH,
    MUTATION_AUTHORIZATION_REQUIRED,
    ROOT_POLICY_REJECTED,
    ROOT_SYMLINK_REJECTED,
    SOURCE_GENERATION_MISMATCH,
    ExpectedFile,
    GuardRejection,
    enforce_root_policy,
    find_repository_root,
    load_authorization_file,
    parse_authorization_obj,
    release_lock_if_owned,
)
from generation_workspace.resolver import resolve_active_generation
from generation_workspace.transaction import (
    begin_generation_transaction,
    commit_generation_transaction,
)

from .conftest import build_bootstrap_authorization, build_transaction_authorization, new_uuid

DIGEST_SCHEMA = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))


def _raw_auth_obj(workspace_root, target="0000000001", transaction_id=None):
    return {
        "authorization_schema_version": "WP-CLAIM-EXIT-MUTATION-AUTHORIZATION-v1",
        "authorization_id": new_uuid(),
        "operation_scope": "BOOTSTRAP_GENERATION_WORKSPACE",
        "transaction_id": transaction_id or new_uuid(),
        "workspace_root": str(workspace_root),
        "apply": True,
        "source_generation_id": None,
        "source_generation_digest": None,
        "target_generation_id": target,
        "expected_stable_file_count": 0,
        "expected_files": [],
    }


def _snapshot(workspace_root):
    return sorted(p.name for p in workspace_root.iterdir())


def test_PG_001_authorization_absent_rejected_zero_writes(flat_workspace):
    before = _snapshot(flat_workspace)
    txn_id = new_uuid()
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, None)
    assert not result.ok
    assert result.error_code == MUTATION_AUTHORIZATION_REQUIRED
    assert result.filesystem_writes == 0
    assert _snapshot(flat_workspace) == before


def test_PG_002_apply_false_rejected_zero_writes(flat_workspace):
    before = _snapshot(flat_workspace)
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    authorization = dataclasses.replace(authorization, apply=False)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_APPLY_REQUIRED
    assert result.filesystem_writes == 0
    assert _snapshot(flat_workspace) == before


@pytest.mark.parametrize("mutation", ["unsupported_value", "missing_field"])
def test_PG_003_authorization_schema_unsupported_or_missing(flat_workspace, mutation):
    obj = _raw_auth_obj(flat_workspace)
    if mutation == "unsupported_value":
        obj["authorization_schema_version"] = "SOME-OTHER-SCHEMA-v9"
        expected = AUTHORIZATION_SCHEMA_UNSUPPORTED
    else:
        del obj["authorization_schema_version"]
        expected = AUTHORIZATION_FIELD_MISSING
    with pytest.raises(GuardRejection) as excinfo:
        parse_authorization_obj(obj)
    assert excinfo.value.error_code == expected


@pytest.mark.parametrize("mutation", ["unknown_field", "missing_required"])
def test_PG_004_unknown_or_missing_field_rejected(flat_workspace, mutation):
    obj = _raw_auth_obj(flat_workspace)
    if mutation == "unknown_field":
        obj["not_a_real_field"] = 1
        expected = AUTHORIZATION_UNKNOWN_FIELD
    else:
        del obj["apply"]
        expected = AUTHORIZATION_FIELD_MISSING
    with pytest.raises(GuardRejection) as excinfo:
        parse_authorization_obj(obj)
    assert excinfo.value.error_code == expected


def test_PG_005_operation_scope_mismatch_rejected(bootstrapped_workspace):
    txn_id = new_uuid()
    bootstrap_style_auth = build_bootstrap_authorization(bootstrapped_workspace, txn_id)
    result = begin_generation_transaction(bootstrapped_workspace, txn_id, bootstrap_style_auth)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_OPERATION_MISMATCH


def test_PG_006_transaction_id_mismatch_rejected(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    result = begin_generation_transaction(bootstrapped_workspace, new_uuid(), authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_TRANSACTION_MISMATCH


def test_PG_007_workspace_root_mismatch_rejected(bootstrapped_workspace, tmp_path):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    other_root = tmp_path / "unrelated"
    other_root.mkdir()
    authorization = dataclasses.replace(authorization, workspace_root=str(other_root))
    result = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_WORKSPACE_MISMATCH


def test_PG_008_symlink_root_rejected(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    symlink_root = tmp_path / "symlinked"
    os.symlink(real_dir, symlink_root, target_is_directory=True)
    txn_id = new_uuid()
    obj = _raw_auth_obj(symlink_root, transaction_id=txn_id)
    authorization = parse_authorization_obj(obj)
    result = bootstrap_generation_workspace(symlink_root, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == ROOT_SYMLINK_REJECTED


def test_PG_008b_symlink_authorization_file_rejected(flat_workspace, tmp_path):
    real_file = tmp_path / "real_auth.json"
    real_file.write_text("{}", encoding="utf-8")
    symlinked_file = tmp_path / "symlinked_auth.json"
    os.symlink(real_file, symlinked_file)
    with pytest.raises(GuardRejection) as excinfo:
        load_authorization_file(symlinked_file, repo_root=tmp_path, workspace_root=flat_workspace)
    assert excinfo.value.error_code == AUTHORIZATION_FILE_REJECTED


def test_PG_009_filesystem_root_home_and_repo_root_rejected(flat_workspace):
    from pathlib import Path

    repo_root = find_repository_root(Path(__file__))
    authorization = build_bootstrap_authorization(flat_workspace, new_uuid())

    for candidate in (Path("/"), Path.home(), repo_root, repo_root.parent):
        with pytest.raises(GuardRejection) as excinfo:
            enforce_root_policy(str(candidate), authorization)
        assert excinfo.value.error_code == ROOT_POLICY_REJECTED


def test_PG_010_stable_file_count_mismatch_rejected(flat_workspace):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    authorization = dataclasses.replace(
        authorization, expected_stable_file_count=authorization.expected_stable_file_count + 1
    )
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_BASELINE_COUNT_MISMATCH


def test_PG_011_expected_inventory_mismatch_rejected(flat_workspace):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    files = list(authorization.expected_files)
    files[0] = ExpectedFile(relative_path="does_not_exist.jsonl", sha256=files[0].sha256)
    authorization = dataclasses.replace(authorization, expected_files=files)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_BASELINE_INVENTORY_MISMATCH


def test_PG_012_expected_sha_mismatch_rejected(flat_workspace):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    files = list(authorization.expected_files)
    files[0] = ExpectedFile(relative_path=files[0].relative_path, sha256="0" * 64)
    authorization = dataclasses.replace(authorization, expected_files=files)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_BASELINE_SHA_MISMATCH


@pytest.mark.parametrize(
    "bad_path,expected_code",
    [
        ("/etc/passwd", AUTHORIZATION_UNSAFE_PATH),
        ("../escape.jsonl", AUTHORIZATION_UNSAFE_PATH),
    ],
)
def test_PG_013_unsafe_relative_path_rejected(flat_workspace, bad_path, expected_code):
    obj = _raw_auth_obj(flat_workspace)
    obj["expected_files"] = [{"relative_path": bad_path, "sha256": "a" * 64}]
    obj["expected_stable_file_count"] = 1
    with pytest.raises(GuardRejection) as excinfo:
        parse_authorization_obj(obj)
    assert excinfo.value.error_code == expected_code


def test_PG_013b_duplicate_relative_path_rejected(flat_workspace):
    obj = _raw_auth_obj(flat_workspace)
    obj["expected_files"] = [
        {"relative_path": "source_01_layer_1.jsonl", "sha256": "a" * 64},
        {"relative_path": "source_01_layer_1.jsonl", "sha256": "b" * 64},
    ]
    obj["expected_stable_file_count"] = 2
    with pytest.raises(GuardRejection) as excinfo:
        parse_authorization_obj(obj)
    assert excinfo.value.error_code == AUTHORIZATION_DUPLICATE_PATH


def test_PG_014_commit_with_different_authorization_digest_rejected(bootstrapped_workspace):
    txn_id = new_uuid()
    begin_auth = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, begin_auth)
    assert begin.ok, begin.reason

    different_auth = dataclasses.replace(begin_auth, authorization_id=new_uuid())
    result = commit_generation_transaction(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, different_auth
    )
    assert not result.ok
    assert result.error_code == AUTHORIZATION_DIGEST_MISMATCH


@pytest.mark.parametrize("field", ["source_generation_id", "source_generation_digest"])
def test_PG_015_source_generation_mismatch_rejected(bootstrapped_workspace, field):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    bogus = "0" * 10 if field == "source_generation_id" else "0" * 64
    authorization = dataclasses.replace(authorization, **{field: bogus})
    result = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert not result.ok
    assert result.error_code == SOURCE_GENERATION_MISMATCH


def test_PG_016_target_generation_mismatch_or_reuse_rejected(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    wrong_target = dataclasses.replace(authorization, target_generation_id="0000000099")
    result = begin_generation_transaction(bootstrapped_workspace, txn_id, wrong_target)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_OPERATION_MISMATCH

    # Reuse of an *existing* target id (already published) is separately rejected.
    txn_id_2 = new_uuid()
    authorization_2 = build_transaction_authorization(bootstrapped_workspace, txn_id_2)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id_2, authorization_2)
    assert begin.ok, begin.reason
    resolved = resolve_active_generation(bootstrapped_workspace)
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, begin.staging_path / entry.name)
    commit = commit_generation_transaction(
        bootstrapped_workspace, txn_id_2, DIGEST_SCHEMA, authorization_2
    )
    assert commit.ok, commit.reason

    txn_id_3 = new_uuid()
    stale_reused_target_auth = dataclasses.replace(
        build_transaction_authorization(bootstrapped_workspace, txn_id_3),
        target_generation_id=authorization_2.target_generation_id,  # already exists now
    )
    result_2 = begin_generation_transaction(
        bootstrapped_workspace, txn_id_3, stale_reused_target_auth
    )
    assert not result_2.ok
    assert result_2.error_code == AUTHORIZATION_OPERATION_MISMATCH


def test_PG_017_toctou_baseline_change_after_authorization_caught_in_phase_b(
    bootstrapped_workspace,
):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)

    resolved = resolve_active_generation(bootstrapped_workspace)
    tampered = resolved.generation_path / "source_01_layer_1.jsonl"
    tampered.write_text('{"record_type": "TAMPERED_AFTER_AUTHORIZATION"}\n', encoding="utf-8")

    result = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_BASELINE_SHA_MISMATCH
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()
    assert not (bootstrapped_workspace / ".execution_lock.json").exists()


def test_PG_018_phase_a_failure_creates_zero_artifacts(flat_workspace):
    before = _snapshot(flat_workspace)
    result = bootstrap_generation_workspace(flat_workspace, new_uuid(), DIGEST_SCHEMA, None)
    assert not result.ok
    assert result.filesystem_writes == 0
    assert _snapshot(flat_workspace) == before
    assert not (flat_workspace / ".execution_lock.json").exists()


def test_PG_019_phase_b_failure_leaves_only_no_artifacts(flat_workspace):
    before = _snapshot(flat_workspace)
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    files = list(authorization.expected_files)
    files[0] = ExpectedFile(relative_path=files[0].relative_path, sha256="f" * 64)
    authorization = dataclasses.replace(authorization, expected_files=files)

    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == AUTHORIZATION_BASELINE_SHA_MISMATCH
    assert _snapshot(flat_workspace) == before  # lock was created then cleaned up
    assert not (flat_workspace / ".execution_lock.json").exists()
    assert not (flat_workspace / "active_generation").exists()
    assert not list(flat_workspace.glob(".staging-*"))


def test_PG_020_lock_ownership_mismatch_not_deleted(flat_workspace):
    lock_path = flat_workspace / ".execution_lock.json"
    lock_path.write_text(
        '{"lock_schema_version":"WP-CLAIM-EXIT-LOCK-v1","lock_id":"owner-a",'
        '"transaction_id":"t-a","authorization_id":"a-a","authorization_digest":"d-a",'
        '"created_at_utc":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    result = release_lock_if_owned(
        flat_workspace,
        lock_filename=".execution_lock.json",
        lock_id="owner-b",
        transaction_id="t-b",
        authorization_id="a-b",
        authorization_digest="d-b",
    )
    assert result.released is False
    assert result.error_code == LOCK_OWNERSHIP_MISMATCH
    assert lock_path.is_file()


def test_PG_020b_lock_ownership_mismatch_reachable_end_to_end(flat_workspace):
    """LOCK_OWNERSHIP_MISMATCH must be reachable as an actual GuardResult,
    not only via the lower-level release_lock_if_owned() helper."""
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)

    # Sabotage: after Guard acquires its own lock, something else rewrites
    # it mid-flight (simulated here by pre-seeding a foreign lock file
    # *before* the run, forcing acquire_lock's O_EXCL create to fail while
    # a Phase-B-style baseline mismatch is also engineered so the code path
    # that attempts an owned-lock release is exercised deterministically).
    files = list(authorization.expected_files)
    files[0] = ExpectedFile(relative_path=files[0].relative_path, sha256="f" * 64)
    bad_authorization = dataclasses.replace(authorization, expected_files=files)

    lock_path = flat_workspace / ".execution_lock.json"
    real_lock_write = guard_module.acquire_lock

    def _acquire_then_sabotage(*args, **kwargs):
        payload = real_lock_write(*args, **kwargs)
        # Foreign process overwrites the lock we were just given.
        lock_path.write_text(
            '{"lock_schema_version":"WP-CLAIM-EXIT-LOCK-v1","lock_id":"foreign",'
            '"transaction_id":"foreign","authorization_id":"foreign",'
            '"authorization_digest":"foreign","created_at_utc":"2026-01-01T00:00:00Z"}',
            encoding="utf-8",
        )
        return payload

    guard_module.acquire_lock = _acquire_then_sabotage
    try:
        result = bootstrap_generation_workspace(
            flat_workspace, txn_id, DIGEST_SCHEMA, bad_authorization
        )
    finally:
        guard_module.acquire_lock = real_lock_write

    assert not result.ok
    assert result.error_code == LOCK_OWNERSHIP_MISMATCH
    assert lock_path.is_file()  # the foreign lock was correctly left untouched


def test_PG_021_authorized_test_authorization_allows_mutation(flat_workspace):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert result.ok, result.reason
    assert (flat_workspace / "active_generation").is_file()
    assert (flat_workspace / "generations" / "gen-0000000001").is_dir()


def test_PG_022_authorization_file_inside_repo_or_workspace_rejected(flat_workspace, tmp_path):
    from pathlib import Path

    repo_root = find_repository_root(Path(__file__))

    in_repo = repo_root / "tools" / "generation_workspace" / "_pg022_tmp_auth.json"
    in_repo.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(GuardRejection) as excinfo:
            load_authorization_file(in_repo, repo_root=repo_root, workspace_root=flat_workspace)
        assert excinfo.value.error_code == AUTHORIZATION_FILE_REJECTED
    finally:
        in_repo.unlink()

    in_workspace = flat_workspace / "_pg022_tmp_auth.json"
    in_workspace.write_text("{}", encoding="utf-8")
    with pytest.raises(GuardRejection) as excinfo:
        load_authorization_file(in_workspace, repo_root=repo_root, workspace_root=flat_workspace)
    assert excinfo.value.error_code == AUTHORIZATION_FILE_REJECTED


def test_PG_023_bootstrap_authorization_reuse_rejected(bootstrapped_workspace):
    txn_id = new_uuid()
    stale_authorization = build_bootstrap_authorization(bootstrapped_workspace, txn_id)
    result = bootstrap_generation_workspace(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, stale_authorization
    )
    assert not result.ok
    assert "already exists" in result.reason


def test_PG_024_transaction_authorization_reuse_rejected_by_source_generation(
    bootstrapped_workspace,
):
    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    begin = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert begin.ok, begin.reason
    resolved = resolve_active_generation(bootstrapped_workspace)
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, begin.staging_path / entry.name)
    commit = commit_generation_transaction(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, authorization
    )
    assert commit.ok, commit.reason

    # Reusing the *same* (now-stale) authorization for a brand new
    # transaction must fail: its source_generation_id/digest still points
    # at the generation that was active before the commit above. Bump only
    # target_generation_id to the now-correct "next" value so that this
    # test isolates the source-generation staleness from the (separately
    # covered, PG-016) target-generation staleness.
    from generation_workspace.transaction import _next_generation_id

    resolved_after_commit = resolve_active_generation(bootstrapped_workspace)
    reuse_txn_id = new_uuid()
    reused_authorization = dataclasses.replace(
        authorization,
        transaction_id=reuse_txn_id,
        target_generation_id=_next_generation_id(resolved_after_commit.generation_id),
    )
    result = begin_generation_transaction(
        bootstrapped_workspace, reuse_txn_id, reused_authorization
    )
    assert not result.ok
    assert result.error_code == SOURCE_GENERATION_MISMATCH


# --- Error Contract Closure: the 3 remaining codes without a direct,
# code-asserting test (found via audit before Final Report). ---


def test_PG_025_authorization_schema_invalid_reachable(flat_workspace):
    """AUTHORIZATION_SCHEMA_INVALID (distinct from *_UNSUPPORTED/*_MISSING):
    a structurally well-formed but semantically invalid field value, e.g.
    an authorization_id that is not a UUID."""
    obj = _raw_auth_obj(flat_workspace)
    obj["authorization_id"] = "not-a-uuid"
    from generation_workspace.mutation_guard import AUTHORIZATION_SCHEMA_INVALID

    with pytest.raises(GuardRejection) as excinfo:
        parse_authorization_obj(obj)
    assert excinfo.value.error_code == AUTHORIZATION_SCHEMA_INVALID


def test_PG_026_lock_already_exists_reachable(flat_workspace):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    lock_path = flat_workspace / ".execution_lock.json"
    lock_path.write_text('{"lock_id": "someone-elses-transaction"}', encoding="utf-8")

    from generation_workspace.mutation_guard import LOCK_ALREADY_EXISTS

    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert not result.ok
    assert result.error_code == LOCK_ALREADY_EXISTS
    lock_path.unlink()


def test_PG_027_target_generation_already_exists_reachable(bootstrapped_workspace):
    """Simulates a crashed prior transaction that reached
    GENERATION_PUBLISHED (Case B: the target Generation directory exists)
    but never switched the Pointer, then a fresh begin() attempt (whose
    source_generation_id still correctly matches, since the Pointer never
    moved) must be rejected because its computed target already exists."""
    import os
    import shutil

    from generation_workspace.digest import compute_generation_digest
    from generation_workspace.durability import fsync_dir, fsync_file
    from generation_workspace.mutation_guard import TARGET_GENERATION_ALREADY_EXISTS
    from generation_workspace.transaction import _next_generation_id

    resolved = resolve_active_generation(bootstrapped_workspace)
    orphan_id = _next_generation_id(resolved.generation_id)
    staging = bootstrapped_workspace / f".staging-gen-{orphan_id}"
    staging.mkdir()
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, staging / entry.name)
    compute_generation_digest(staging, orphan_id, DIGEST_SCHEMA)  # sanity: staging is valid
    for entry in staging.iterdir():
        fsync_file(entry)
    fsync_dir(staging)
    generations_dir = bootstrapped_workspace / "generations"
    os.rename(staging, generations_dir / f"gen-{orphan_id}")
    fsync_dir(generations_dir)
    # Pointer intentionally left untouched (Case B: orphaned published Generation).

    txn_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, txn_id)
    assert authorization.target_generation_id == orphan_id  # confirms the collision setup
    result = begin_generation_transaction(bootstrapped_workspace, txn_id, authorization)
    assert not result.ok
    assert result.error_code == TARGET_GENERATION_ALREADY_EXISTS
