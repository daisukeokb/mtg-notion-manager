"""Phase 3H-R2: CI supply chain and credential configuration tests.

Static checks (no network, no Docker) that fail when the hardening is weakened:
an action referenced by tag or branch, a checkout that keeps the token, a write
permission, an unpinned base image, or a Python package installed without the
hash-verified lock (or a lock that no longer matches pyproject.toml).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = REPO_ROOT / "ci" / "isolated-tests" / "Dockerfile"
RUNNER = REPO_ROOT / "scripts" / "run_isolated_tests.sh"
LOCK = REPO_ROOT / "requirements" / "ci-lock.txt"
PYPROJECTS = (
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "tools" / "generation_workspace" / "pyproject.toml",
)

#: Reviewed external actions. Adding one requires pinning it and updating this set.
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python"}
#: linux/amd64 manifest of python:3.9-slim-bookworm (see the Dockerfile header).
BASE_IMAGE_DIGEST = "sha256:03dee9ce00747bb5390e7b4ad00307b4dbc7790f7da77cc3581f93cfa99a6363"
PINNED_USES = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)@(?P<sha>[0-9a-f]{40}) # v\d+\.\d+\.\d+$")


def _uses_references() -> list[str]:
    refs = []
    for workflow in WORKFLOWS:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:-\s+)?uses:\s*(.+?)\s*$", line)
            if match:
                refs.append(match.group(1))
    return refs


def _lock_entries() -> dict[str, tuple[str, list[str]]]:
    text = LOCK.read_text(encoding="utf-8").replace("\\\n", " ")
    entries: dict[str, tuple[str, list[str]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        spec, *hashes = line.split()
        name, _, version = spec.partition("==")
        assert version, f"lock entry is not pinned with ==: {spec}"
        key = canonicalize_name(name)
        assert key not in entries, f"duplicate lock entry: {name}"
        entries[key] = (version, [h for h in hashes if h.startswith("--hash=")])
    return entries


# --- R2-01: GitHub Actions ------------------------------------------------------


def test_ci_is_the_only_workflow() -> None:
    # A new workflow must be reviewed (and added to these checks) explicitly.
    assert WORKFLOWS == [WORKFLOW]


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    refs = _uses_references()
    assert refs, "no uses: references found"
    for ref in refs:
        match = PINNED_USES.match(ref)
        assert match, f"action not pinned to a full commit SHA with a version comment: {ref}"
        assert match.group("repo") in ALLOWED_ACTIONS, f"unreviewed action: {ref}"


def test_each_action_uses_a_single_pinned_commit() -> None:
    by_repo: dict[str, set[str]] = {}
    for ref in _uses_references():
        repo, _, rest = ref.partition("@")
        by_repo.setdefault(repo, set()).add(rest)
    assert all(len(versions) == 1 for versions in by_repo.values()), by_repo


# --- R2-02: credentials and permissions -----------------------------------------


def test_every_checkout_drops_the_token() -> None:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    checkouts = [i for i, line in enumerate(lines) if "uses: actions/checkout@" in line]
    assert checkouts
    for index in checkouts:
        following = [line.strip() for line in lines[index + 1 : index + 3]]
        assert following == ["with:", "persist-credentials: false"], lines[index]


def test_workflow_permissions_are_read_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read\n", workflow, re.MULTILINE)
    assert not re.search(r":\s*write\b", workflow)
    assert "write-all" not in workflow
    assert "secrets." not in workflow
    assert "GITHUB_TOKEN" not in workflow
    assert "pull_request_target" not in workflow


# --- R2-03: base image ------------------------------------------------------------


def test_base_image_is_pinned_to_the_verified_amd64_digest() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    froms = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    assert froms == [f"python:3.9-slim-bookworm@{BASE_IMAGE_DIGEST}"]
    assert "--platform linux/amd64" in RUNNER.read_text(encoding="utf-8")


def test_runner_python_matches_the_image_python() -> None:
    image_python = re.search(r"\(Python (3\.9\.\d+)\)", DOCKERFILE.read_text(encoding="utf-8"))
    assert image_python
    versions = set(re.findall(r'python-version: "([^"]+)"', WORKFLOW.read_text(encoding="utf-8")))
    assert versions == {image_python.group(1)}


# --- R2-04: Python dependencies -----------------------------------------------------

_LOCKED_INSTALL = "pip install --require-hashes --no-deps -r requirements/ci-lock.txt"


def _pip_install_commands(text: str) -> list[str]:
    joined = text.replace("\\\n", " ")
    commands = []
    for line in joined.splitlines():
        for part in re.split(r"&&", line):
            if "pip install" in part:
                commands.append(" ".join(part.split()))
    return commands


@pytest.mark.parametrize("path", [WORKFLOW, DOCKERFILE], ids=["workflow", "dockerfile"])
def test_every_pip_install_is_hash_locked_or_offline(path: Path) -> None:
    commands = _pip_install_commands(path.read_text(encoding="utf-8"))
    assert any(_LOCKED_INSTALL in command for command in commands)
    for command in commands:
        offline_local = all(
            flag in command for flag in ("--no-deps", "--no-build-isolation", "--no-index")
        )
        assert _LOCKED_INSTALL in command or offline_local, f"unlocked install: {command}"
        assert "--upgrade" not in command, f"unpinned upgrade: {command}"


@pytest.mark.parametrize("path", [WORKFLOW, DOCKERFILE], ids=["workflow", "dockerfile"])
def test_installs_are_followed_by_pip_check(path: Path) -> None:
    assert "pip check" in path.read_text(encoding="utf-8")


def test_lock_entries_are_pinned_and_hashed() -> None:
    entries = _lock_entries()
    assert entries
    for name, (version, hashes) in entries.items():
        assert hashes, f"{name}=={version} has no --hash"
        for value in hashes:
            assert re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", value), value


def test_lock_satisfies_every_declared_requirement() -> None:
    entries = _lock_entries()
    declared: list[str] = ["editables"]  # hatchling editable build requirement
    for path in PYPROJECTS:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        declared += project.get("dependencies", [])
        declared += project.get("optional-dependencies", {}).get("dev", [])
        declared += data.get("build-system", {}).get("requires", [])
    for spec in declared:
        requirement = Requirement(spec)
        key = canonicalize_name(requirement.name)
        assert key in entries, f"{requirement.name} is not in {LOCK.name}"
        version = entries[key][0]
        assert requirement.specifier.contains(version, prereleases=True), (
            f"{requirement.name}=={version} does not satisfy {spec}"
        )


def test_lock_is_generated_by_the_lock_script() -> None:
    assert "GENERATED by scripts/lock_dependencies.py" in LOCK.read_text(encoding="utf-8")
    assert (REPO_ROOT / "scripts" / "lock_dependencies.py").is_file()
