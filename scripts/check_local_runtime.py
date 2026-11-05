"""Phase 3H-R5: read-only check of a local environment made by setup_local_venv.py.

Run it with the interpreter of that environment:

    VENV_DIR/bin/python -I scripts/check_local_runtime.py

It checks that the interpreter is CPython LOCAL_PYTHON in a virtual
environment, that the installed distributions are exactly the lock (plus the
locked pip and the two local projects), runs `pip check`, and then renders the
`--help` of the CLI and of each of its commands in-process.

The CLI part runs under a CPython audit hook installed first, which refuses
(before it happens) opening any file named .env*, every socket connection,
send, bind and name lookup, and starting any process. No command body runs
(`--help` is handled before it), so Config.load() and the Notion client are
never reached; the hook makes that an observed fact instead of an assumption.
Nothing is written. Exit status: 0 = all checks passed, 1 = a check failed.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import io
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK = REPO_ROOT / "requirements" / "ci-lock.txt"
PIP_LOCK = REPO_ROOT / "requirements" / "ci-pip-lock.txt"
LOCAL_PROJECTS = {"mtg-notion-manager", "wp-claim-exit-generation-workspace"}
#: The commands of the CLI (CLAUDE.md); a new or missing command fails the check.
COMMANDS = (
    "apply-dedupe-plan", "apply-price-link-dedupe", "apply-single-title-update",
    "audit-duplicates", "dedupe-cards", "doctor", "import", "import-article",
    "import-cards", "plan-title-updates", "review-duplicate-conflicts", "verify-import",
)
_NETWORK_EVENTS = ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind",
                   "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                   "socket.getnameinfo")
_PROCESS_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                   "os.fork")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {_normalize(name): version
            for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==([^ \\]+)", text, re.M)}


def distribution_differences(installed: dict[str, str], expected: dict[str, str]) -> list[str]:
    """What differs between the installed distributions and the expected pins."""
    problems = []
    for name, version in sorted(expected.items()):
        if installed.get(name) != version:
            problems.append(f"{name}: expected {version}, installed {installed.get(name)}")
    for name in sorted(set(installed) - set(expected) - LOCAL_PROJECTS):
        problems.append(f"{name}=={installed[name]} is not in the lock")
    for name in sorted(LOCAL_PROJECTS - set(installed)):
        problems.append(f"{name} is not installed")
    return problems


def blocked_reason(event: str, args: tuple[Any, ...]) -> str | None:
    """Why the audit hook refuses this event (None when it is allowed)."""
    if event == "open" and args:
        path = args[0]
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, (str, os.PathLike)) and os.path.basename(
                os.fspath(path)).startswith(".env"):
            return f"open of {os.path.basename(os.fspath(path))}"
    if event in _NETWORK_EVENTS:
        return f"network ({event})"
    if event in _PROCESS_EVENTS:
        return f"process ({event})"
    return None


def local_python() -> str:
    """LOCAL_PYTHON of scripts/setup_local_venv.py (loaded by path, not imported)."""
    path = Path(__file__).resolve().parent / "setup_local_venv.py"
    spec = importlib.util.spec_from_file_location("setup_local_venv", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.LOCAL_PYTHON)


def check_environment() -> list[str]:
    problems = []
    expected = local_python()
    found = f"{platform.python_implementation()} {platform.python_version()}"
    if found != f"CPython {expected}":
        problems.append(f"interpreter is {found}, expected CPython {expected}")
    if sys.prefix == sys.base_prefix:
        problems.append(f"{sys.executable} is not in a virtual environment")
    installed = {_normalize(d.metadata["Name"]): d.version
                 for d in importlib.metadata.distributions()}
    problems += distribution_differences(installed, {**pins(LOCK), **pins(PIP_LOCK)})
    env = {key: value for key, value in os.environ.items() if not key.startswith("NOTION_")}
    result = subprocess.run([sys.executable, "-I", "-m", "pip", "check"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            check=False)
    if result.returncode != 0:
        problems.append(f"pip check: {result.stdout.strip()}")
    return problems


def make_hook(attempts: list[str]) -> Callable[[str, tuple[Any, ...]], None]:
    """An audit hook that records and refuses the events blocked_reason() names."""

    def hook(event: str, args: tuple[Any, ...]) -> None:
        reason = blocked_reason(event, args)
        if reason is not None:
            attempts.append(reason)
            raise PermissionError(f"blocked by check_local_runtime: {reason}")

    return hook


def check_cli() -> list[str]:
    attempts: list[str] = []
    sys.addaudithook(make_hook(attempts))  # cannot be removed; before the CLI is imported
    for key in [key for key in os.environ if key.startswith("NOTION_")]:
        del os.environ[key]

    import typer

    from mtg_notion_manager.cli import app

    group = typer.main.get_command(app)
    problems = []
    commands = tuple(sorted(getattr(group, "commands", {})))
    if commands != COMMANDS:
        problems.append(f"commands are {list(commands)}, expected {list(COMMANDS)}")
    for args in [["--help"], *[[name, "--help"] for name in commands]]:
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = group.main(args, prog_name="mtg-notion-manager", standalone_mode=False)
        except Exception as exc:  # reported as a failed check
            problems.append(f"{' '.join(args)}: {type(exc).__name__}: {exc}")
            continue
        if code not in (0, None) or "Usage:" not in out.getvalue():
            problems.append(f"{' '.join(args)}: exit {code}, no usage text")
    problems += [f"refused: {reason}" for reason in attempts]
    print(f"ok: --help of the CLI and its {len(commands)} commands rendered" if not problems
          else "CLI check failed")
    return problems


def main() -> int:
    problems = check_environment()
    if not problems:
        print(f"ok: CPython {platform.python_version()} at {sys.executable}; "
              "distributions match the lock; pip check passed")
        problems = check_cli()
    for problem in problems:
        print(f"LOCAL RUNTIME CHECK FAILED: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
