"""INT-001..INT-006: Repository Integration tests.

INT-001 (editable install succeeds) is validated by the Local Validation /
CI install step itself, not by a pytest assertion here (a test cannot
assert its own installation precondition). INT-002..INT-006 are executable
here.

None of these tests ever reference or execute against the production
WP-CLAIM-EXIT workspace; all filesystem fixtures are tmp_path-rooted.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

from generation_workspace.model import parse_pointer
from generation_workspace.transaction import CASE_F_POINTER_INVALID, recover_generation_transaction

REPO_ROOT = Path(__file__).resolve().parents[3]


def _console_script_path() -> Path:
    # pip installs console_scripts into the same bin/ directory as the
    # interpreter running this test (true for both venvs and CI runners).
    suffix = ".exe" if sys.platform == "win32" else ""
    return Path(sys.executable).with_name("generation-workspace" + suffix)


def test_INT_002_console_script_help_exits_zero():
    script = _console_script_path()
    assert script.is_file(), (
        f"generation-workspace console script not found at {script}; "
        "editable install (tools/generation_workspace) must run before this test"
    )
    result = subprocess.run(
        [str(script), "--help"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "bootstrap" in result.stdout


def test_INT_003_module_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "generation_workspace", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert "bootstrap" in result.stdout


def test_INT_004_documented_public_interface_importable_after_install():
    import generation_workspace as pkg

    documented_names = [
        "resolve_active_generation",
        "verify_active_generation",
        "begin_generation_transaction",
        "commit_generation_transaction",
        "recover_generation_transaction",
        "bootstrap_generation_workspace",
        "compute_generation_digest",
    ]
    for name in documented_names:
        assert hasattr(pkg, name), f"public interface regression: {name} missing"
        assert callable(getattr(pkg, name))


def test_INT_005_dedicated_ci_job_configured():
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file()
    text = ci_path.read_text(encoding="utf-8")

    assert "generation-workspace:" in text, "dedicated generation-workspace CI job not found"
    # Existing jobs must remain untouched/present, not removed or renamed.
    for existing_job in ("test:", "lint:", "type-check:"):
        assert existing_job in text

    required_commands = [
        "pip install -e tools/generation_workspace",
        "pytest tools/generation_workspace/tests",
        "--collect-only",
        "ruff check tools/generation_workspace",
        "mypy tools/generation_workspace/generation_workspace",
        "generation-workspace --help",
        "python -m generation_workspace --help",
    ]
    for command in required_commands:
        assert command in text, f"CI job missing required command: {command!r}"


def _assert_case_f(bootstrapped_workspace, mutate) -> None:
    before = sorted(p.name for p in bootstrapped_workspace.rglob("*"))
    mutate(bootstrapped_workspace)
    snapshot_before_recovery = sorted(p.name for p in bootstrapped_workspace.rglob("*"))
    result = recover_generation_transaction(bootstrapped_workspace)
    after = sorted(p.name for p in bootstrapped_workspace.rglob("*"))

    assert result.case == "F"
    assert result.classification == CASE_F_POINTER_INVALID
    assert result.status == "FAILED_REQUIRES_RECOVERY"
    assert result.authoritative_generation is None
    assert result.automatic_mutation == "PROHIBITED"
    # Filesystem Mutation / Automatic Selection / Automatic Rollback: all 0.
    assert after == snapshot_before_recovery
    return before


def test_INT_006_case_f_pointer_missing(bootstrapped_workspace):
    def mutate(root):
        (root / "active_generation").unlink()

    _assert_case_f(bootstrapped_workspace, mutate)


def test_INT_006_case_f_pointer_empty(bootstrapped_workspace):
    def mutate(root):
        (root / "active_generation").write_bytes(b"")

    _assert_case_f(bootstrapped_workspace, mutate)


def test_INT_006_case_f_pointer_malformed_json(bootstrapped_workspace):
    def mutate(root):
        (root / "active_generation").write_bytes(b"{not valid json")

    _assert_case_f(bootstrapped_workspace, mutate)


def test_INT_006_case_f_unsupported_pointer_schema_version(bootstrapped_workspace):
    def mutate(root):
        pointer_path = root / "active_generation"
        pointer = parse_pointer(pointer_path.read_bytes())
        broken = dataclasses.replace(pointer, pointer_schema_version="UNSUPPORTED-v99")
        pointer_path.write_bytes(broken.to_canonical_bytes())

    _assert_case_f(bootstrapped_workspace, mutate)


def test_INT_006_case_f_invalid_generation_id(bootstrapped_workspace):
    def mutate(root):
        pointer_path = root / "active_generation"
        pointer = parse_pointer(pointer_path.read_bytes())
        broken = dataclasses.replace(pointer, generation_id="not-an-id")
        pointer_path.write_bytes(broken.to_canonical_bytes())

    _assert_case_f(bootstrapped_workspace, mutate)


# The sixth INT-006 condition (referenced Generation Directory missing) is
# covered by test_recovery.py::test_T_010_pointer_references_unknown_generation
# (pre-existing Failure Injection Matrix test, updated for the Case F result).
