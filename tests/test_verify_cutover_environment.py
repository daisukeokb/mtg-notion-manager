"""Phase 3H-R6 Decision 06: the environment of the gates' Python processes (R6-F05) and the
persisted evidence (R6-F06), on synthetic values only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from tests.test_verify_cutover_gate import (
    _build_archive,
    _context,
    _FakeRun,
    _install,
    gate,
)

INJECTED = {
    "DYLD_INSERT_LIBRARIES": "/planted/libevil.dylib",
    "DYLD_LIBRARY_PATH": "/planted/lib",
    "PYTHONPATH": "/planted/site",
    "PYTHONHOME": "/planted/home",
    "NOTION_API_KEY": "synthetic-not-a-secret-7f3a",
    "PIP_INDEX_URL": "https://planted.invalid/simple",
}
TRUSTED = ("/usr/bin", "/bin")


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path]:
    archive = _build_archive(tmp_path)
    return archive, _install(tmp_path, archive)


# --- the allowlist ------------------------------------------------------------------------


def test_injected_variables_are_not_passed() -> None:
    environ = {"HOME": "/h", "LANG": "C.UTF-8", "PATH": "/planted/bin:/usr/bin", **INJECTED}
    environment = gate.python_environment(environ, TRUSTED)
    assert environment == {"HOME": "/h", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}


def test_an_invalid_inherited_path_is_replaced() -> None:
    environment = gate.python_environment({"HOME": "/h", "PATH": "relative:/tmp/x::"}, TRUSTED)
    assert environment["PATH"] == os.pathsep.join(TRUSTED)


@pytest.mark.parametrize("environ", [{}, {"HOME": ""}, {"USER": "u"}])
def test_a_missing_required_variable_blocks(environ: dict[str, str]) -> None:
    with pytest.raises(gate.GateError, match="HOME"):
        gate.python_environment(environ, TRUSTED)


@pytest.mark.parametrize("dirs", [(), ("bin",), ("/usr/bin:/planted",)])
def test_unusable_trusted_directories_block(dirs: tuple[str, ...]) -> None:
    with pytest.raises(gate.GateError, match="trusted directories"):
        gate.python_environment({"HOME": "/h"}, dirs)


def _child_report(tmp_path: Path, environment: dict[str, str]) -> dict[str, Any]:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "r6_planted.py").write_text("PLANTED = True\n", encoding="utf-8")
    code = ("import importlib.util, json, os, sys; print(json.dumps({'names': sorted(os.environ), "
            "'planted': importlib.util.find_spec('r6_planted') is not None}))")
    # Without -I on purpose: what is not in the environment cannot reach the child at all.
    done = subprocess.run([sys.executable, "-B", "-c", code], env=environment,
                          capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


def test_a_real_child_gets_none_of_the_injected_values(tmp_path: Path) -> None:
    injected = {**INJECTED, "PYTHONPATH": str(tmp_path / "site"), "HOME": str(tmp_path),
                "PATH": "/planted/bin:/usr/bin:/bin"}
    report = _child_report(tmp_path, gate.python_environment(injected, TRUSTED))
    assert report["planted"] is False
    # macOS may add __CF_USER_TEXT_ENCODING to a new process; nothing else is added.
    names = {n for n in report["names"] if not n.startswith("__CF_")}
    assert names <= {*gate.PYTHON_ENVIRONMENT_KEEP, "PATH"}
    assert not names & set(INJECTED)


def test_control_the_same_injection_reaches_an_inheriting_child(tmp_path: Path) -> None:
    injected = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(tmp_path / "site"), "NOTION_API_KEY": "synthetic"}
    report = _child_report(tmp_path, injected)
    assert report["planted"] is True and "NOTION_API_KEY" in report["names"]


# --- every Python of items 5-8 and of the rollback check uses it --------------------------


class _RecordingRun(_FakeRun):
    def __init__(self, **answers: tuple[int, str]) -> None:
        super().__init__(**answers)
        self.environments: list[dict[str, str] | None] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.environments.append(kwargs.get("env"))
        return super().__call__(command, **kwargs)


def _prepared(tmp_path: Path, installed_pair: tuple[Path, Path], run: Any, upto: str) -> Any:
    from tests.test_verify_cutover_gate import _after_gate4

    ctx = _after_gate4(tmp_path, installed_pair, run, upto)
    ctx.python_environment = gate.python_environment(
        {"HOME": str(tmp_path), **INJECTED}, TRUSTED)
    return ctx


def test_items_5_to_8_start_python_only_with_the_allowlisted_environment(
        tmp_path: Path, installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import platform

    for name, value in INJECTED.items():
        monkeypatch.setenv(name, value)
    runtime_root = installed[1] / "python"
    venv = tmp_path / "venv"
    run = _RecordingRun(**{
        "python3.14": (0, f"CPython {platform.python_version()} arm64\n"),
        "venv/bin/python -I -B -c": (0, f"{venv}/bin/python\n{venv}\n{runtime_root}\n"),
        "check_local_runtime": (0, "ok\n"),
        "--version": (0, gate.LEGACY_VERSION + "\n"),
        "pip freeze": (0, ""),
    })
    ctx = _prepared(tmp_path, installed, run, "5")
    assert gate.gate5_venv_resolution(ctx).status == gate.PASS
    ctx.passed.append("5")
    gate.gate6_7_lock_and_pip_check(ctx)
    ctx.passed.append("6-7")
    legacy = tmp_path / "legacy" / "bin"
    legacy.mkdir(parents=True)
    (legacy / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(gate, "legacy_fingerprint", lambda c, v: gate.LEGACY_FINGERPRINT)
    gate.gate8_legacy(ctx)
    assert len(run.environments) == len(run.commands) == 5
    for environment in run.environments:
        assert environment == ctx.python_environment
        assert not set(environment) & set(INJECTED)


def test_the_rollback_check_uses_the_given_environment(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "bin").mkdir(parents=True)
    python = legacy / "bin" / "python"
    python.write_text("", encoding="utf-8")
    (legacy / "bin" / "mtg-notion-manager").write_text(f"#!{python}\n", encoding="utf-8")
    run = _RecordingRun(**{"--version": (0, gate.LEGACY_VERSION + "\n"),
                           "--legacy-cli-worker": (0, "")})
    settings = gate.Settings(archive=tmp_path / "a", repo=tmp_path, legacy_venv=legacy)
    environment = gate.python_environment({"HOME": str(tmp_path), **INJECTED}, TRUSTED)
    assert gate.rollback_readiness(settings, run, environment)["status"] == gate.PASS
    assert run.environments == [environment, environment]
    assert gate.rollback_readiness(settings, run, None)["status"] == gate.BLOCKED


def test_run_python_refuses_without_a_prepared_environment(tmp_path: Path) -> None:
    ctx = _context(tmp_path, tmp_path / "missing", tmp_path)
    ctx.python_environment = None
    with pytest.raises(gate.GateError, match="Step 0"):
        ctx.run_python(["python", "--version"])


def test_step0_records_names_only_and_blocks_without_home(
        tmp_path: Path, installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in INJECTED.items():
        monkeypatch.setenv(name, value)
    run = _FakeRun(**{"git status": (0, "")})
    ctx = _context(tmp_path, *installed, passed=[], run=run)
    result = gate.step0_clean_tree(ctx)
    assert result.status == gate.PASS
    assert set(INJECTED) <= set(result.evidence["python_environment_dropped"])
    assert not set(INJECTED) & set(result.evidence["python_environment"])
    assert not any(value in json.dumps(result.evidence) for value in INJECTED.values())
    monkeypatch.delenv("HOME")
    ctx = _context(tmp_path, *installed, passed=[], run=_FakeRun(**{"git status": (0, "")}))
    results = gate.run_gates(ctx)
    assert results[0].status == gate.BLOCKED and "HOME" in results[0].evidence["error"]


# --- R6-F06: persisted evidence -----------------------------------------------------------


def _report(home: Path, repo: Path) -> dict[str, Any]:
    decomposed = unicodedata.normalize("NFD", str(repo))
    return {"metadata": {"archive": {"path": f"{home}/work/archive.tar.gz"}},
            "gates": [{"evidence": {"venv": f"{repo}/.venv-py314-r5/bin/python",
                                    "config": f"{home}/.config/gh/hosts.yml",
                                    "nfd": f"{decomposed}/.venv/bin/python",
                                    "names": ["HOME", "PATH"]}}]}


def test_exported_evidence_has_no_user_paths(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    repo = home / "マイドライブ" / "repo"
    source, out = tmp_path / "report.json", tmp_path / "evidence.json"
    source.write_text(json.dumps(_report(home, repo), ensure_ascii=False), encoding="utf-8")
    assert gate.export_evidence(str(source), str(out), repo=repo, home=home) == 0
    text = out.read_text(encoding="utf-8")
    assert "<repo>/.venv-py314-r5/bin/python" in text and "~/.config/gh/hosts.yml" in text
    assert "<archive-dir>/archive.tar.gz" in text  # the more specific replacement wins
    assert "<repo>/.venv/bin/python" in text  # the decomposed spelling too
    assert str(home) not in text and "someone" not in text
    with pytest.raises(FileExistsError):  # never overwrites
        gate.export_evidence(str(source), str(out), repo=repo, home=home)


def test_the_archive_directory_is_anonymized(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    download = tmp_path / "tmp" / "-Users-someone-scratch" / "archive"
    report = {"metadata": {"archive": {"path": f"{download}/a.tar.gz"}},
              "gates": [{"evidence": {"artifact": {"path": f"{download}/a.tar.gz"}}}]}
    source, out = tmp_path / "report.json", tmp_path / "evidence.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    assert gate.export_evidence(str(source), str(out), repo=home / "r", home=home) == 0
    text = out.read_text(encoding="utf-8")
    assert text.count("<archive-dir>/a.tar.gz") == 2 and "someone" not in text


@pytest.mark.parametrize("address", ["a.b@gmail.com", "x@example.org", "first-last@sub.ex.jp"])
def test_the_email_pattern_matches_addresses(address: str) -> None:
    assert gate._EMAIL.search(f"/path/GoogleDrive-{address}/folder")


@pytest.mark.parametrize("report", [
    {"note": "contact unrelated.person@example.com"},  # shares nothing with the home name
    # A path under home but outside the repository, through a folder named by the account
    # (the shape of the auditor's probe): "~/..." keeps the address, so it must be refused.
    {"path": "HOME/Library/CloudStorage/GoogleDrive-other.person@gmail.com/x"},
])
def test_export_refuses_when_an_email_address_remains(tmp_path: Path,
                                                     report: dict[str, str]) -> None:
    home = tmp_path / "home" / "someone"
    source = tmp_path / "report.json"
    text = json.dumps(report).replace("HOME", str(home))
    source.write_text(text, encoding="utf-8")
    assert gate.export_evidence(str(source), str(tmp_path / "out.json"), repo=home / "r",
                                home=home) == 2
    assert not (tmp_path / "out.json").exists()


def test_export_refuses_when_the_home_name_remains(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    source = tmp_path / "report.json"
    source.write_text(json.dumps({"note": "/elsewhere/someone/file"}), encoding="utf-8")
    assert gate.export_evidence(str(source), str(tmp_path / "out.json"), repo=home / "r",
                                home=home) == 2


def test_a_shallow_archive_directory_is_not_used_as_a_replacement(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    report = {"metadata": {"archive": {"path": "/tmp/a.tar.gz"}},
              "gates": [{"evidence": {"other": "/tmp/unrelated/file"}}]}
    source, out = tmp_path / "report.json", tmp_path / "evidence.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    assert gate.export_evidence(str(source), str(out), repo=home / "r", home=home) == 0
    text = out.read_text(encoding="utf-8")
    assert "<archive-dir>" not in text and "/tmp/unrelated/file" in text


def test_a_relative_home_blocks() -> None:
    with pytest.raises(gate.GateError, match="absolute"):
        gate.python_environment({"HOME": "relative/home"}, TRUSTED)
