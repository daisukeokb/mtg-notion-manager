import hashlib

from generation_workspace.bootstrap import bootstrap_generation_workspace
from generation_workspace.digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS
from generation_workspace.mutation_guard import MUTATION_AUTHORIZATION_REQUIRED
from generation_workspace.transaction import begin_generation_transaction

from .conftest import build_bootstrap_authorization, new_uuid

DIGEST_SCHEMA = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))


def test_T_018_only_bootstrap_creates_initial_pointer(flat_workspace):
    """T-018: begin_generation_transaction (the normal, non-bootstrap entry
    point) refuses to run before a Pointer exists; only
    bootstrap_generation_workspace may create the first one."""
    result = begin_generation_transaction(flat_workspace, new_uuid())
    assert not result.ok
    assert result.error_code == MUTATION_AUTHORIZATION_REQUIRED

    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert result.ok, result.reason
    assert (flat_workspace / "active_generation").is_file()


def test_T_019_double_bootstrap_rejected(bootstrapped_workspace):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(bootstrapped_workspace, txn_id)
    result = bootstrap_generation_workspace(
        bootstrapped_workspace, txn_id, DIGEST_SCHEMA, authorization
    )
    assert not result.ok
    assert "already exists" in result.reason


def _sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def test_T_030_bootstrap_copy_preserves_flat_baseline_sha(flat_workspace):
    before = {p.name: _sha256(p) for p in flat_workspace.iterdir() if p.is_file()}

    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    result = bootstrap_generation_workspace(flat_workspace, txn_id, DIGEST_SCHEMA, authorization)
    assert result.ok, result.reason

    published = flat_workspace / "generations" / "gen-0000000001"
    after = {p.name: _sha256(p) for p in published.iterdir() if p.is_file()}

    assert before == after  # byte-for-byte identical, Hard Link not used
