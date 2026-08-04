import shutil

import pytest
from generation_workspace.digest import (
    SUPPORTED_DIGEST_SCHEMA_VERSIONS,
    UnsupportedDigestSchemaError,
    compute_generation_digest,
)
from generation_workspace.resolver import resolve_active_generation, verify_active_generation
from generation_workspace.transaction import (
    begin_generation_transaction,
    commit_generation_transaction,
)

from .conftest import build_transaction_authorization, new_uuid, write_flat_workspace

DIGEST_SCHEMA = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))


def test_T_012_digest_recomputable_after_transaction_cleanup(bootstrapped_workspace):
    """T-012: after the .control_transaction.json is gone (bootstrap already
    cleaned it up implicitly by never needing one committed), the digest can
    still be recomputed purely from Pointer + Generation files."""
    resolved = resolve_active_generation(bootstrapped_workspace)
    assert resolved.ok
    assert not (bootstrapped_workspace / ".control_transaction.json").exists()
    recomputed = compute_generation_digest(
        resolved.generation_path, resolved.generation_id, DIGEST_SCHEMA
    )
    assert recomputed == resolved.pointer.generation_digest


def test_T_013_digest_independent_of_transaction_id(tmp_path):
    """T-013: identical Generation content + identical generation_id yields
    identical digest regardless of which transaction_id produced it."""
    root_a = tmp_path / "a"
    write_flat_workspace(root_a)
    root_b = tmp_path / "b"
    write_flat_workspace(root_b)
    # Force identical content between the two flat baselines.
    shutil.rmtree(root_b)
    shutil.copytree(root_a, root_b)

    digest_a = compute_generation_digest(root_a, "0000000001", DIGEST_SCHEMA)
    digest_b = compute_generation_digest(root_b, "0000000001", DIGEST_SCHEMA)
    assert digest_a == digest_b  # different bootstrap calls would use different transaction_id


def test_T_014_digest_changes_with_generation_id(flat_workspace):
    digest_1 = compute_generation_digest(flat_workspace, "0000000001", DIGEST_SCHEMA)
    digest_2 = compute_generation_digest(flat_workspace, "0000000002", DIGEST_SCHEMA)
    assert digest_1 != digest_2  # relative_path embeds generation_id


def test_T_024_unsupported_digest_schema_version_rejected(flat_workspace):
    with pytest.raises(UnsupportedDigestSchemaError):
        compute_generation_digest(flat_workspace, "0000000001", "UNKNOWN-SCHEMA-v99")


def test_T_026_staging_and_published_digest_are_equal(bootstrapped_workspace):
    """T-026: Digest computed while content lived in .staging-gen-* equals
    the Digest recomputed after publish/rename; Staging directory name is
    not part of the digest input."""
    transaction_id = new_uuid()
    authorization = build_transaction_authorization(bootstrapped_workspace, transaction_id)
    begin = begin_generation_transaction(bootstrapped_workspace, transaction_id, authorization)
    assert begin.ok, begin.reason

    # Populate the staging generation with a second-generation-worthy copy
    # of the current published generation's content (same file set).
    resolved = resolve_active_generation(bootstrapped_workspace)
    for entry in resolved.generation_path.iterdir():
        shutil.copy2(entry, begin.staging_path / entry.name)

    staging_digest = compute_generation_digest(
        begin.staging_path, begin.target_generation, DIGEST_SCHEMA
    )

    commit = commit_generation_transaction(
        bootstrapped_workspace, transaction_id, DIGEST_SCHEMA, authorization
    )
    assert commit.ok, commit.reason

    published_path = bootstrapped_workspace / "generations" / f"gen-{commit.generation_id}"
    published_digest = compute_generation_digest(
        published_path, commit.generation_id, DIGEST_SCHEMA
    )

    assert staging_digest == published_digest == commit.generation_digest

    verified = verify_active_generation(bootstrapped_workspace)
    assert verified.ok
