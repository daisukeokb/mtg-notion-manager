import sys
import uuid
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

CANDIDATE_STATUS_LINE = "Candidate Part 1 Status: NON-CANONICAL / UNREVIEWED / NOT ACCEPTED"


def new_uuid() -> str:
    return str(uuid.uuid4())


def write_flat_workspace(root: Path, source_count: int = 2, control_action: str = "CTL-INIT-01"):
    """Build a minimal, self-consistent flat (pre-Generation) workspace
    under `root`, analogous in *shape* to the real WP-CLAIM-EXIT workspace
    but with tiny synthetic content. Never touches production data.
    """
    root.mkdir(parents=True, exist_ok=True)
    source_names = [f"source_{i:02d}_layer_{i}.jsonl" for i in range(1, source_count + 1)]
    for name in source_names:
        (root / name).write_text('{"record_type": "CHECKPOINT_METADATA"}\n', encoding="utf-8")

    candidate_name = "candidate_part_01_deliverables_01_06.md"
    (root / candidate_name).write_text(
        "# Test Candidate\n\nSome content.\n\n" + CANDIDATE_STATUS_LINE + "\n",
        encoding="utf-8",
    )

    transaction_id = new_uuid()
    file_classification = {name: "SOURCE_CHECKPOINT" for name in source_names}
    file_classification[candidate_name] = "CANDIDATE_PART"
    manifest = {
        "workspace_version": "TEST-v1",
        "file_classification": file_classification,
        "completed_stage_files": source_names + [candidate_name],
        "last_control_action": control_action,
        "last_control_transaction_id": transaction_id,
    }
    state = {
        "source_checkpoint_files": source_names,
        "last_control_action": control_action,
        "last_control_transaction_id": transaction_id,
    }

    import json

    (root / "workspace_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / "workspace_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return source_names, candidate_name


@pytest.fixture
def flat_workspace(tmp_path):
    root = tmp_path / "workspace"
    write_flat_workspace(root)
    return root


def _sha256_of(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_bootstrap_authorization(workspace_root: Path, transaction_id: str):
    """A valid, fully-populated Test Authorization for
    BOOTSTRAP_GENERATION_WORKSPACE, computed by scanning the caller's own
    flat workspace (never the production WP-CLAIM-EXIT workspace)."""
    from generation_workspace.inventory import derive_expected_inventory
    from generation_workspace.mutation_guard import (
        AUTHORIZATION_SCHEMA_VERSION,
        OPERATION_SCOPE_BOOTSTRAP,
        ExpectedFile,
        MutationAuthorization,
    )

    expected_names = derive_expected_inventory(workspace_root)
    expected_files = [
        ExpectedFile(relative_path=name, sha256=_sha256_of(workspace_root / name))
        for name in expected_names
    ]
    return MutationAuthorization(
        authorization_schema_version=AUTHORIZATION_SCHEMA_VERSION,
        authorization_id=new_uuid(),
        operation_scope=OPERATION_SCOPE_BOOTSTRAP,
        transaction_id=transaction_id,
        workspace_root=str(workspace_root.resolve()),
        apply=True,
        source_generation_id=None,
        source_generation_digest=None,
        target_generation_id="0000000001",
        expected_stable_file_count=len(expected_files),
        expected_files=expected_files,
    )


def build_transaction_authorization(workspace_root: Path, transaction_id: str):
    """A valid Test Authorization for GENERATION_TRANSACTION, computed from
    the *current* active generation of the caller's own workspace."""
    from generation_workspace.mutation_guard import (
        AUTHORIZATION_SCHEMA_VERSION,
        OPERATION_SCOPE_TRANSACTION,
        ExpectedFile,
        MutationAuthorization,
    )
    from generation_workspace.resolver import resolve_active_generation
    from generation_workspace.transaction import _next_generation_id

    resolved = resolve_active_generation(workspace_root)
    assert resolved.ok, resolved.reason
    expected_files = [
        ExpectedFile(relative_path=p.name, sha256=_sha256_of(p))
        for p in sorted(resolved.generation_path.iterdir(), key=lambda p: p.name)
    ]
    return MutationAuthorization(
        authorization_schema_version=AUTHORIZATION_SCHEMA_VERSION,
        authorization_id=new_uuid(),
        operation_scope=OPERATION_SCOPE_TRANSACTION,
        transaction_id=transaction_id,
        workspace_root=str(workspace_root.resolve()),
        apply=True,
        source_generation_id=resolved.generation_id,
        source_generation_digest=resolved.pointer.generation_digest,
        target_generation_id=_next_generation_id(resolved.generation_id),
        expected_stable_file_count=len(expected_files),
        expected_files=expected_files,
    )


def bootstrap(workspace_root: Path):
    """Convenience: bootstrap `workspace_root` with a valid authorization."""
    from generation_workspace.bootstrap import bootstrap_generation_workspace
    from generation_workspace.digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS

    digest_schema = next(iter(SUPPORTED_DIGEST_SCHEMA_VERSIONS))
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(workspace_root, txn_id)
    result = bootstrap_generation_workspace(workspace_root, txn_id, digest_schema, authorization)
    assert result.ok, result.reason
    return result


@pytest.fixture
def bootstrapped_workspace(flat_workspace):
    bootstrap(flat_workspace)
    return flat_workspace
