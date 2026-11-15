"""Phase 3H-R7 D-1: the bootstrap trust checks of scripts/cutover_readiness.py, on synthetic
runtime, venv and application trees only (no Python of a fixture is ever executed)."""

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
EVIDENCE = "docs/evidence/phase-3h-r6/EVIDENCE.json"


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
DOTENV_MAIN = b'''import os


def _load_dotenv_disabled():
    if "PYTHON_DOTENV_DISABLED" not in os.environ:
        return False
    return os.environ["PYTHON_DOTENV_DISABLED"].casefold() in {"1", "true"}


def load_dotenv(dotenv_path=None):
    if _load_dotenv_disabled():
        return False
    return True
'''


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.runtime = root / "runtime"
        self.venv = root / "venv"
        self.app = root / "app"
        self.stdlib = self.runtime / "lib" / "python3.14"
        self.site = self.venv / "lib" / "python3.14" / "site-packages"
        self.runtime_site = self.stdlib / "site-packages"
        files = {
            self.runtime / "bin" / "python3.14": b"synthetic base interpreter",
            self.runtime / "lib" / "libpython3.14.dylib": b"synthetic dylib",
            self.stdlib / "os.py": b"# synthetic stdlib\n",
            self.stdlib / "lib-dynload" / "_x.cpython-314-darwin.so": b"synthetic extension",
            self.venv / "bin" / "python3.14": b"synthetic interpreter, never executed",
            self.venv / "pyvenv.cfg": (f"home = {self.runtime}/bin\n"
                                       "include-system-site-packages = false\n"
                                       "version = 3.14.10\n").encode(),
            self.site / "_editable_impl_mtg_notion_manager.pth": str(self.app).encode(),
            self.site / "distutils-precedence.pth": b"import os; var = 'SETUPTOOLS'\n",
            self.site / "dep" / "__init__.py": b"VALUE = 1\n",
            self.site / "dotenv" / "main.py": DOTENV_MAIN,
            self.site / "python_dotenv-1.2.1.dist-info" / "METADATA": b"Name: python-dotenv\n",
            self.app / "mtg_notion_manager" / "__init__.py": b"",
        }
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.venv / "bin" / "python").symlink_to("python3.14")
        self.runtime_site.mkdir()
        self.python = str(self.venv / "bin" / "python3.14")
        self.manifest_path = root / "manifest.json"
        self.provenance: dict[str, Any] = {
            "archive_sha256": cr.VERIFIED_ARCHIVE_SHA256, "attestation_verified": True,
            "evidence": EVIDENCE, "evidence_sha256": _sha((REPO_ROOT / EVIDENCE).read_bytes())}
        self.rebuild()

    def rebuild(self, **changes: Any) -> None:
        """A manifest of the trees as they are now (what a human would record)."""
        trees = {name: cr.walk_tree(root).entries
                 for name, root in (("runtime", self.runtime), ("venv", self.venv),
                                    ("app", self.app))}
        self.manifest: dict[str, Any] = {
            "schema": cr.MANIFEST_SCHEMA, "python_version": "3.14.10",
            "runtime_root": str(self.runtime), "venv": str(self.venv), "app_root": str(self.app),
            "interpreter": {"path": "bin/python3.14", "sha256": trees["venv"]["bin/python3.14"]},
            "trees": trees, "provenance": dict(self.provenance)}
        self.write_manifest(**changes)

    def write_manifest(self, **changes: Any) -> None:
        self.manifest.update(changes)
        self.manifest_path.write_bytes(json.dumps(self.manifest).encode())

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
                     "interpreter_integrity", "runtime_version", "tree_inventory", "pth_files",
                     "startup_hooks", "environment", "toctou"]
    assert "1 executable lines" in result.checks[names.index("pth_files")].detail
    assert "human-asserted" in result.checks[names.index("provenance")].detail
    # stdlib, lib-dynload, 2 site-packages and the app root added by the editable .pth
    assert "none in 5 sys.path directories" in result.checks[names.index("startup_hooks")].detail


def test_executable_hash_mismatch(fx: Fixture) -> None:
    (fx.venv / "bin" / "python3.14").write_bytes(b"replaced interpreter")
    check = _failed(fx.verify())
    assert (check.name, check.detail) == ("interpreter_integrity", "executable hash mismatch")


def test_interpreter_hash_must_agree_with_the_tree(fx: Fixture) -> None:
    fx.manifest["trees"]["venv"]["bin/python3.14"] = "0" * 64
    fx.write_manifest()
    assert _failed(fx.verify()).name == "interpreter_integrity"


def test_interpreter_symlink_is_not_followed(fx: Fixture) -> None:
    target = fx.venv / "bin" / "python3.14"
    real = fx.root / "elsewhere"
    real.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(real)
    check = _failed(fx.verify())
    assert check.name == "interpreter_integrity" and "symlink" in check.detail


def test_manifest_mismatch(fx: Fixture) -> None:
    assert _failed(fx.verify(expected="0" * 64)).name == "manifest_integrity"


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
    {"schema": "mtg-notion-manager/runtime-manifest/1"},
    {"venv": "relative/venv"},
    {"app_root": None},
    {"python_version": 3.14},
    {"interpreter": {"path": "../../elsewhere/python", "sha256": "0" * 64}},
    {"interpreter": {"path": 7, "sha256": "0" * 64}},
    {"interpreter": {"path": "bin/python3.14", "sha256": "NOT-A-HASH"}},
    {"trees": {"runtime": {}, "venv": {}}},
    {"trees": {"runtime": {"../x": "0" * 64}, "venv": {}, "app": {}}},
    {"trees": {"runtime": {"x": "NOT-A-HASH"}, "venv": {}, "app": {}}},
    {"provenance": []},
])
def test_invalid_manifest(fx: Fixture, change: dict[str, Any]) -> None:
    fx.write_manifest(**change)
    assert _failed(fx.verify()).name == "manifest_schema"


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


def _pyvenv(fx: Fixture, version: str, system: str = "false") -> None:
    (fx.venv / "pyvenv.cfg").write_text(
        f"home = {fx.runtime}/bin\ninclude-system-site-packages = {system}\n"
        f"version = {version}\n", encoding="utf-8")


@pytest.mark.parametrize("version", ["3.14.11", "3.13.9", "3.9.6", ""])
def test_unknown_runtime_version(fx: Fixture, version: str) -> None:
    _pyvenv(fx, version)
    fx.rebuild()
    check = _failed(fx.verify())
    assert check.name == "runtime_version" and "unknown runtime version" in check.detail


def test_unknown_runtime_version_even_if_the_manifest_agrees(fx: Fixture) -> None:
    _pyvenv(fx, "3.14.11")
    fx.rebuild(python_version="3.14.11")
    check = _failed(fx.verify())
    assert check.name == "runtime_version" and "unknown runtime version: 3.14.11" in check.detail


def test_manifest_version_must_match_the_venv(fx: Fixture) -> None:
    fx.write_manifest(python_version="3.14.9")
    assert _failed(fx.verify()).name == "runtime_version"


def test_system_site_packages_are_refused(fx: Fixture) -> None:
    _pyvenv(fx, "3.14.10", system="true")
    fx.rebuild()
    assert "system site-packages" in _failed(fx.verify()).detail


def test_pyvenv_cfg_change_is_caught_by_the_inventory(fx: Fixture) -> None:
    with open(fx.venv / "pyvenv.cfg", "a", encoding="utf-8") as handle:
        handle.write("extra = 1\n")
    check = _failed(fx.verify())
    assert check.name == "tree_inventory" and "venv: hash or link mismatch: ['pyvenv.cfg']" \
        in check.detail


# --- complete inventories: nothing unlisted, nothing changed -----------------------------


@pytest.mark.parametrize("where", ["runtime", "venv", "app"])
def test_unlisted_file_in_any_tree(fx: Fixture, where: str) -> None:
    root = {"runtime": fx.stdlib, "venv": fx.site, "app": fx.app / "mtg_notion_manager"}[where]
    (root / "planted.py").write_text("import os\n", encoding="utf-8")
    check = _failed(fx.verify())
    assert check.name == "tree_inventory" and f"{where}: entries not in the manifest" \
        in check.detail and "planted.py" in check.detail


def test_missing_entry(fx: Fixture) -> None:
    (fx.runtime / "lib" / "libpython3.14.dylib").unlink()
    check = _failed(fx.verify())
    assert "runtime: missing entries: ['lib/libpython3.14.dylib']" in check.detail


@pytest.mark.parametrize("relative", ["lib/python3.14/site-packages/dep/__init__.py",
                                      "lib/python3.14/site-packages/dotenv/main.py"])
def test_modified_dependency(fx: Fixture, relative: str) -> None:
    (fx.venv / relative).write_text("VALUE = 2\n", encoding="utf-8")
    check = _failed(fx.verify())
    assert check.name == "tree_inventory" and relative in check.detail


def test_modified_library(fx: Fixture) -> None:
    (fx.runtime / "lib" / "libpython3.14.dylib").write_bytes(b"planted dylib")
    assert "lib/libpython3.14.dylib" in _failed(fx.verify()).detail


def test_modified_application_source(fx: Fixture) -> None:
    (fx.app / "mtg_notion_manager" / "__init__.py").write_text("import os\n", encoding="utf-8")
    assert "app: hash or link mismatch" in _failed(fx.verify()).detail


def test_symlink_target_change(fx: Fixture) -> None:
    link = fx.venv / "bin" / "python"
    link.unlink()
    link.symlink_to("/usr/bin/python3")
    assert "venv: hash or link mismatch: ['bin/python']" in _failed(fx.verify()).detail


def test_special_file_in_a_tree(fx: Fixture) -> None:
    os.mkfifo(fx.site / "planted.fifo")
    check = _failed(fx.verify())
    assert check.name == "tree_inventory" and "unexpected file type" in check.detail


def test_unreadable_directory_in_a_tree(fx: Fixture) -> None:
    hidden = fx.site / "hidden"
    hidden.mkdir()
    (hidden / "sitecustomize.py").write_text("import os\n", encoding="utf-8")
    fx.rebuild()
    hidden.chmod(0)
    try:
        if os.access(hidden, os.R_OK):
            pytest.skip("running with privileges that ignore directory modes")
        check = _failed(fx.verify())
        assert check.name == "tree_inventory" and "unreadable directory" in check.detail
    finally:
        hidden.chmod(0o755)


def test_missing_tree_root(fx: Fixture) -> None:
    fx.write_manifest(app_root=str(fx.root / "absent"))
    assert "app: tree root missing" in _failed(fx.verify()).detail


# --- startup hooks on every sys.path directory (pinned by the manifest or not) -----------


@pytest.mark.parametrize("hook", ["sitecustomize.py", "usercustomize.py",
                                  "sitecustomize/__init__.py",
                                  "__pycache__/sitecustomize.cpython-314.pyc",
                                  "usercustomize.cpython-314-darwin.so"])
@pytest.mark.parametrize("where", ["stdlib", "lib-dynload", "venv-site", "runtime-site", "app"])
def test_unexpected_startup_hook_even_when_pinned(fx: Fixture, hook: str, where: str) -> None:
    directory = {"stdlib": fx.stdlib, "lib-dynload": fx.stdlib / "lib-dynload",
                 "venv-site": fx.site, "runtime-site": fx.runtime_site, "app": fx.app}[where]
    path = directory / hook
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"import os\n")
    fx.rebuild()  # the manifest records the hook: it must still fail
    check = _failed(fx.verify())
    assert check.name == "startup_hooks" and "unexpected startup hook" in check.detail


def test_zip_on_sys_path_is_refused(fx: Fixture) -> None:
    (fx.runtime / "lib" / "python314.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    fx.rebuild()
    check = _failed(fx.verify())
    assert check.name == "startup_hooks" and "zip on sys.path" in check.detail


def test_malicious_pth_is_rejected_and_never_executed(fx: Fixture) -> None:
    marker = fx.root / "pth-was-executed"
    (fx.site / "zz-evil.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('owned')\n", encoding="utf-8")
    check = _failed(fx.verify())
    assert check.name == "tree_inventory" and "zz-evil.pth" in check.detail
    assert not marker.exists()


@pytest.mark.parametrize("line", ["/usr/lib/planted", "../../../../outside", "/"])
def test_pth_path_outside_the_verified_trees(fx: Fixture, line: str) -> None:
    (fx.site / "zz-path.pth").write_text(line + "\n", encoding="utf-8")
    fx.rebuild()
    check = _failed(fx.verify())
    assert check.name == "pth_files" and "outside the verified trees" in check.detail


def test_pth_path_that_does_not_exist(fx: Fixture) -> None:
    (fx.site / "zz-path.pth").write_text(str(fx.app / "later") + "\n", encoding="utf-8")
    fx.rebuild()
    assert "not an existing directory" in _failed(fx.verify()).detail


def test_pinned_import_pth_is_counted_not_hidden(fx: Fixture) -> None:
    (fx.site / "zz-import.pth").write_text("import os\nimport\tsys\n", encoding="utf-8")
    fx.rebuild()
    result = fx.verify()
    assert result.status == cr.PASS
    assert "3 executable lines" in [c for c in result.checks if c.name == "pth_files"][0].detail


# --- provenance -------------------------------------------------------------------------


def _with_provenance(fx: Fixture, **changes: Any) -> Any:
    fx.provenance.update(changes)
    fx.rebuild()
    result = fx.verify()
    return result, [c for c in result.checks if c.name == "provenance"][0]


def test_untrusted_provenance_without_archive(fx: Fixture) -> None:
    result, check = _with_provenance(fx, archive_sha256=None)
    assert result.status == cr.UNVERIFIED and check.status == cr.UNVERIFIED


@pytest.mark.parametrize("change", [{"attestation_verified": False}, {"evidence": None},
                                    {"evidence": "../outside.json"}, {"evidence_sha256": None}])
def test_untrusted_provenance_without_attestation_evidence(fx: Fixture,
                                                           change: dict[str, Any]) -> None:
    result, check = _with_provenance(fx, **change)
    assert result.status == cr.UNVERIFIED and check.status == cr.UNVERIFIED


def test_provenance_of_another_archive_fails(fx: Fixture) -> None:
    result, check = _with_provenance(fx, archive_sha256="1" * 64)
    assert result.status == cr.FAIL and check.status == cr.FAIL


def test_provenance_evidence_hash_mismatch(fx: Fixture) -> None:
    _, check = _with_provenance(fx, evidence_sha256="0" * 64)
    assert check.status == cr.FAIL and "sha256 differs" in check.detail


def test_provenance_evidence_missing(fx: Fixture) -> None:
    _, check = _with_provenance(fx, evidence="docs/evidence/absent.json")
    assert check.status == cr.FAIL and "not readable" in check.detail


@pytest.mark.parametrize("record", [
    {"archive": {"sha256": "1" * 64}, "formal_gate_run": {"gates": {"1": "PASS"}}},
    {"archive": {"sha256": "@"}, "formal_gate_run": {"gates": {"1": "PASS", "4": "FAIL"}}},
    {"archive": {"sha256": "@"}, "formal_gate_run": {"gates": {}}},
    {"archive": "x"},
])
def test_provenance_evidence_must_record_the_archive_with_all_gates_pass(
        fx: Fixture, tmp_path: Path, record: dict[str, Any]) -> None:
    root = tmp_path / "evidence-root"
    (root / "e").mkdir(parents=True)
    data = json.dumps(record).replace("@", cr.VERIFIED_ARCHIVE_SHA256).encode()
    (root / "e" / "EVIDENCE.json").write_bytes(data)
    fx.provenance.update(evidence="e/EVIDENCE.json", evidence_sha256=_sha(data))
    fx.rebuild()
    result = cr.verify_bootstrap(fx.manifest_path, fx.manifest_sha, fx.python, CLEAN_ENV,
                                 evidence_root=root)
    assert [c.status for c in result.checks if c.name == "provenance"] == [cr.FAIL]


def test_matching_hashes_alone_do_not_pass(fx: Fixture) -> None:
    """Integrity without independent trust roots (manifest hash, provenance) is not PASS."""
    fx.provenance.update(archive_sha256=None)
    fx.rebuild()
    result = fx.verify(expected=None)
    assert result.status == cr.UNVERIFIED
    assert all(c.status in (cr.PASS, cr.UNVERIFIED) for c in result.checks)


# --- TOCTOU -------------------------------------------------------------------------------


def test_file_added_after_the_scan(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook created after the startup-hook scan, before the end of the run."""
    real = cr.startup_hooks

    def scan_then_plant(directories: list[Path]) -> list[str]:
        found = real(directories)
        (fx.app / "sitecustomize.py").write_text("import os\n", encoding="utf-8")
        return found

    monkeypatch.setattr(cr, "startup_hooks", scan_then_plant)
    check = _failed(fx.verify())
    assert check.name == "toctou" and "app" in check.detail


def test_interpreter_replaced_after_verification(fx: Fixture,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    real = cr.injected_variables

    def check_then_swap(environ: dict[str, str]) -> list[str]:
        interpreter = fx.venv / "bin" / "python3.14"
        data = interpreter.read_bytes()
        interpreter.unlink()
        interpreter.write_bytes(data)  # same content, new inode
        return real(environ)

    monkeypatch.setattr(cr, "injected_variables", check_then_swap)
    check = _failed(fx.verify())
    assert check.name == "toctou" and "bin/python3.14" in check.detail


def test_manifest_replaced_during_the_run(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    real = cr.injected_variables

    def check_then_replace(environ: dict[str, str]) -> list[str]:
        data = fx.manifest_path.read_bytes()
        fx.manifest_path.unlink()
        fx.manifest_path.write_bytes(data)
        return real(environ)

    monkeypatch.setattr(cr, "injected_variables", check_then_replace)
    check = _failed(fx.verify())
    assert check.name == "toctou" and "the manifest" in check.detail


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


def test_executable_pth_lines() -> None:
    assert cr.executable_pth_lines(b"/path\nimport os\nimport\tsys\n importx\nimportant\n") == 2


def test_pth_paths() -> None:
    site = Path("/s/site")
    assert cr.pth_paths(site, b"# c\n\nimport os\n/abs\nrel\n../up\n") == [
        Path("/abs"), Path("/s/site/rel"), Path("/s/up")]
