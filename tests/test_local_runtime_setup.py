"""Phase 3H-R5: the local Python 3.14 environment (scripts/setup_local_venv.py and
scripts/check_local_runtime.py).

Offline: the download is replaced by a fake and no environment is created. The
audit hook of the check is only installed in a child process.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "setup_local_venv.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("setup_local_venv_r5", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commands() -> list[list[str]]:
    """The literal argument lists of the run(...) calls in the script."""
    text = SCRIPT.read_text(encoding="utf-8")
    calls = re.findall(r"\brun\((python, .*?)\)\n", text, re.S)
    return [re.findall(r'"([^"]+)"', call) for call in calls]


def test_local_python_is_the_ci_python() -> None:
    versions = set(re.findall(r'python-version: "([^"]+)"', WORKFLOW.read_text(encoding="utf-8")))
    assert versions == {_load().LOCAL_PYTHON}


def test_installs_are_the_hash_locked_and_offline_ci_installs() -> None:
    installs = [args for args in _commands() if "install" in args]
    assert len(installs) == 3
    bootstrap, locked, local = installs
    for flag in ("--require-hashes", "--no-deps", "--no-index", "--only-binary=:all:"):
        assert flag in bootstrap
    assert locked[locked.index("install"):] == [
        "install", "--require-hashes", "--no-deps", "--only-binary=:all:", "-r"]
    assert re.search(r'"-r", LOCK\)', SCRIPT.read_text(encoding="utf-8"))
    assert _load().LOCK == "requirements/ci-lock.txt"
    for flag in ("--no-deps", "--no-build-isolation", "--no-index"):
        assert flag in local
    assert local[-4:] == ["-e", ".", "-e", "tools/generation_workspace"]
    for args in installs:
        assert not any(arg.startswith("--upgrade") or arg.startswith("--index") for arg in args)
    assert any(args[-2:] == ["pip", "check"] for args in _commands())


def test_venv_is_created_without_the_base_pip_and_after_the_wheel_is_verified() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "venv.EnvBuilder(with_pip=False)" in text
    body = text[text.index("def create("):]
    download, create = body.index("download_verified_pip("), body.index("EnvBuilder(")
    assert download < create < body.index('"pip", "install"')


def test_pip_pin_is_the_ci_pip_lock() -> None:
    lock = (REPO_ROOT / "requirements" / "ci-pip-lock.txt").read_text(encoding="utf-8")
    version, digest = _load().pinned_pip()
    assert f"pip=={version} \\\n    --hash=sha256:{digest}\n" in lock


@pytest.mark.parametrize("kind", ["directory", "file", "dangling-symlink"])
def test_existing_target_is_refused(tmp_path: Path, kind: str) -> None:
    module = _load()
    target = tmp_path / "venv"
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("", encoding="utf-8")
    else:
        target.symlink_to(tmp_path / "missing")
    with pytest.raises(module.SetupError, match="refusing to modify"):
        module.check_target(target)


def test_other_interpreter_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(module.platform, "python_version", lambda: "3.9.6")
    with pytest.raises(module.SetupError, match="expected CPython 3.14.10"):
        module.check_base_interpreter()


def test_dry_run_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    module = _load()
    monkeypatch.setattr(module, "check_base_interpreter", lambda: None)
    monkeypatch.setattr(module, "create", lambda target: pytest.fail("create() was called"))
    target = tmp_path / "new-venv"
    assert module.main([str(target), "--dry-run"]) == 0
    assert not target.exists()
    assert "dry-run: would create" in capsys.readouterr().out


def test_failures_exit_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(module, "check_base_interpreter", lambda: None)
    (tmp_path / "venv").mkdir()
    assert module.main([str(tmp_path / "venv")]) == 1
    assert module.main([]) == 2


class _Response:
    def __init__(self, url: str, data: bytes) -> None:
        self._url, self._data = url, data

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        return self._data


def test_download_is_verified_before_it_is_written(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    data = b"not the pinned wheel"
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda url, timeout: _Response(url, data))
    with pytest.raises(module.SetupError, match="does not match"):
        module.download_verified_pip("26.0.1", "0" * 64, tmp_path)
    assert list(tmp_path.iterdir()) == []
    wheel = module.download_verified_pip("26.0.1", hashlib.sha256(data).hexdigest(), tmp_path)
    assert wheel.read_bytes() == data


def test_download_that_leaves_https_is_refused(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda url, timeout: _Response("http://example.invalid/pip.whl", b""))
    with pytest.raises(module.SetupError, match="left HTTPS"):
        module.download_verified_pip("26.0.1", "0" * 64, tmp_path)


def test_pip_runs_without_user_configuration_or_credentials(
        monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    env = module.pip_environment()
    assert env["PIP_CONFIG_FILE"] == module.os.devnull
    assert not [key for key in env
                if key.startswith(("NOTION_", "PYTHON")) or key == "PIP_INDEX_URL"]


def test_local_environments_are_ignored_by_git() -> None:
    patterns = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv/" in patterns and ".venv-*/" in patterns


# --- scripts/check_local_runtime.py ---------------------------------------------------

CHECK = REPO_ROOT / "scripts" / "check_local_runtime.py"


def _load_check() -> Any:
    spec = importlib.util.spec_from_file_location("check_local_runtime_r5", CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_lists_every_cli_command() -> None:
    import typer

    from mtg_notion_manager.cli import app

    group = typer.main.get_command(app)
    assert tuple(sorted(getattr(group, "commands", {}))) == _load_check().COMMANDS


def test_check_reads_the_same_lock_files_as_setup() -> None:
    check, setup = _load_check(), _load()
    assert check.LOCK == REPO_ROOT / setup.LOCK
    assert check.PIP_LOCK == REPO_ROOT / setup.PIP_LOCK
    assert check.local_python() == setup.LOCAL_PYTHON


@pytest.mark.parametrize(("installed", "problem"), [
    ({"a": "1", "pip": "2"}, "is not installed"),
    ({"a": "1", "pip": "2", "mtg-notion-manager": "0", "wp-claim-exit-generation-workspace": "0",
      "extra": "9"}, "extra==9 is not in the lock"),
    ({"a": "0", "pip": "2", "mtg-notion-manager": "0", "wp-claim-exit-generation-workspace": "0"},
     "a: expected 1, installed 0"),
])
def test_distribution_differences_are_reported(installed: dict[str, str], problem: str) -> None:
    problems = _load_check().distribution_differences(installed, {"a": "1", "pip": "2"})
    assert any(problem in text for text in problems), problems


def test_exact_lock_has_no_differences() -> None:
    installed = {"a": "1", "pip": "2", "mtg-notion-manager": "0",
                 "wp-claim-exit-generation-workspace": "0"}
    assert _load_check().distribution_differences(installed, {"a": "1", "pip": "2"}) == []


@pytest.mark.parametrize(("event", "args", "blocked"), [
    ("open", ("/repo/.env", "r", 0), True),
    ("open", (b"/repo/.env.production", "r", 0), True),
    ("open", (Path("/repo/.env"), "r", 0), True),
    ("open", ("/repo/.env.example.txt", "r", 0), True),
    ("open", ("/repo/src/config.py", "r", 0), False),
    ("open", (3, "r", 0), False),
    ("socket.connect", (None, ("api.notion.com", 443)), True),
    ("socket.getaddrinfo", ("api.notion.com", 443, 0, 0, 0), True),
    ("subprocess.Popen", ("git", ["git"], None, None), True),
    ("os.system", (b"true",), True),
    ("import", ("json", None, [], [], []), False),
])
def test_blocked_reason(event: str, args: tuple[Any, ...], blocked: bool) -> None:
    assert (_load_check().blocked_reason(event, args) is not None) is blocked


# The audit hook cannot be removed again, so it is only installed in a child
# process (which gets no network and no credentials: it only opens a dummy file).
_CHILD = r"""
import importlib.util, sys
from pathlib import Path

import click, typer.main

spec = importlib.util.spec_from_file_location("check", sys.argv[1])
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
dummy = Path(sys.argv[2])


class Leaky(click.Command):
    def format_help(self, ctx, formatter):
        dummy.read_text()  # what a command that loads .env while rendering help would do
        super().format_help(ctx, formatter)


group = click.Group(commands=[Leaky("leaky")])
typer.main.get_command = lambda app: group
check.COMMANDS = ("leaky",)
problems = check.check_cli()
print("\n".join(problems))
sys.exit(0 if problems else 3)
"""


def test_check_cli_refuses_and_reports_an_env_read(tmp_path: Path) -> None:
    dummy = tmp_path / ".env"
    dummy.write_text("NOTION_API_KEY=test-dummy-not-a-real-secret\n", encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", "-c", _CHILD, str(CHECK), str(dummy)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            check=False, timeout=60)
    assert result.returncode == 0, result.stdout
    assert "refused: open of .env" in result.stdout
    assert "leaky --help: PermissionError" in result.stdout
