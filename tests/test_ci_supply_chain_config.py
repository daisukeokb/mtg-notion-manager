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
#: linux/amd64 manifest of python:3.14.10-slim-trixie (see the Dockerfile header).
BASE_IMAGE_DIGEST = "sha256:ec6a14df5b63e3fbb3498e3240f7f640c230f9dca67683d23265db3866697f2d"
PINNED_USES = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)@(?P<sha>[0-9a-f]{40}) # v\d+\.\d+\.\d+$")


def _uses_references() -> list[str]:
    refs = []
    for workflow in WORKFLOWS:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:-\s+)?uses:\s*(.+?)\s*$", line)
            if match:
                refs.append(match.group(1))
            elif re.search(r"\buses\s*:", line):
                refs.append(line.strip())  # e.g. flow style {uses: ...}: never accepted
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
        following = [line.strip() for line in lines[index + 1 : index + 5]]
        assert following == [
            "with:",
            "persist-credentials: false",
            "- name: Verify no persisted Git credentials",
            "run: python3 -I scripts/check_no_persisted_credentials.py",
        ], lines[index]


def test_credential_check_reads_the_effective_config_and_prints_key_names_only() -> None:
    # Phase 3H-R3 replaced the shell script; its behaviour (including every
    # case the shell version covered) is tested in tests/test_credential_check.py.
    assert not (REPO_ROOT / "scripts" / "check_no_persisted_credentials.sh").exists()
    script = (REPO_ROOT / "scripts" / "check_no_persisted_credentials.py").read_text(
        encoding="utf-8"
    )
    for fragment in ('"--show-scope"', '"--show-origin"', '"--includes"', '"-z"', '"get-url"',
                     '"--all"', '"--push"', "redact(", "EXIT_ERROR"):
        assert fragment in script
    assert "credentials in URL" in script


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
    assert froms == [f"python:3.14.10-slim-trixie@{BASE_IMAGE_DIGEST}"]
    assert "--platform linux/amd64" in RUNNER.read_text(encoding="utf-8")


def test_runner_python_matches_the_image_python() -> None:
    image_python = re.search(r"\(Python (3\.\d+\.\d+)\)", DOCKERFILE.read_text(encoding="utf-8"))
    assert image_python
    versions = set(re.findall(r'python-version: "([^"]+)"', WORKFLOW.read_text(encoding="utf-8")))
    assert versions == {image_python.group(1)}


# --- R2-04: Python dependencies -----------------------------------------------------

_LOCKED_INSTALL = (
    "pip install --require-hashes --no-deps --only-binary=:all: -r requirements/ci-lock.txt"
)


def _pip_install_commands(text: str) -> list[str]:
    joined = text.replace("\\\n", " ")
    commands = []
    for line in joined.splitlines():
        for part in re.split(r"&&|;|\|\|", line):
            if re.search(r"\bpip3?(\.\d+)?\s+install\b", part):
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


def test_pip_lock_pins_one_hashed_wheel_matching_the_generator() -> None:
    # Phase 3H-R3 (D-1): the pip installed on the runner by bootstrap_locked_pip.sh.
    pip_lock = REPO_ROOT / "requirements" / "ci-pip-lock.txt"
    text = pip_lock.read_text(encoding="utf-8")
    assert "GENERATED by scripts/lock_dependencies.py" in text
    entries = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert len(entries) == 2
    version = re.fullmatch(r"pip==(\d+\.\d+(?:\.\d+)?) \\", entries[0])
    assert version, entries[0]
    assert re.fullmatch(r"    --hash=sha256:[0-9a-f]{64}", entries[1]), entries[1]
    generator = (REPO_ROOT / "scripts" / "lock_dependencies.py").read_text(encoding="utf-8")
    assert f'PIP_VERSION = "{version.group(1)}"' in generator
    # pip itself is not part of the dependency lock (it would be reinstalled mid-install).
    assert "pip" not in _lock_entries()


def test_pip_bootstrap_verifies_before_running_and_installs_offline() -> None:
    script = (REPO_ROOT / "scripts" / "bootstrap_locked_pip.sh").read_text(encoding="utf-8")
    download = script.index("curl --fail")
    verify = script.index("sha256sum --check --strict")
    install = script.index('python -I "$wheel/pip" install')
    assert download < verify < install
    assert "--proto '=https' --proto-redir '=https'" in script
    # Explicit result checks on every step instead of relying on `set -e`.
    assert "set -uo pipefail" in script.splitlines()
    assert not any(re.match(r"set -\w*e", line) for line in script.splitlines())
    for line in script.splitlines():
        if 'python -I "$wheel/pip" install' in line:
            assert "--require-hashes" in line and "--no-index" in line


def test_lock_is_generated_by_the_lock_script() -> None:
    assert "GENERATED by scripts/lock_dependencies.py" in LOCK.read_text(encoding="utf-8")
    assert (REPO_ROOT / "scripts" / "lock_dependencies.py").is_file()


# --- Phase 3H-R4: runtime modernization ----------------------------------------------

#: Python minors past their end of life when this was reviewed (2026-11-02,
#: https://peps.python.org/api/release-cycle.json). CI must not run on them.
END_OF_LIFE_MINORS = {"3.8", "3.9", "3.10"}


def _load_lock_script() -> object:
    import importlib.util

    path = REPO_ROOT / "scripts" / "lock_dependencies.py"
    spec = importlib.util.spec_from_file_location("lock_dependencies_r4", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_python_is_the_same_everywhere_and_supported() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    header = re.search(r"\(Python (3\.\d+\.\d+)\)", dockerfile)
    assert header
    python = header.group(1)
    minor = ".".join(python.split(".")[:2])
    assert minor not in END_OF_LIFE_MINORS
    # The FROM tag, the runner, the lock generator and the lock all name it.
    assert re.search(rf"^FROM python:{re.escape(python)}-slim-[a-z]+@sha256:[0-9a-f]{{64}}$",
                     dockerfile, re.M)
    versions = re.findall(r'python-version: "([^"]+)"', WORKFLOW.read_text(encoding="utf-8"))
    assert versions and set(versions) == {python}
    generator = (REPO_ROOT / "scripts" / "lock_dependencies.py").read_text(encoding="utf-8")
    assert f'TARGET_PYTHON = "{python}"' in generator
    assert f"# Target: CPython {minor} / manylinux x86_64." in LOCK.read_text(encoding="utf-8")
    # mypy analyses the CI dependencies, which may need this Python (click 8.5: >= 3.10).
    mypy = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]
    assert mypy["python_version"] == minor


def test_lock_holds_no_distribution_only_older_pythons_need() -> None:
    # tomli / exceptiongroup are backports for Python < 3.11; with the
    # markers evaluated for the CI target they are not part of the lock.
    entries = _lock_entries()
    assert "tomli" not in entries and "exceptiongroup" not in entries


def _item(name: str, version: str, *requires: str) -> dict:
    return {"metadata": {"name": name, "version": version, "requires_dist": list(requires)}}


def test_lock_generator_evaluates_markers_for_the_target() -> None:
    lock = _load_lock_script()
    items = [
        _item("app", "1.0", 'old-backport; python_version < "3.11"',
              'new-dep>=2; python_version >= "3.12"', "plugin[extra]",
              'win; sys_platform == "win32"'),
        _item("old-backport", "1.0"),
        _item("new-dep", "2.1"),
        _item("plugin", "1.0", 'extra-dep; extra == "extra"', 'unused; extra == "other"'),
        _item("extra-dep", "1.0"),
        _item("unused", "1.0"),
        _item("win", "1.0"),
    ]
    needed = [item["metadata"]["name"] for item in lock.target_closure(["app"], items)]
    assert needed == ["app", "extra-dep", "new-dep", "plugin"]


@pytest.mark.parametrize(
    "items",
    [
        # needed on the target, missing from the resolution (marker false for
        # the interpreter that resolved it)
        [_item("app", "1.0", 'new-dep; python_version >= "3.12"')],
        # needed on the target in a newer version than the resolution holds
        [_item("app", "1.0", 'click>=8.2.1; python_version >= "3.10"'), _item("click", "8.1.8")],
    ],
)
def test_lock_generator_rejects_a_resolution_the_target_cannot_use(items: list[dict]) -> None:
    lock = _load_lock_script()
    with pytest.raises(lock.TargetMismatch):
        lock.target_closure(["app"], items)
