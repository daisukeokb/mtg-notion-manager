"""Phase 3H-R1 (Layer B): configuration and self-check logic tests.

These run in the normal pytest suite (no Docker, no network): they read the
isolation configuration files and unit-test the pure helpers of
ci/isolated-tests/verify_isolation.py, so that weakening the isolation (for
example dropping `--network none`, adding a volume mount, or adding a
non-isolated pytest step to CI) fails the tests even where Docker is not
available. The actual container isolation is verified in CI by
scripts/run_isolated_tests.sh and scripts/check_isolation_red.sh.
"""

from __future__ import annotations

import errno
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "scripts" / "run_isolated_tests.sh"
RED = REPO_ROOT / "scripts" / "check_isolation_red.sh"
DOCKERFILE = REPO_ROOT / "ci" / "isolated-tests" / "Dockerfile"
IN_CONTAINER = REPO_ROOT / "ci" / "isolated-tests" / "run_in_container.sh"
VERIFY = REPO_ROOT / "ci" / "isolated-tests" / "verify_isolation.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _load_verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location("layer_b_verify_isolation", VERIFY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = _load_verify()


def _docker_run_block(script: str) -> str:
    match = re.search(r"^docker run --rm \\\n(?:.*\\\n)*.*$", script, flags=re.MULTILINE)
    assert match, "docker run block not found"
    return match.group(0)


# --- runner: mandatory isolation options ----------------------------------------


@pytest.mark.parametrize(
    "option",
    [
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--user 10001:10001",
        "--pids-limit",
        "--tmpfs /tmp:",
        '"PYTEST_ADDOPTS=-p no:cacheprovider"',
    ],
)
def test_runner_uses_mandatory_isolation_options(option: str) -> None:
    assert option in _docker_run_block(RUNNER.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "forbidden",
    ["-v ", "--volume", "--mount", "--privileged", "docker.sock", "--env-file", "--network host",
     "--network bridge", "--net=", "$NOTION", "${NOTION"],
)
def test_runner_has_no_mounts_privileges_or_host_secrets(forbidden: str) -> None:
    assert forbidden not in _docker_run_block(RUNNER.read_text(encoding="utf-8"))


def test_runner_has_no_non_isolated_fallback() -> None:
    script = RUNNER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert 'command -v docker' in script and "exit 2" in script
    # The only pytest invocations live in the container entrypoint.
    assert "pytest" not in re.sub(r"#.*", "", script)


def test_runner_builds_from_committed_head_only() -> None:
    script = RUNNER.read_text(encoding="utf-8")
    assert 'git init -q "$SRC"' in script
    assert 'fetch -q --depth=1 "file://$REPO_ROOT" "$HEAD_SHA"' in script
    build = 'docker build --quiet -f "$SRC/ci/isolated-tests/Dockerfile" -t "$IMAGE" "$SRC"'
    assert build in script


def test_dummy_credentials_match_everywhere() -> None:
    import conftest

    runner = RUNNER.read_text(encoding="utf-8")
    for name, value in conftest.TEST_DUMMY_ENV.items():
        assert verify.EXPECTED_DUMMY_ENV[name] == value
        assert f"-e {name}={value}" in runner


# --- container entrypoint, image, workflow ------------------------------------


def test_container_entrypoint_runs_self_check_first() -> None:
    lines = [
        line.strip()
        for line in IN_CONTAINER.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "set -eu" in lines
    commands = [line for line in lines if not line.startswith(("set ", "cd ", "echo "))]
    assert commands[0] == "python ci/isolated-tests/verify_isolation.py /app"
    # A caller-supplied command only runs after the self-check.
    assert commands[1].startswith('if [ "$#" -gt 0 ]')
    assert "pytest" in commands
    assert "pytest tools/generation_workspace/tests" in commands


def test_dockerfile_is_pinned_unprivileged_and_rejects_dotenv() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.9-slim-bookworm@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "USER 10001:10001" in dockerfile
    assert "find /app -name .env" in dockerfile


def test_dockerignore_excludes_dotenv() -> None:
    entries = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    assert ".env" in entries
    assert "**/.env" in entries


def test_workflow_test_job_is_isolated_and_has_no_plain_pytest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    test_job = workflow.split("\n  test:\n", 1)[1].split("\n  lint:\n", 1)[0]
    assert "persist-credentials: false" in test_job
    assert "run: bash scripts/run_isolated_tests.sh\n" in test_job
    assert "run: bash scripts/check_isolation_red.sh" in test_job
    assert "secrets." not in workflow
    assert workflow.count("persist-credentials: false") == workflow.count(
        "uses: actions/checkout@"
    ) - _jobs_without_code_execution(workflow)
    # Every step that executes repository code goes through the isolated runner.
    for line in workflow.splitlines():
        command = line.strip()
        if not command.startswith("run:"):
            continue
        body = command[len("run:"):].strip()
        executes_code = re.match(
            r"(python -m )?(pytest|generation-workspace|generation_workspace)", body
        ) or body.startswith("python -m generation_workspace")
        assert not executes_code, f"non-isolated execution step: {command}"


def test_red_check_script_covers_each_isolation_property() -> None:
    red = RED.read_text(encoding="utf-8")
    for case in (
        "network interface present",
        "non-dummy credential",
        "secret-like environment variable",
        ".env present",
        "running as root",
        "writable application directory",
        "docker socket mounted",
        "docker unavailable (no fallback)",
    ):
        assert f'expect_failure "{case}"' in red


# --- verify_isolation.py pure helpers -----------------------------------------


def test_external_interfaces_only_loopback_allowed() -> None:
    assert verify.external_interfaces(["lo"]) == []
    assert verify.external_interfaces(["lo", "eth0", "docker0"]) == ["docker0", "eth0"]


def test_ipv4_route_table_parsing() -> None:
    header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
    assert verify.ipv4_routes(header) == []
    assert verify.ipv4_routes(header + "eth0\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\n")
    assert verify.ipv4_routes("") == []


def test_ipv6_route_table_parsing() -> None:
    lo = "00000000000000000000000000000001 80 0000 00 0000 00 00000000 00000100 1 lo\n"
    eth = "fe800000000000000000000000000000 40 0000 00 0000 00 00000100 00000001 1 eth0\n"
    assert verify.ipv6_non_loopback_routes(lo) == []
    assert verify.ipv6_non_loopback_routes(lo + eth) == [eth.rstrip("\n")]


def test_secretish_env_names() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/home",
        "GPG_KEY": "public-key-id",
        **verify.EXPECTED_DUMMY_ENV,
    }
    assert verify.secretish_env_names(environ) == []
    assert verify.secretish_env_names({**environ, "GITHUB_TOKEN": "x", "MY_SECRET": "y"}) == [
        "GITHUB_TOKEN",
        "MY_SECRET",
    ]


def test_dummy_env_mismatches() -> None:
    assert verify.dummy_env_mismatches(dict(verify.EXPECTED_DUMMY_ENV)) == []
    changed = {**verify.EXPECTED_DUMMY_ENV, "NOTION_API_KEY": "other"}
    assert verify.dummy_env_mismatches(changed) == ["NOTION_API_KEY"]
    assert verify.dummy_env_mismatches({}) == sorted(verify.EXPECTED_DUMMY_ENV)


def test_dotenv_files_detection(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / ".env").write_text("ignored inside .git\n", encoding="utf-8")
    assert verify.dotenv_files(tmp_path) == []
    (tmp_path / "sub" / ".env").write_text("DUMMY=1\n", encoding="utf-8")
    assert verify.dotenv_files(tmp_path) == [str(tmp_path / "sub" / ".env")]


def test_unreachable_error_classification() -> None:
    assert verify.is_unreachable_error(OSError(errno.ENETUNREACH, "Network is unreachable"))
    assert verify.is_unreachable_error(OSError(errno.EHOSTUNREACH, "No route to host"))
    assert not verify.is_unreachable_error(OSError(errno.ECONNREFUSED, "refused"))
    assert not verify.is_unreachable_error(TimeoutError("timed out"))


def test_structure_check_refuses_non_linux_before_any_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(verify.IsolationCheckFailed, match="not Linux"):
        verify.check_structure()


def _jobs_without_code_execution(workflow: str) -> int:
    """lint / type-check jobs only run static analysis and keep the default checkout."""
    return sum(1 for job in ("\n  lint:\n", "\n  type-check:\n") if job in workflow)
