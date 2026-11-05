"""Phase 3H-R5: create a NEW local virtual environment on the CI runtime.

Run it with the interpreter the environment is based on, which must be exactly
CPython LOCAL_PYTHON (the python-version of .github/workflows/ci.yml):

    BASE_PYTHON -I scripts/setup_local_venv.py VENV_DIR [--dry-run]

VENV_DIR must not exist: an existing environment (e.g. the current .venv) is
never modified, upgraded or replaced. --dry-run only runs the checks that come
before any download or file creation.

The steps are those of the CI jobs (.github/workflows/ci.yml):

1. pip from requirements/ci-pip-lock.txt: the wheel is downloaded over HTTPS
   and its sha256 is checked before any of its code runs; it is then
   installed by running pip from inside that verified wheel. The pip bundled
   with BASE_PYTHON is never run (the venv is created without pip).
2. requirements/ci-lock.txt with --require-hashes --no-deps --only-binary.
3. this repository and tools/generation_workspace, editable and offline
   (--no-index --no-build-isolation: the build backend comes from the lock).
4. pip check.

pip runs with the user's pip configuration ignored (PIP_CONFIG_FILE=os.devnull,
no PIP_* variables) and without NOTION_* variables. The script reads no .env
file and no credentials, does not import repository code and changes no shell
profile or PATH. README "ローカル実行環境(Python 3.14)" describes its use.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
import venv
from pathlib import Path

#: Must equal python-version in .github/workflows/ci.yml (tested).
LOCAL_PYTHON = "3.14.10"

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK = "requirements/ci-lock.txt"
PIP_LOCK = "requirements/ci-pip-lock.txt"
PIP_WHEEL_URL = "https://files.pythonhosted.org/packages/py3/p/pip/pip-{version}-py3-none-any.whl"


class SetupError(Exception):
    pass


def check_base_interpreter() -> None:
    found = f"{platform.python_implementation()} {platform.python_version()}"
    if found != f"CPython {LOCAL_PYTHON}":
        raise SetupError(f"{sys.executable} is {found}, expected CPython {LOCAL_PYTHON}")
    if sys.prefix != sys.base_prefix:
        raise SetupError(f"{sys.executable} is a virtual environment; use the base interpreter")


def check_target(target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise SetupError(f"{target} already exists; refusing to modify an existing environment")


def pinned_pip() -> tuple[str, str]:
    """The version and sha256 of the single pip wheel in the pip lock."""
    text = (REPO_ROOT / PIP_LOCK).read_text(encoding="utf-8")
    entries = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    pin = re.fullmatch(r"pip==(\d+(?:\.\d+)*) \\", entries[0]) if len(entries) == 2 else None
    digest = re.fullmatch(r" *--hash=sha256:([0-9a-f]{64})", entries[1]) if pin else None
    if not (pin and digest):
        raise SetupError(f"{PIP_LOCK} must pin exactly one pip wheel with one sha256")
    return pin.group(1), digest.group(1)


def download_verified_pip(version: str, digest: str, directory: Path) -> Path:
    url = PIP_WHEEL_URL.format(version=version)
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 (fixed https URL)
        if not response.geturl().startswith("https://"):
            raise SetupError(f"download of pip {version} left HTTPS")
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise SetupError(f"sha256 of the downloaded pip {version} wheel does not match {PIP_LOCK}")
    wheel = directory / f"pip-{version}-py3-none-any.whl"
    wheel.write_bytes(data)
    return wheel


def pip_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIP_", "NOTION_", "PYTHON"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def run(*command: str | Path) -> str:
    result = subprocess.run([str(part) for part in command], cwd=REPO_ROOT, env=pip_environment(),
                            stdout=subprocess.PIPE, text=True, check=False)
    print(result.stdout, end="")
    if result.returncode != 0:
        raise SetupError(f"command failed ({result.returncode}): {' '.join(map(str, command))}")
    return result.stdout


def create(target: Path) -> None:
    version, digest = pinned_pip()
    with tempfile.TemporaryDirectory() as tmp:
        wheel = download_verified_pip(version, digest, Path(tmp))
        # Created here, not only checked before the download: mkdir fails if the
        # path appeared in the meantime, so an existing environment is never
        # written into.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        venv.EnvBuilder(with_pip=False).create(target)
        python = target / "bin" / "python"
        run(python, "-I", wheel / "pip", "install", "--require-hashes", "--no-deps", "--no-index",
            "--only-binary=:all:", "--find-links", tmp, "-r", PIP_LOCK)
    installed = run(python, "-I", "-m", "pip", "--version")
    if not installed.startswith(f"pip {version} from {target.resolve()}/"):
        raise SetupError(f"unexpected pip after bootstrap: {installed.strip()}")
    print(f"ok: hash-verified {installed.strip()}")
    run(python, "-I", "-m", "pip", "install", "--require-hashes", "--no-deps",
        "--only-binary=:all:", "-r", LOCK)
    run(python, "-I", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--no-index",
        "-e", ".", "-e", "tools/generation_workspace")
    run(python, "-I", "-m", "pip", "check")
    run(python, "-I", "-c", "import sys; print('ok:', sys.version.split()[0], sys.executable)")


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    args = [arg for arg in argv if arg != "--dry-run"]
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} VENV_DIR [--dry-run]", file=sys.stderr)
        return 2
    target = Path(args[0]).absolute()
    try:
        check_base_interpreter()
        check_target(target)
        version, _ = pinned_pip()
        if dry_run:
            print(f"dry-run: would create {target} with {sys.executable} "
                  f"(CPython {LOCAL_PYTHON}) and pip {version}")
            return 0
        create(target)
    except (SetupError, OSError) as exc:
        print(f"LOCAL VENV SETUP FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
