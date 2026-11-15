"""Phase 3H-R7 D-1: the bootstrap trust checks of scripts/cutover_readiness.py, on synthetic
runtime and venv fixtures only (no Python of a fixture is ever executed)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cutover_readiness.py"


def load_readiness() -> Any:
    spec = importlib.util.spec_from_file_location("cutover_readiness_r7", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


cr = load_readiness()
CLEAN_ENV = {"HOME": "/Users/synthetic", "USER": "synthetic", "LANG": "C.UTF-8",
             "PATH": "/planted/bin:/usr/bin", "TERM": "xterm"}
DOTENV_MAIN = b'def _load_dotenv_disabled():\n    return "PYTHON_DOTENV_DISABLED" in env\n'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runtime = root / "runtime"
        self.venv = root / "venv"
        self.site = self.venv / "lib" / "python3.14" / "site-packages"
        self.runtime_site = self.runtime / "lib" / "python3.14" / "site-packages"
        self.files: dict[Path, bytes] = {
            self.runtime / "lib" / "libpython3.14.dylib": b"synthetic dylib",
            self.venv / "bin" / "python3.14": b"synthetic interpreter, never executed",
            self.venv / "pyvenv.cfg": (f"home = {self.runtime}/bin\n"
                                       "include-system-site-packages = false\n"
                                       "version = 3.14.10\n").encode(),
            self.site / "_editable_impl_mtg_notion_manager.pth": b"/synthetic/src\n",
            self.site / "distutils-precedence.pth": b"import os; var = 'SETUPTOOLS'\n",
            self.site / "dep" / "__init__.py": b"VALUE = 1\n",
            self.site / "dotenv" / "main.py": DOTENV_MAIN,
        }
        for path, data in self.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.site / "python_dotenv-1.2.1.dist-info").mkdir()
        self.runtime_site.mkdir(parents=True)
        self.python = str(self.venv / "bin" / "python3.14")
        self.manifest_path = root / "manifest.json"
        self.manifest = {
            "schema": cr.MANIFEST_SCHEMA,
            "python_version": "3.14.10",
            "runtime_root": str(self.runtime),
            "venv": str(self.venv),
            "interpreter": {"path": "bin/python3.14",
                            "sha256": _sha(self.files[self.venv / "bin" / "python3.14"])},
            "pyvenv_cfg": {"sha256": _sha(self.files[self.venv / "pyvenv.cfg"])},
            "libraries": {"lib/libpython3.14.dylib": _sha(b"synthetic dylib")},
            "files": {"lib/python3.14/site-packages/dep/__init__.py": _sha(b"VALUE = 1\n")},
            "pth": {"_editable_impl_mtg_notion_manager.pth": _sha(b"/synthetic/src\n"),
                    "distutils-precedence.pth": _sha(b"import os; var = 'SETUPTOOLS'\n")},
            "provenance": {"archive_sha256": cr.VERIFIED_ARCHIVE_SHA256,
                           "attestation_verified": True,
                           "evidence": "docs/evidence/phase-3h-r6/EVIDENCE.json"},
        }
        self.write_manifest()

    def write_manifest(self, **changes: Any) -> str:
        self.manifest.update(changes)
        data = json.dumps(self.manifest).encode()
        self.manifest_path.write_bytes(data)
        return _sha(data)

    @property
    def manifest_sha(self) -> str:
        return _sha(self.manifest_path.read_bytes())

    def verify(self, environ: dict[str, str] | None = None, python: str | None = None,
               expected: str | None = "auto") -> Any:
        return cr.verify_bootstrap(self.manifest_path,
                                   self.manifest_sha if expected == "auto" else expected,
                                   python or self.python, CLEAN_ENV if environ is None else environ)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _failed(result: Any) -> Any:
    assert result.status == cr.FAIL, result.as_dict()
    return result.checks[-1]


def test_untampered_fixture_passes(fx: Fixture) -> None:
    result = fx.verify()
    assert result.status == cr.PASS, result.as_dict()
    names = [c.name for c in result.checks]
    assert names == ["manifest_integrity", "manifest_schema", "provenance", "interpreter_path",
                     "interpreter_integrity", "runtime_version", "libraries",
                     "dependency_files", "startup_hooks", "pth_files", "environment", "toctou"]
    assert "1 executable lines" in result.checks[names.index("pth_files")].detail


def test_executable_hash_mismatch(fx: Fixture) -> None:
    (fx.venv / "bin" / "python3.14").write_bytes(b"replaced interpreter")
    check = _failed(fx.verify())
    assert (check.name, check.detail) == ("interpreter_integrity", "executable hash mismatch")


def test_interpreter_symlink_is_not_followed(fx: Fixture) -> None:
    target = fx.venv / "bin" / "python3.14"
    real = fx.root / "elsewhere"
    real.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(real)
    check = _failed(fx.verify())
    assert check.name == "interpreter_integrity" and "symlink" in check.detail


def test_manifest_mismatch(fx: Fixture) -> None:
    check = _failed(fx.verify(expected="0" * 64))
    assert check.name == "manifest_integrity"


def test_missing_expected_manifest(fx: Fixture) -> None:
    fx.manifest_path.unlink()
    check = _failed(fx.verify(expected="0" * 64))
    assert check.name == "manifest_present" and "missing expected manifest" in check.detail


def test_no_recorded_manifest_hash_is_unverified_not_pass(fx: Fixture) -> None:
    result = fx.verify(expected=None)
    assert result.status == cr.UNVERIFIED
    assert result.checks[0].name == "manifest_integrity"


@pytest.mark.parametrize("change", [
    {"extra": 1},
    {"schema": "other/1"},
    {"venv": "relative/venv"},
    {"interpreter": {"path": "../../elsewhere/python", "sha256": "0" * 64}},
    {"pth": {"../evil.pth": "0" * 64}},
    {"libraries": {"lib/x": "NOT-A-HASH"}},
])
def test_invalid_manifest(fx: Fixture, change: dict[str, Any]) -> None:
    fx.write_manifest(**change)
    check = _failed(fx.verify())
    assert check.name in ("manifest_schema", "interpreter_path")


def test_manifest_that_is_not_json(fx: Fixture) -> None:
    fx.manifest_path.write_bytes(b"\xff not json")
    assert _failed(fx.verify()).name == "manifest_schema"


@pytest.mark.parametrize("python", ["/usr/bin/python3", "relative/python3.14", "{venv}/bin/python"])
def test_unexpected_python_path(fx: Fixture, python: str) -> None:
    check = _failed(fx.verify(python=python.format(venv=fx.venv)))
    assert check.name == "interpreter_path" and "unexpected Python path" in check.detail


@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                                  "DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "NOTION_API_KEY",
                                  "NOTION_TOKEN", "PIP_INDEX_URL"])
def test_unexpected_environment_variable(fx: Fixture, name: str) -> None:
    check = _failed(fx.verify(environ={**CLEAN_ENV, name: "synthetic-planted-value"}))
    assert check.name == "environment" and name in check.detail
    assert "synthetic-planted-value" not in check.detail


@pytest.mark.parametrize("version", ["3.14.11", "3.13.9", "3.9.6", ""])
def test_unknown_runtime_version(fx: Fixture, version: str) -> None:
    cfg = (f"home = {fx.runtime}/bin\ninclude-system-site-packages = false\n"
           f"version = {version}\n").encode()
    (fx.venv / "pyvenv.cfg").write_bytes(cfg)
    fx.write_manifest(pyvenv_cfg={"sha256": _sha(cfg)})
    check = _failed(fx.verify())
    assert check.name == "runtime_version" and "unknown runtime version" in check.detail


def test_unknown_runtime_version_even_if_the_manifest_agrees(fx: Fixture) -> None:
    cfg = (f"home = {fx.runtime}/bin\ninclude-system-site-packages = false\n"
           "version = 3.14.11\n").encode()
    (fx.venv / "pyvenv.cfg").write_bytes(cfg)
    fx.write_manifest(pyvenv_cfg={"sha256": _sha(cfg)}, python_version="3.14.11")
    check = _failed(fx.verify())
    assert check.name == "runtime_version" and "unknown runtime version: 3.14.11" in check.detail


def test_manifest_version_must_match_the_venv(fx: Fixture) -> None:
    fx.write_manifest(python_version="3.14.9")
    assert _failed(fx.verify()).name == "runtime_version"


def test_system_site_packages_are_refused(fx: Fixture) -> None:
    cfg = (f"home = {fx.runtime}/bin\ninclude-system-site-packages = true\n"
           "version = 3.14.10\n").encode()
    (fx.venv / "pyvenv.cfg").write_bytes(cfg)
    fx.write_manifest(pyvenv_cfg={"sha256": _sha(cfg)})
    assert "system site-packages" in _failed(fx.verify()).detail


def test_pyvenv_cfg_hash_mismatch(fx: Fixture) -> None:
    with open(fx.venv / "pyvenv.cfg", "a", encoding="utf-8") as handle:
        handle.write("extra = 1\n")
    assert _failed(fx.verify()).detail == "pyvenv.cfg hash mismatch"


def test_malicious_pth_is_rejected_and_never_executed(fx: Fixture) -> None:
    marker = fx.root / "pth-was-executed"
    (fx.site / "zz-evil.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('owned')\n", encoding="utf-8")
    check = _failed(fx.verify())
    assert check.name == "pth_files" and "unknown .pth files: ['zz-evil.pth']" in check.detail
    assert not marker.exists()


def test_pinned_pth_content_change(fx: Fixture) -> None:
    (fx.site / "_editable_impl_mtg_notion_manager.pth").write_text(
        "/synthetic/src\nimport os\n", encoding="utf-8")
    check = _failed(fx.verify())
    assert "_editable_impl_mtg_notion_manager.pth: hash mismatch" in check.detail


def test_missing_pinned_pth(fx: Fixture) -> None:
    (fx.site / "distutils-precedence.pth").unlink()
    assert "distutils-precedence.pth: missing" in _failed(fx.verify()).detail


def test_pth_in_the_runtime_site_packages_is_checked_too(fx: Fixture) -> None:
    (fx.runtime_site / "planted.pth").write_text("import os\n", encoding="utf-8")
    assert "planted.pth" in _failed(fx.verify()).detail


@pytest.mark.parametrize("hook", ["sitecustomize.py", "usercustomize.py",
                                  "sitecustomize/__init__.py",
                                  "__pycache__/sitecustomize.cpython-314.pyc",
                                  "usercustomize.cpython-314-darwin.so"])
@pytest.mark.parametrize("where", ["venv", "runtime"])
def test_unexpected_startup_hook(fx: Fixture, hook: str, where: str) -> None:
    path = (fx.site if where == "venv" else fx.runtime_site) / hook
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"import os\n")
    check = _failed(fx.verify())
    assert check.name == "startup_hooks" and "unexpected startup hook" in check.detail


def test_modified_dependency(fx: Fixture) -> None:
    (fx.site / "dep" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    check = _failed(fx.verify())
    assert check.name == "dependency_files" and "hash mismatch" in check.detail


def test_modified_library(fx: Fixture) -> None:
    (fx.runtime / "lib" / "libpython3.14.dylib").write_bytes(b"planted dylib")
    assert _failed(fx.verify()).name == "libraries"


def test_missing_library(fx: Fixture) -> None:
    (fx.runtime / "lib" / "libpython3.14.dylib").unlink()
    assert "lib/libpython3.14.dylib" in _failed(fx.verify()).detail


def test_untrusted_provenance_without_archive(fx: Fixture) -> None:
    fx.write_manifest(provenance={})
    result = fx.verify()
    assert result.status == cr.UNVERIFIED
    assert [c.status for c in result.checks if c.name == "provenance"] == [cr.UNVERIFIED]


def test_untrusted_provenance_without_attestation(fx: Fixture) -> None:
    fx.write_manifest(provenance={"archive_sha256": cr.VERIFIED_ARCHIVE_SHA256,
                                  "attestation_verified": False, "evidence": "x"})
    assert fx.verify().status == cr.UNVERIFIED


def test_provenance_of_another_archive_fails(fx: Fixture) -> None:
    fx.write_manifest(provenance={"archive_sha256": "1" * 64, "attestation_verified": True,
                                  "evidence": "x"})
    result = fx.verify()
    assert result.status == cr.FAIL
    assert [c.status for c in result.checks if c.name == "provenance"] == [cr.FAIL]


def test_matching_hashes_alone_do_not_pass(fx: Fixture) -> None:
    """Integrity without independent trust roots (manifest hash, provenance) is not PASS."""
    fx.write_manifest(provenance={})
    result = fx.verify(expected=None)
    assert result.status == cr.UNVERIFIED
    assert all(c.status in (cr.PASS, cr.UNVERIFIED) for c in result.checks)


def test_toctou_change_after_verification(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """The interpreter is swapped after it was hashed (while pyvenv.cfg is read)."""
    real = cr.read_pinned

    def read_then_swap(path: Path, *args: Any, **kwargs: Any) -> Any:
        result = real(path, *args, **kwargs)
        if Path(path).name == "pyvenv.cfg":
            interpreter = fx.venv / "bin" / "python3.14"
            data = interpreter.read_bytes()
            interpreter.unlink()
            interpreter.write_bytes(data)  # same content, new inode
        return result

    monkeypatch.setattr(cr, "read_pinned", read_then_swap)
    check = _failed(fx.verify())
    assert check.name == "toctou" and "python3.14" in check.detail


def test_toctou_change_during_read(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        calls["n"] += 1
        info = real_fstat(fd)
        if calls["n"] == 2:  # the "after" fstat of the manifest read
            return os.stat_result((info.st_mode, info.st_ino + 1, *tuple(info)[2:]))
        return info

    monkeypatch.setattr(cr.os, "fstat", fstat)
    check = _failed(fx.verify())
    assert check.name == "manifest_present" and "changed while it was read" in check.detail


def test_no_fixture_interpreter_is_run(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "execv", refuse)
    assert fx.verify().status == cr.PASS


def test_executable_pth_lines() -> None:
    assert cr.executable_pth_lines(b"/path\nimport os\nimport\tsys\n importx\nimportant\n") == 2
