"""Phase 3H-R6 Decision 05: archive symlink chains (F02), the verified archive (F03),
trusted tools (F04) and the minor findings, on synthetic fixtures only."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from tests.test_verify_cutover_gate import (
    _build_archive,
    _context,
    _FakeRun,
    _install,
    _pyc,
    gate,
)


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path]:
    archive = _build_archive(tmp_path)
    return archive, _install(tmp_path, archive)

# --- F02: symlinks resolved inside the archive -------------------------------------------


def _tar(*members: tuple[str, str, str]) -> bytes:
    """(name, kind, link target): kind is 'file', 'dir' or 'link'."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == "link":
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, target, 0o775
                tar.addfile(info)
            else:
                info.size, info.mode = 1, 0o644
                tar.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


BASE = (("python", "dir", ""), ("python/bin", "dir", ""), ("python/lib", "dir", ""),
        ("python/lib/os.py", "file", ""), ("python/bin/python3.14", "file", ""))


def _problems(*members: tuple[str, str, str]) -> list[str]:
    with pytest.raises(gate.ArchiveRejected) as raised:
        gate.validate_archive(_tar(*BASE, *members))
    return raised.value.problems


@pytest.mark.parametrize("members, problem", [
    # A -> B -> outside
    ((("python/bin/a", "link", "b"), ("python/bin/b", "link", "../../outside")),
     "symlink escapes the archive"),
    # A -> B -> C -> outside
    ((("python/bin/a", "link", "b"), ("python/bin/b", "link", "c"),
      ("python/bin/c", "link", "../../../etc/passwd")), "symlink escapes the archive"),
    # A -> B -> A
    ((("python/bin/a", "link", "b"), ("python/bin/b", "link", "a")), "symlink cycle"),
    # relative traversal
    ((("python/bin/x", "link", "../../../tmp"),), "symlink escapes the archive"),
    # the chain of the security audit: s -> .., t -> s/../../..
    ((("python/a", "dir", ""), ("python/a/b", "dir", ""), ("python/a/b/s", "link", ".."),
      ("python/a/b/t", "link", "s/../../..")), "symlink escapes the archive"),
    # absolute targets, directly and in a chain
    ((("python/bin/x", "link", "/etc/passwd"),), "absolute symlink target"),
    ((("python/bin/a", "link", "b"), ("python/bin/b", "link", "/usr/bin")),
     "absolute symlink target in the chain"),
    # a target that is not in the archive, and a path through a file
    ((("python/bin/x", "link", "missing"),), "symlink to a name that is not in the archive"),
    ((("python/bin/x", "link", "../lib/os.py/deeper"),), "symlink path through a file"),
])
def test_unsafe_symlinks_are_rejected(members: tuple, problem: str) -> None:
    assert any(p.startswith(problem) for p in _problems(*members)), _problems(*members)


@pytest.mark.parametrize("members", [
    (("python/x", "file", ""), ("python/x/y", "file", "")),  # file / directory conflict
    (("python/x/y", "file", ""), ("python/x", "file", "")),  # same, other order
    (("python/l/f.py", "file", ""), ("python/l", "link", "lib")),  # below a symlink, first
    (("python/l", "link", "lib"), ("python/l/f.py", "file", "")),  # same, other order
])
def test_members_below_a_non_directory_are_rejected_in_any_order(members: tuple) -> None:
    assert any(p.startswith("member below a non-directory") for p in _problems(*members))


def test_valid_internal_symlink_chains_are_accepted() -> None:
    manifest = gate.validate_archive(_tar(
        *BASE,
        ("python/bin/python3", "link", "python3.14"),
        ("python/bin/python", "link", "python3"),  # two hops
        ("python/lib/current", "link", "."),  # a directory link
        ("python/bin/os", "link", "../lib/current/os.py"),  # through a directory link
        ("python/bin/up", "link", "../bin/./python"),  # '..' and '.' inside the archive
    ))
    assert manifest["python/bin/python"].kind == "link"


def test_extraction_refuses_a_symlink_leaving_the_destination(tmp_path: Path) -> None:
    data = _tar(*BASE, ("python/bin/python3", "link", "python3.14"))
    manifest = gate.validate_archive(data)
    manifest["python/bin/python3"] = gate.Entry("link", target="../../../../outside")
    with pytest.raises(gate.ExtractionMismatch):
        gate.safe_extract(data, manifest, tmp_path / "dest")


def test_modified_extraction_is_detected(tmp_path: Path) -> None:
    data = _tar(*BASE)
    manifest = gate.validate_archive(data)
    gate.safe_extract(data, manifest, tmp_path / "dest")
    assert gate.verify_extraction(manifest, tmp_path / "dest") == 2
    (tmp_path / "dest" / "python" / "lib" / "os.py").write_bytes(b"y")
    with pytest.raises(gate.ExtractionMismatch):
        gate.verify_extraction(manifest, tmp_path / "dest")


# --- F03: the archive is read once; the path is watched ----------------------------------


def _gates_1_to_3(tmp_path: Path, installed_pair: tuple[Path, Path]) -> tuple[Any, _FakeRun]:
    archive, runtime_dir = installed_pair
    digest = gate.sha256_file(archive)
    attestation = json.dumps([{"verificationResult": {
        "statement": {"subject": [{"digest": {"sha256": digest}}]},
        "signature": {"certificate": {"buildSignerURI": gate.SIGNER_URI}}}}])
    run = _FakeRun(**{"git status": (0, ""), "gh api": (0, f"sha256:{digest}\n"),
                      "attestation verify": (0, attestation)})
    ctx = _context(tmp_path, archive, runtime_dir, passed=[], run=run)
    return ctx, run


def _rewrite_in_place(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    with open(path, "r+b") as handle:  # same inode
        handle.write(bytes(data))


def test_archive_changed_after_item_1_fails_item_2(tmp_path: Path,
                                                   installed: tuple[Path, Path]) -> None:
    ctx, run = _gates_1_to_3(tmp_path, installed)
    assert gate.step0_clean_tree(ctx).status == gate.PASS
    ctx.passed.append("step0")
    assert gate.gate1_archive_sha256(ctx).status == gate.PASS
    ctx.passed.append("1")
    _rewrite_in_place(ctx.settings.archive)
    result = gate.gate2_release_digest(ctx)
    assert result.status == gate.FAIL and "archive" in result.evidence
    assert not any("gh" in " ".join(c) for c in run.commands)


def test_archive_replaced_after_item_2_fails_item_3(tmp_path: Path,
                                                    installed: tuple[Path, Path]) -> None:
    ctx, run = _gates_1_to_3(tmp_path, installed)
    for check, gate_id in ((gate.step0_clean_tree, "step0"), (gate.gate1_archive_sha256, "1"),
                           (gate.gate2_release_digest, "2")):
        assert check(ctx).status == gate.PASS
        ctx.passed.append(gate_id)
    archive = ctx.settings.archive
    replacement = archive.with_name("replacement")
    replacement.write_bytes(archive.read_bytes())  # same content, another file
    os.replace(replacement, archive)
    result = gate.gate3_provenance(ctx)
    assert result.status == gate.FAIL
    assert "replaced or modified" in result.evidence["archive"]


def test_same_name_with_another_sha256_fails(tmp_path: Path,
                                             installed: tuple[Path, Path]) -> None:
    ctx, _ = _gates_1_to_3(tmp_path, installed)
    ctx.passed[:] = ["step0"]
    assert gate.gate1_archive_sha256(ctx).status == gate.PASS
    ctx.passed.append("1")
    archive = ctx.settings.archive
    other = archive.with_name("other")
    other.write_bytes(archive.read_bytes() + b"\x00")
    os.replace(other, archive)
    assert gate.gate2_release_digest(ctx).status == gate.FAIL


def test_item_3_verifies_a_private_copy_of_the_verified_bytes(
        tmp_path: Path, installed: tuple[Path, Path]) -> None:
    ctx, run = _gates_1_to_3(tmp_path, installed)
    for check, gate_id in ((gate.step0_clean_tree, "step0"), (gate.gate1_archive_sha256, "1"),
                           (gate.gate2_release_digest, "2")):
        assert check(ctx).status == gate.PASS
        ctx.passed.append(gate_id)
    result = gate.gate3_provenance(ctx)
    assert result.status == gate.PASS, result.evidence
    (command,) = [c for c in run.commands if "attestation" in c]
    assert command[3] != str(ctx.settings.archive)  # never the original path
    assert result.evidence["verified_copy_sha256_after"] == ctx.artifact.sha256
    assert list((tmp_path / "work").iterdir()) == []  # the copy is removed


def test_a_copy_changed_during_item_3_fails(tmp_path: Path,
                                            installed: tuple[Path, Path]) -> None:
    ctx, run = _gates_1_to_3(tmp_path, installed)
    for check, gate_id in ((gate.step0_clean_tree, "step0"), (gate.gate1_archive_sha256, "1"),
                           (gate.gate2_release_digest, "2")):
        assert check(ctx).status == gate.PASS
        ctx.passed.append(gate_id)
    inner = ctx.run

    def tampering(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if "attestation" in command:
            copy = Path(command[3])
            copy.chmod(0o600)
            copy.write_bytes(b"swapped")
        return inner(command, **kwargs)

    ctx.run = tampering
    result = gate.gate3_provenance(ctx)
    assert result.status == gate.FAIL and result.evidence["archive"] == "verified copy changed"


def test_input_changed_during_extraction_fails_item_4(
        tmp_path: Path, installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    archive, runtime_dir = installed
    ctx = _context(tmp_path, archive, runtime_dir)
    original = gate.safe_extract

    def extract_while_the_file_changes(*args: Any) -> None:
        _rewrite_in_place(archive)
        original(*args)

    monkeypatch.setattr(gate, "safe_extract", extract_while_the_file_changes)
    result = gate.gate4_runtime(ctx)
    assert result.status == gate.FAIL
    assert "archive" in result.evidence and "archive_unchanged_at_end" not in result.evidence


def test_extraction_changed_after_extraction_fails_item_4(
        tmp_path: Path, installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    archive, runtime_dir = installed
    ctx = _context(tmp_path, archive, runtime_dir)
    original = gate.safe_extract

    def extract_then_tamper(data: Any, manifest: Any, destination: Path) -> None:
        original(data, manifest, destination)
        (destination / "python" / "lib" / "pkg" / "other.py").write_text("VALUE = 2\n")

    monkeypatch.setattr(gate, "safe_extract", extract_then_tamper)
    result = gate.gate4_runtime(ctx)
    assert result.status == gate.FAIL and "extraction" in result.evidence


def test_item_4_records_the_artifact_and_the_end_check(
        tmp_path: Path, installed: tuple[Path, Path]) -> None:
    result = gate.gate4_runtime(_context(tmp_path, *installed))
    assert result.status == gate.PASS, result.evidence
    assert result.evidence["artifact"]["sha256"] == gate.sha256_file(installed[0])
    assert result.evidence["archive_unchanged_at_end"] is True
    assert result.evidence["recompilation"]["extracted_files_verified"] > 0


def test_symlinked_archive_is_refused(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    link = tmp_path / "link.tar.gz"
    link.symlink_to(installed[0])
    with pytest.raises(gate.GateError):
        gate.read_artifact(link)


def test_world_writable_installed_file_fails(tmp_path: Path,
                                             installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    _pyc(runtime_dir, "other").chmod(0o646)
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["installed_world_writable_files"] == [
        f"python/lib/pkg/__pycache__/{_pyc(runtime_dir, 'other').name}"]


# --- F04: trusted tools ------------------------------------------------------------------


def _tool(directory: Path, name: str, mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\necho tool\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _toolbox(tmp_path: Path) -> Path:
    trusted = tmp_path / "trusted"
    for name in gate.TOOLS:
        _tool(trusted, name)
    return trusted


def test_tools_come_from_trusted_directories_not_from_path(
        tmp_path: Path, installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    trusted = _toolbox(tmp_path)
    evil = _tool(tmp_path / "evil", "git")
    monkeypatch.setenv("PATH", f"{evil.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    for planted in ("PERL5OPT", "PERL5LIB", "GIT_DIR", "GIT_CONFIG_PARAMETERS",
                    "DYLD_INSERT_LIBRARIES", "DEVELOPER_DIR", "PYTHONPATH"):
        monkeypatch.setenv(planted, "planted")
    run = _FakeRun(**{"--version": (0, "tool 1.0\n"), "git status": (0, "")})
    ctx = _context(tmp_path, *installed, passed=[], run=run,
                   tool_dirs=(str(trusted),), tool_roots=(str(trusted),))
    ctx.tools = None
    result = gate.step0_clean_tree(ctx)
    assert result.status == gate.PASS, result.evidence
    real_trusted = os.path.realpath(trusted)
    assert ctx.tools["git"] == os.path.join(real_trusted, "git")  # the checked real path
    # What PATH would have run is only recorded. On a noexec tmp (the CI container)
    # shutil.which skips the planted file, so compare with what it really finds here.
    would_run = shutil.which("git")
    if would_run is not None and os.path.realpath(would_run) != ctx.tools["git"]:
        assert result.evidence["inherited_path_would_differ"]["git"] == would_run
    else:
        assert "git" not in result.evidence["inherited_path_would_differ"]
    assert result.evidence["tools"]["git"]["sha256"] == gate.sha256_file(trusted / "git")
    assert all(c[0].startswith(real_trusted) for c in run.commands)
    assert ctx.tool_environment["PATH"] == str(trusted)
    assert set(ctx.tool_environment) <= {*gate.TOOL_ENVIRONMENT_KEEP, "PATH"}
    for planted in ("PERL5OPT", "PERL5LIB", "GIT_DIR", "GIT_CONFIG_PARAMETERS",
                    "DYLD_INSERT_LIBRARIES", "DEVELOPER_DIR", "PYTHONPATH"):
        assert planted not in ctx.tool_environment
        assert planted in result.evidence["dropped_variables"]


def test_tool_environment_keeps_only_the_allowlist() -> None:
    environment = gate.tool_environment(
        {"HOME": "/h", "LANG": "C", "GH_TOKEN": "t", "PERL5OPT": "-MX", "GIT_DIR": "/g",
         "PATH": "/evil"}, ("/usr/bin", "/bin"))
    assert environment == {"HOME": "/h", "LANG": "C", "GH_TOKEN": "t",
                           "PATH": f"/usr/bin{os.pathsep}/bin"}


@pytest.mark.parametrize("problem", ["outside", "group-writable", "world-writable",
                                     "world-writable-dir", "missing", "not-executable"])
def test_untrusted_tools_block_step_0(tmp_path: Path, installed: tuple[Path, Path],
                                      problem: str) -> None:
    trusted = _toolbox(tmp_path)
    git = trusted / "git"
    if problem == "outside":
        git.unlink()
        git.symlink_to(_tool(tmp_path / "elsewhere", "git"))
    elif problem == "group-writable":
        git.chmod(0o775)
    elif problem == "world-writable":
        git.chmod(0o757)
    elif problem == "world-writable-dir":
        trusted.chmod(0o777)
    elif problem == "missing":
        git.unlink()
    else:
        git.chmod(0o644)
    run = _FakeRun(**{"--version": (0, "tool 1.0\n"), "git status": (0, "")})
    ctx = _context(tmp_path, *installed, passed=[], run=run,
                   tool_dirs=(str(trusted),), tool_roots=(str(trusted),))
    ctx.tools = None
    try:
        results = gate.run_gates(ctx)
    finally:
        trusted.chmod(0o755)
    assert [r.status for r in results] == [gate.BLOCKED] + [gate.NOT_RUN] * 7
    assert "ToolUntrusted" in results[0].evidence["error"]
    assert run.commands == []


def test_legacy_fingerprint_uses_the_resolved_tools_and_checks_each_step(tmp_path: Path) -> None:
    ctx = _context(tmp_path, tmp_path / "missing", tmp_path)
    ctx.tools = {name: f"/trusted/{name}" for name in gate.TOOLS}
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(command)
        code = 1 if command[0].endswith("sort") else 0
        return subprocess.CompletedProcess(command, code, b"", b"")

    ctx.run = run
    with pytest.raises(gate.GateError, match="sort failed"):
        gate.legacy_fingerprint(ctx, tmp_path)
    assert [c[0] for c in calls] == ["/trusted/find", "/trusted/sort"]


# --- the other findings -------------------------------------------------------------------


def test_gate5_checks_the_venv_python_before_running_it(
        tmp_path: Path, installed: tuple[Path, Path]) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to("/usr/bin/true")
    run = _FakeRun()
    ctx = _context(tmp_path, *installed, run=run, new_venv=venv,
                   passed=["step0", "1", "2", "3", "4"])
    result = gate.gate5_venv_resolution(ctx)
    assert result.status == gate.FAIL and run.commands == []


def _venv_with(tmp_path: Path, runtime: Path, python: str, home: Path) -> Path:
    """A venv whose bin/python is a 'copy' of the runtime's python3.14, a 'bad-copy', or a
    'symlink' into the runtime's bin, with pyvenv.cfg's home set to `home`."""
    (runtime / "bin").mkdir(parents=True, exist_ok=True)
    (runtime / "bin" / "python3.14").write_bytes(b"interpreter")
    venv = tmp_path / f"venv-{python}"
    (venv / "bin").mkdir(parents=True)
    target = venv / "bin" / "python"
    if python == "symlink":
        target.symlink_to(runtime / "bin" / "python3.14")
    else:
        target.write_bytes(b"interpreter" if python == "copy" else b"something else")
    (venv / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.14.10\n", encoding="utf-8")
    return venv


@pytest.mark.parametrize("python, home_ok, expected", [
    ("copy", True, True),  # venv --copies, as .venv-py314-r5 was made
    ("symlink", True, True),
    ("bad-copy", True, False),
    ("copy", False, False),
    ("symlink", False, False),
])
def test_venv_python_origin(tmp_path: Path, python: str, home_ok: bool, expected: bool) -> None:
    runtime = tmp_path / "runtime"
    home = runtime / "bin" if home_ok else tmp_path / "elsewhere"
    venv = _venv_with(tmp_path, runtime, python, home)
    origin = gate.venv_python_origin(venv, runtime)
    assert origin["ok"] is expected, origin


def test_venv_copy_is_compared_with_the_verified_hash(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    venv = _venv_with(tmp_path, runtime, "copy", runtime / "bin")
    verified = gate.sha256_file(runtime / "bin" / "python3.14")
    assert gate.venv_python_origin(venv, runtime, verified)["ok"] is True
    # The runtime file changed after item 4: the verified hash, not the file, decides.
    (runtime / "bin" / "python3.14").write_bytes(b"swapped after item 4")
    (venv / "bin" / "python").write_bytes(b"swapped after item 4")
    assert gate.venv_python_origin(venv, runtime, verified)["ok"] is False


def test_pth_files_are_recorded(tmp_path: Path) -> None:
    site = tmp_path / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "distutils-precedence.pth").write_text("import os\n", encoding="utf-8")
    assert list(gate.pth_files(tmp_path)) == [
        "lib/python3.14/site-packages/distutils-precedence.pth"]


def test_rollback_check_error_is_blocked(tmp_path: Path) -> None:
    def broken(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise OSError("boom")

    legacy = tmp_path / "legacy"
    (legacy / "bin").mkdir(parents=True)
    python = legacy / "bin" / "python"
    python.write_text("", encoding="utf-8")
    (legacy / "bin" / "mtg-notion-manager").write_text(f"#!{python}\n", encoding="utf-8")
    settings = gate.Settings(archive=tmp_path / "a", repo=tmp_path, legacy_venv=legacy)
    result = gate.rollback_readiness(settings, broken)
    assert result["status"] == gate.BLOCKED and "OSError" in result["error"]


def test_report_metadata(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    run = _FakeRun(**{"rev-parse": (0, "abc123\n")})
    ctx = _context(tmp_path, *installed, run=run)
    metadata = gate.run_metadata(ctx, "2026-01-01T00:00:00.000000Z")
    assert metadata["commit"] == "abc123"
    assert metadata["script_sha256"] == gate.sha256_file(Path(gate.__file__))
    assert metadata["started_at"] == "2026-01-01T00:00:00.000000Z" and metadata["finished_at"]
    assert metadata["archive"]["sha256"] == gate.sha256_file(installed[0])
