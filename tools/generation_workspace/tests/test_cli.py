"""CLI-001..CLI-015: Production Migration CLI tests.

All tests invoke generation_workspace.cli.main(argv) in-process (never a
subprocess) against tmp_path-rooted synthetic workspaces. The production
WP-CLAIM-EXIT workspace is never referenced or executed against.
"""

import dataclasses
import json

import pytest
from generation_workspace import cli

from .conftest import build_bootstrap_authorization, new_uuid


def _write_authorization_file(path, authorization):
    obj = {
        "authorization_schema_version": authorization.authorization_schema_version,
        "authorization_id": authorization.authorization_id,
        "operation_scope": authorization.operation_scope,
        "transaction_id": authorization.transaction_id,
        "workspace_root": authorization.workspace_root,
        "apply": authorization.apply,
        "source_generation_id": authorization.source_generation_id,
        "source_generation_digest": authorization.source_generation_digest,
        "target_generation_id": authorization.target_generation_id,
        "expected_stable_file_count": authorization.expected_stable_file_count,
        "expected_files": [
            {"relative_path": f.relative_path, "sha256": f.sha256}
            for f in authorization.expected_files
        ],
    }
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def authorization_file(flat_workspace, tmp_path):
    txn_id = new_uuid()
    authorization = build_bootstrap_authorization(flat_workspace, txn_id)
    auth_path = tmp_path / "auth" / "bootstrap_authorization.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    _write_authorization_file(auth_path, authorization)
    return auth_path, authorization


def test_CLI_001_no_arguments_is_usage_error():
    code = cli.main([])
    assert code == cli.EXIT_USAGE


def test_CLI_002_default_mode_is_preflight_only(flat_workspace, authorization_file):
    auth_path, _ = authorization_file
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
        ]
    )
    assert code == cli.EXIT_OK
    assert not (flat_workspace / "active_generation").exists()


def test_CLI_003_preflight_success_creates_zero_files(flat_workspace, authorization_file):
    auth_path, _ = authorization_file
    before = sorted(p.name for p in flat_workspace.iterdir())
    cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
        ]
    )
    after = sorted(p.name for p in flat_workspace.iterdir())
    assert before == after


def test_CLI_004_apply_without_authorization_file_rejected(flat_workspace):
    code = cli.main(["bootstrap", "--workspace-root", str(flat_workspace), "--apply"])
    assert code == cli.EXIT_USAGE


def test_CLI_005_authorization_file_alone_does_not_mutate(flat_workspace, authorization_file):
    auth_path, _ = authorization_file
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
        ]
    )
    assert code == cli.EXIT_OK
    assert not (flat_workspace / "active_generation").exists()
    assert not (flat_workspace / "generations").exists()


def test_CLI_006_apply_with_authorized_workspace_bootstraps(flat_workspace, authorization_file):
    auth_path, _ = authorization_file
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--apply",
        ]
    )
    assert code == cli.EXIT_OK
    assert (flat_workspace / "active_generation").is_file()
    assert (flat_workspace / "generations" / "gen-0000000001").is_dir()


def test_CLI_007_unsupported_authorization_exit_code_3(flat_workspace, tmp_path):
    bad_path = tmp_path / "bad_auth.json"
    bad_path.write_text(
        json.dumps({"authorization_schema_version": "NOT-SUPPORTED"}), encoding="utf-8"
    )
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(bad_path),
        ]
    )
    assert code == cli.EXIT_AUTHORIZATION_REJECTED


def test_CLI_008_root_policy_violation_exit_code_4(tmp_path):
    import os

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    symlink_root = tmp_path / "symlinked"
    os.symlink(real_dir, symlink_root, target_is_directory=True)

    from .conftest import write_flat_workspace

    write_flat_workspace(real_dir)

    authorization = build_bootstrap_authorization(real_dir, new_uuid())
    authorization = dataclasses.replace(authorization, workspace_root=str(symlink_root))
    auth_path = tmp_path / "auth.json"
    _write_authorization_file(auth_path, authorization)

    code = cli.main(
        ["bootstrap", "--workspace-root", str(symlink_root), "--authorization-file", str(auth_path)]
    )
    assert code == cli.EXIT_ROOT_POLICY_REJECTED


def test_CLI_009_baseline_mismatch_exit_code_5(flat_workspace, authorization_file):
    auth_path, authorization = authorization_file
    tampered = flat_workspace / "source_01_layer_1.jsonl"
    tampered.write_text('{"record_type": "TAMPERED"}\n', encoding="utf-8")

    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
        ]
    )
    assert code == cli.EXIT_BASELINE_MISMATCH


def test_CLI_010_existing_lock_exit_code_6(flat_workspace, authorization_file):
    auth_path, _ = authorization_file
    lock_path = flat_workspace / ".execution_lock.json"
    lock_path.write_text('{"lock_id": "someone-else"}', encoding="utf-8")

    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--apply",
        ]
    )
    assert code == cli.EXIT_LOCK_CONFLICT
    lock_path.unlink()


def test_CLI_011_failed_requires_recovery_exit_code_8(
    flat_workspace, authorization_file, monkeypatch
):
    auth_path, _ = authorization_file

    def _boom(*args, **kwargs):
        from generation_workspace.bootstrap import BootstrapResult
        from generation_workspace.mutation_guard import FAILED_REQUIRES_RECOVERY

        return BootstrapResult(
            ok=False, reason="synthetic failure", error_code=FAILED_REQUIRES_RECOVERY
        )

    monkeypatch.setattr(cli, "bootstrap_generation_workspace", _boom)
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--apply",
        ]
    )
    assert code == cli.EXIT_FAILED_REQUIRES_RECOVERY


def test_CLI_012_report_file_inside_workspace_rejected(flat_workspace, authorization_file):
    auth_path, _ = authorization_file
    report_path = flat_workspace / "report.json"
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--report-file",
            str(report_path),
        ]
    )
    assert code == cli.EXIT_USAGE
    assert not report_path.exists()


def test_CLI_013_json_output_excludes_authorization_and_sha_lists(
    flat_workspace, authorization_file, capsys
):
    auth_path, authorization = authorization_file
    cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload.keys()) == {
        "mode",
        "operation",
        "authorization_id",
        "authorization_digest",
        "transaction_id",
        "workspace_root_verified",
        "baseline_file_count",
        "baseline_digest_status",
        "planned_generation_id",
        "preflight_status",
        "apply_requested",
        "mutation_started",
        "result",
        "error_code",
        "filesystem_writes",
    }
    for entry in authorization.expected_files:
        assert entry.sha256 not in captured.out


def test_CLI_014_expected_failure_never_raises_unhandled_traceback(flat_workspace):
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            "/no/such/file.json",
        ]
    )
    assert isinstance(code, int)
    assert code != cli.EXIT_OK


def test_CLI_015_apply_goes_through_function_level_guard(
    flat_workspace, authorization_file, monkeypatch
):
    """The CLI must call bootstrap_generation_workspace (which re-runs
    Phase A/Lock/Phase B) rather than writing to the filesystem itself."""
    auth_path, _ = authorization_file
    calls = []
    original = cli.bootstrap_generation_workspace

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "bootstrap_generation_workspace", _spy)
    code = cli.main(
        [
            "bootstrap",
            "--workspace-root",
            str(flat_workspace),
            "--authorization-file",
            str(auth_path),
            "--apply",
        ]
    )
    assert code == cli.EXIT_OK
    assert len(calls) == 1  # the guarded function, not a direct filesystem write, was used


# --- inspect-generation-recovery ---------------------------------------------


def test_CLI_016_inspect_generation_recovery_field_and_type_contract(
    bootstrapped_workspace, capsys
):
    """TEST-RI-08 (CLI layer): automatic_mutation/filesystem_writes/
    mutation_started exact values and types, plus the full 29-field
    Common(9) + Command-specific(20) output contract."""
    code = cli.main(
        ["inspect-generation-recovery", "--workspace-root", str(bootstrapped_workspace), "--json"]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    common_fields = {
        "schema_version",
        "mode",
        "operation",
        "workspace_root_verified",
        "result",
        "error_code",
        "mutation_started",
        "filesystem_writes",
        "recovery_required",
    }
    command_specific_fields = {
        "underlying_recovery_case",
        "underlying_recovery_status",
        "underlying_safe_action",
        "lock_present",
        "lock_metadata_status",
        "lock_transaction_id",
        "control_transaction_present",
        "control_transaction_tmp_present",
        "pointer_generation_id",
        "pointer_transaction_id",
        "current_verified_active_generation_id",
        "staging_present",
        "staging_entry_count",
        "generation_directories_present",
        "inspection_classification",
        "transaction_binding_status",
        "phase_origin",
        "transaction_commit_status",
        "safe_action",
        "automatic_mutation",
    }
    assert len(common_fields) == 9
    assert len(command_specific_fields) == 20
    assert set(payload.keys()) == common_fields | command_specific_fields

    assert payload["automatic_mutation"] == "PROHIBITED"
    assert isinstance(payload["automatic_mutation"], str)
    assert payload["filesystem_writes"] == 0
    assert isinstance(payload["filesystem_writes"], int)
    assert payload["mutation_started"] is False
    assert isinstance(payload["mutation_started"], bool)


def test_CLI_017_inspect_generation_recovery_no_na_in_json(tmp_path, bootstrapped_workspace):
    for workspace in (tmp_path / "never_bootstrapped", bootstrapped_workspace):
        workspace.mkdir(parents=True, exist_ok=True)
        code = None
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(
                ["inspect-generation-recovery", "--workspace-root", str(workspace), "--json"]
            )
        assert code == cli.EXIT_OK
        assert '"N/A"' not in buf.getvalue()
        assert "N/A" not in buf.getvalue()


def test_CLI_018_inspect_generation_recovery_is_read_only(bootstrapped_workspace):
    def _fingerprint():
        return sorted(
            str(p.relative_to(bootstrapped_workspace)) for p in bootstrapped_workspace.rglob("*")
        )

    before = _fingerprint()
    code = cli.main(
        ["inspect-generation-recovery", "--workspace-root", str(bootstrapped_workspace), "--json"]
    )
    after = _fingerprint()

    assert code == cli.EXIT_OK
    assert before == after


def test_CLI_019_inspect_generation_recovery_missing_workspace_root_usage_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    code = cli.main(["inspect-generation-recovery", "--workspace-root", str(missing), "--json"])
    assert code == cli.EXIT_USAGE
