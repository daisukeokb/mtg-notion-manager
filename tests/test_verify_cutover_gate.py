"""Phase 3H-R6: scripts/verify_cutover_gate.py (the Cutover Safety Gate) on synthetic fixtures.

Nothing here reads the installed runtime, a virtual environment of the
repository, the network or any credential: the archive, the "installed"
runtime and the gh / git / interpreter answers are all made up in tmp_path.
The recompilation uses the interpreter running the tests in place of the fresh
extraction, without the macOS sandbox (which is only used by the real run).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import platform
import py_compile
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_cutover_gate.py"
DOC = REPO_ROOT / "docs" / "local-python-runtime.md"
TAG = sys.implementation.cache_tag
EPOCH = 1704067200
MODULE = '"""Docstring."""\nVALUE = 1\nassert VALUE\n'


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("verify_cutover_gate_r6", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


gate = _load()


def _compile(source: Path, cfile: Path, dfile: str, optimize: int = 0,
             mode: py_compile.PycInvalidationMode = py_compile.PycInvalidationMode.TIMESTAMP
             ) -> None:
    # In a fresh process, as the runtime does on import: marshal output can depend on
    # the reference counts of the compiling process (FLAG_REF), so the long-running
    # test process would not stand in for it.
    code = ("import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], "
            "dfile=sys.argv[3], doraise=True, optimize=int(sys.argv[4]), "
            "invalidation_mode=py_compile.PycInvalidationMode[sys.argv[5]])")
    subprocess.run([sys.executable, "-I", "-B", "-S", "-c", code, str(source), str(cfile),
                    dfile, str(optimize), mode.name], check=True, timeout=60,
                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})


def _build_archive(tmp: Path, extra: list[tarfile.TarInfo] | None = None) -> Path:
    """A small python-build-standalone-like archive with one shipped (stale) .pyc."""
    build = tmp / "build"
    lib = build / "python" / "lib" / "pkg"
    (lib / "__pycache__").mkdir(parents=True)
    (build / "python" / "bin").mkdir()
    for name in ("mod.py", "other.py"):
        (lib / name).write_text(MODULE, encoding="utf-8")
    # Like the real archive: compiled at build time, sources normalized afterwards.
    _compile(lib / "mod.py", lib / "__pycache__" / f"mod.{TAG}.pyc", "/build/python/lib/pkg/mod.py")
    for name in ("mod.py", "other.py"):
        os.utime(lib / name, (EPOCH, EPOCH))
    tool = build / "python" / "bin" / "tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    (build / "python" / "bin" / "link").symlink_to("tool")
    archive_dir = tmp / "dl"
    archive_dir.mkdir()
    archive = archive_dir / gate.ARCHIVE_NAME
    def like_the_real_archive(info: tarfile.TarInfo) -> tarfile.TarInfo:
        # The symlinks of the real archive are 0o775. A symlink created here takes the
        # host's mode: 0o755 on macOS, but 0o777 (world-writable, rejected) on Linux CI.
        if info.issym():
            info.mode = 0o775
        return info

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(build / "python", arcname="python", filter=like_the_real_archive)
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
    return archive


def _install(tmp: Path, archive: Path) -> Path:
    """Extract like the setup did, then let "the runtime" (re)write its caches."""
    runtime_dir = tmp / "rt"
    gate.safe_extract(archive, gate.validate_archive(archive), runtime_dir)
    lib = runtime_dir / "python" / "lib" / "pkg"
    for name in ("mod", "other"):
        source = lib / f"{name}.py"
        _compile(source, lib / "__pycache__" / f"{name}.{TAG}.pyc", str(source))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (runtime_dir / "ARCHIVE_SHA256").write_text(digest + "\n", encoding="utf-8")
    return runtime_dir


class _LocalIsolation(gate.Isolation):
    def verify(self, ctx: Any, python: Path, home_file: Path) -> dict:
        return dict(gate.PROBE_EXPECTED)


def _context(tmp: Path, archive: Path, runtime_dir: Path, *, passed: list[str] | None = None,
             run: Any = None, isolation: Any = None, **settings: Any) -> Any:
    (tmp / "work").mkdir(exist_ok=True)
    options = {"archive": archive, "repo": tmp, "runtime_dir": runtime_dir,
               "work_parent": tmp / "work", "python_version": platform.python_version(),
               "cache_tag": TAG,
               "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()
               if archive.is_file() else "0" * 64}
    options.update(settings)
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return gate.Context(
        gate.Settings(**options), run or gate.default_run,
        isolation or (lambda work: _LocalIsolation(work, [], {**environment, "HOME": str(work)})),
        interpreter=lambda fresh: Path(sys.executable),
        passed=["step0", "1", "2", "3"] if passed is None else passed)


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path]:
    archive = _build_archive(tmp_path)
    return archive, _install(tmp_path, archive)


def _pyc(runtime_dir: Path, name: str, opt: str = "") -> Path:
    return runtime_dir / "python" / "lib" / "pkg" / "__pycache__" / f"{name}.{TAG}{opt}.pyc"


# --- gate 4: recompilation and comparison ---------------------------------------------------


def test_identical_recompilation_passes(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    result = gate.gate4_runtime(_context(tmp_path, *installed))
    assert result.status == gate.PASS, result.evidence
    recompiled = result.evidence["recompilation"]
    assert recompiled["jobs"] == recompiled["identical"] == 2
    assert result.evidence["archived_pyc"] == 1 and result.evidence["generated_pyc"] == 1


def test_work_directory_is_removed_and_nothing_else_written(
        tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    before = {p: p.stat().st_mtime_ns for p in runtime_dir.rglob("*")}
    gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert {p: p.stat().st_mtime_ns for p in runtime_dir.rglob("*")} == before
    assert list((tmp_path / "work").iterdir()) == []


def test_different_source_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    source = runtime_dir / "python" / "lib" / "pkg" / "other.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["different"] == ["python/lib/pkg/other.py"]


def test_different_bytecode_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    pyc = _pyc(runtime_dir, "other")
    data = bytearray(pyc.read_bytes())
    data[-1] ^= 0xFF
    pyc.write_bytes(bytes(data))
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    (mismatch,) = result.evidence["recompilation"]["mismatches"]
    assert mismatch["pyc"].endswith(f"other.{TAG}.pyc")
    assert mismatch["difference"]["header"] is False
    assert mismatch["installed_sha256"] != mismatch["recompiled_sha256"]


def test_difference_report_classifies_flag_ref_only_changes() -> None:
    base = bytes(16) + b"\x7a\x05hello\xe3\x01"
    flagged = bytes(16) + b"\xfa\x05hello\xe3\x01"
    report = gate.first_difference(base, flagged)
    assert report["flag_ref_bit_only"] is True and report["differing_bytes"] == 1
    assert report["bytes"] == [[16, "7a", "fa"]] and report["header"] is False
    other = gate.first_difference(base, bytes(16) + b"\x7a\x05jello\xe3\x01")
    assert other["flag_ref_bit_only"] is False
    assert gate.first_difference(base, base + b"x")["flag_ref_bit_only"] is False


def test_header_mismatch_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    pyc = _pyc(runtime_dir, "mod")
    data = bytearray(pyc.read_bytes())
    data[8] ^= 0x01  # source mtime in the header
    pyc.write_bytes(bytes(data))
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["recompilation"]["mismatches"][0]["difference"]["header"] is True


@pytest.mark.parametrize("truncate", [0, 8, 15, 40])
def test_malformed_or_truncated_pyc_fails(tmp_path: Path, installed: tuple[Path, Path],
                                          truncate: int) -> None:
    archive, runtime_dir = installed
    pyc = _pyc(runtime_dir, "other")
    pyc.write_bytes(pyc.read_bytes()[:truncate])
    assert gate.gate4_runtime(_context(tmp_path, archive, runtime_dir)).status == gate.FAIL


def test_wrong_optimization_level_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    source = runtime_dir / "python" / "lib" / "pkg" / "other.py"
    _compile(source, _pyc(runtime_dir, "other", ".opt-1"), str(source), optimize=0)
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    (mismatch,) = result.evidence["recompilation"]["mismatches"]
    assert mismatch["conditions"]["optimize"] == 1


def test_correct_optimization_level_passes(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    source = runtime_dir / "python" / "lib" / "pkg" / "other.py"
    _compile(source, _pyc(runtime_dir, "other", ".opt-2"), str(source), optimize=2)
    assert gate.gate4_runtime(_context(tmp_path, archive, runtime_dir)).status == gate.PASS


def test_incorrect_dfile_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    source = runtime_dir / "python" / "lib" / "pkg" / "other.py"
    _compile(source, _pyc(runtime_dir, "other"), "/elsewhere/other.py")
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL


def test_invalidation_mode_mismatch_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    source = runtime_dir / "python" / "lib" / "pkg" / "other.py"
    _compile(source, _pyc(runtime_dir, "other"), str(source),
             mode=py_compile.PycInvalidationMode.CHECKED_HASH)
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["recompilation"]["mismatches"][0]["flags"] == 3


def test_missing_source_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    (runtime_dir / "python" / "lib" / "pkg" / "other.py").unlink()
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["missing"] == ["python/lib/pkg/other.py"]


def test_missing_shipped_pyc_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    _pyc(runtime_dir, "mod").unlink()
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["missing"] == [f"python/lib/pkg/__pycache__/mod.{TAG}.pyc"]


@pytest.mark.parametrize("name", [
    f"ghost.{TAG}.pyc",  # no verified source
    "other.cpython-27.pyc",  # another interpreter's cache
    "notes.txt",
    f"other.{TAG}.opt-3.pyc",
    "evil.so",
])
def test_unexpected_cache_entry_fails(tmp_path: Path, installed: tuple[Path, Path],
                                      name: str) -> None:
    archive, runtime_dir = installed
    (runtime_dir / "python" / "lib" / "pkg" / "__pycache__" / name).write_bytes(b"x")
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["unexpected"] == [f"python/lib/pkg/__pycache__/{name}"]


def test_unexpected_cache_path_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    stray = runtime_dir / "python" / "lib" / "pkg" / "__pycache__" / "__pycache__"
    stray.mkdir()
    assert gate.gate4_runtime(_context(tmp_path, archive, runtime_dir)).status == gate.FAIL


def test_unexpected_executable_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    _pyc(runtime_dir, "other").chmod(0o755)
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL
    assert result.evidence["unexpected"] == [f"python/lib/pkg/__pycache__/other.{TAG}.pyc"]


def test_executable_bit_change_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    (runtime_dir / "python" / "bin" / "tool").chmod(0o644)
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.evidence["different"] == ["python/bin/tool"]


@pytest.mark.parametrize("where", ["extra", "retarget", "replace-file"])
def test_unexpected_symlink_fails(tmp_path: Path, installed: tuple[Path, Path], where: str) -> None:
    archive, runtime_dir = installed
    bin_dir = runtime_dir / "python" / "bin"
    if where == "extra":
        (bin_dir / "extra").symlink_to("tool")
    elif where == "retarget":
        (bin_dir / "link").unlink()
        (bin_dir / "link").symlink_to("/bin/sh")
    else:
        (bin_dir / "tool").unlink()
        (bin_dir / "tool").symlink_to("/bin/sh")
    assert gate.gate4_runtime(_context(tmp_path, archive, runtime_dir)).status == gate.FAIL


def _member(name: str, kind: bytes = tarfile.REGTYPE, linkname: str = "", mode: int = 0o644
            ) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, linkname, mode
    info.size = 1 if kind == tarfile.REGTYPE else 0
    return info


@pytest.mark.parametrize("member", [
    _member("../evil"),
    _member("/abs/evil"),
    _member("python/../../evil"),
    _member("other-root/file"),
    _member("python/bin/escape", tarfile.SYMTYPE, "../../outside"),
    _member("python/bin/absolute", tarfile.SYMTYPE, "/etc/passwd"),
    _member("python/bin/hard", tarfile.LNKTYPE, "python/bin/tool"),
    _member("python/bin/fifo", tarfile.FIFOTYPE),
    _member("python/bin/dev", tarfile.CHRTYPE),
    _member("python/bin/setuid", mode=0o4755),
    _member("python/bin/world-writable", mode=0o666),
    _member("python/bin/link/through", ),  # below a symlink member
], ids=lambda m: m.name)
def test_invalid_archive_member_fails(tmp_path: Path, member: tarfile.TarInfo) -> None:
    archive = _build_archive(tmp_path, [member])
    with pytest.raises(gate.ArchiveRejected):
        gate.validate_archive(archive)
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    result = gate.gate4_runtime(_context(tmp_path, archive, runtime_dir))
    assert result.status == gate.FAIL and result.evidence["archive"]


def test_duplicate_or_conflicting_member_fails(tmp_path: Path) -> None:
    archive = _build_archive(tmp_path, [_member("python/lib/pkg/mod.py")])
    with pytest.raises(gate.ArchiveRejected, match="duplicate"):
        gate.validate_archive(archive)


def test_unreadable_archive_fails(tmp_path: Path) -> None:
    archive = tmp_path / gate.ARCHIVE_NAME
    archive.write_bytes(b"not a tar file")
    with pytest.raises(gate.ArchiveRejected, match="unreadable"):
        gate.validate_archive(archive)


def test_wrong_python_version_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    result = gate.gate4_runtime(_context(tmp_path, *installed, python_version="3.0.0"))
    assert result.status == gate.FAIL and result.evidence["identity"] is False


def test_unavailable_isolation_is_blocked(tmp_path: Path, installed: tuple[Path, Path],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(work: Path) -> Any:
        raise gate.IsolationUnavailable("no sandbox here")

    result = gate.gate4_runtime(_context(tmp_path, *installed, isolation=unavailable))
    assert result.status == gate.BLOCKED and result.evidence["isolation"] == "no sandbox here"
    monkeypatch.setattr(gate.os, "access", lambda path, mode: False)
    with pytest.raises(gate.IsolationUnavailable, match="sandbox-exec"):
        gate.macos_sandbox(tmp_path)


@pytest.mark.parametrize("observed", [
    {**gate.PROBE_EXPECTED, "network": "allowed"},
    {**gate.PROBE_EXPECTED, "write_outside": "allowed"},
    {**gate.PROBE_EXPECTED, "read_home": "allowed"},
    {**gate.PROBE_EXPECTED, "write_work": "denied"},
    {"error": "sandbox-exec: invalid profile"},
])
def test_isolation_probe_that_does_not_show_isolation_is_blocked(
        tmp_path: Path, installed: tuple[Path, Path], observed: dict) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert "--compile-worker" not in command  # never reached
        return subprocess.CompletedProcess(command, 0, json.dumps(observed), "")

    isolation = lambda work: gate.Isolation(work, [], {})  # noqa: E731 (real verify())
    result = gate.gate4_runtime(_context(tmp_path, *installed, run=run, isolation=isolation))
    assert result.status == gate.BLOCKED


def test_isolated_commands_get_only_the_sanitized_environment(tmp_path: Path) -> None:
    if sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK):
        isolation = gate.Isolation(tmp_path, ["/usr/bin/sandbox-exec", "-p", "profile"],
                                   {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    else:
        isolation = gate.macos_sandbox(tmp_path)
    seen: dict = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    isolation.run(run, ["python", "-I"])
    assert set(seen["env"]) <= {"PATH", "HOME", "TMPDIR", "LC_ALL"}
    assert not any(key.startswith(("PYTHON", "NOTION_")) for key in seen["env"])


def test_sandbox_profile_denies_network_home_reads_and_outside_writes() -> None:
    profile = gate.SANDBOX_PROFILE
    assert "(deny network*)" in profile
    assert '(deny file-read* (subpath "{home}"))' in profile
    allow_work = '(allow file-write* (subpath "{work}"))'
    assert profile.index("(deny file-write*)") < profile.index(allow_work)


def test_unauthorized_output_path_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "mod.py"
    source.write_text(MODULE, encoding="utf-8")
    (tmp_path / "out").mkdir()
    outside = tmp_path / "outside.pyc"
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"installed": "x", "fresh_source": str(source), "dfile": "x.py",
                                 "optimize": 0, "cfile": str(outside)}]), encoding="utf-8")
    assert gate.compile_worker(str(jobs), str(tmp_path / "r.json"), str(tmp_path / "out")) == 1
    assert not outside.exists()
    assert "refused output" in json.loads((tmp_path / "r.json").read_text())["errors"][0]


# --- gate ordering ---------------------------------------------------------------------------


def test_each_gate_requires_every_earlier_gate() -> None:
    for index, check in enumerate(gate.GATES):
        assert tuple(check.requires) == gate.GATE_IDS[:index]


@pytest.mark.parametrize("passed", [[], ["step0", "1", "2"], ["step0", "2", "3"]])
def test_gate4_refuses_to_run_before_its_prerequisites(
        tmp_path: Path, installed: tuple[Path, Path], passed: list[str]) -> None:
    with pytest.raises(gate.GateOrderError):
        gate.gate4_runtime(_context(tmp_path, *installed, passed=passed))
    assert list((tmp_path / "work").iterdir()) == []  # no comparison was started


class _FakeRun:
    """Answers for git / gh / interpreters; records every command."""

    def __init__(self, **answers: tuple[int, str]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.commands.append(command)
        for key, (code, out) in self.answers.items():
            if key in " ".join(command):
                return subprocess.CompletedProcess(command, code, out, "")
        raise AssertionError(f"unexpected command: {command}")


def test_prerequisite_failure_leaves_later_gates_not_run(
        tmp_path: Path, installed: tuple[Path, Path]) -> None:
    run = _FakeRun(**{"git status": (0, " M config/card_match_overrides.json\n")})
    results = gate.run_gates(_context(tmp_path, *installed, passed=[], run=run))
    assert [r.status for r in results] == [gate.FAIL] + [gate.NOT_RUN] * 7
    assert run.commands == [["git", "status", "--short", "--", *gate.STEP0_PATHS]]


def test_blocked_gate_stops_the_run(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    archive, runtime_dir = installed
    run = _FakeRun(**{"git status": (0, "")})
    ctx = _context(tmp_path, tmp_path / "missing.tar.gz", runtime_dir, passed=[], run=run)
    results = gate.run_gates(ctx)
    assert [r.status for r in results][:3] == [gate.PASS, gate.BLOCKED, gate.NOT_RUN]
    assert gate.exit_status(results) == 2


def test_unexpected_exception_fails_closed(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    def boom(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise KeyError("boom")

    results = gate.run_gates(_context(tmp_path, *installed, passed=[], run=boom))
    assert results[0].status == gate.BLOCKED and "KeyError" in results[0].evidence["error"]
    assert {r.status for r in results[1:]} == {gate.NOT_RUN}


def test_step0_covers_config() -> None:
    assert "config" in gate.STEP0_PATHS and "src" in gate.STEP0_PATHS


# --- gates 1-3 -------------------------------------------------------------------------------


def _attestation(digest: str, signer: str = gate.SIGNER_URI) -> str:
    return json.dumps([{"verificationResult": {
        "statement": {"subject": [{"name": "x", "digest": {"sha256": digest}}]},
        "signature": {"certificate": {"buildSignerURI": signer}}}}])


def test_gates_1_to_3_pass_on_matching_evidence(tmp_path: Path, installed: tuple[Path, Path]
                                                ) -> None:
    archive, runtime_dir = installed
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    run = _FakeRun(**{"git status": (0, ""), "gh api": (0, f"sha256:{digest}\n"),
                      "gh attestation": (0, _attestation(digest))})
    ctx = _context(tmp_path, archive, runtime_dir, passed=[], run=run)
    results = gate.run_gates(ctx, gate.GATES[:4])
    assert [r.status for r in results] == [gate.PASS] * 4
    assert "--signer-workflow" in run.commands[-1]


def test_archive_sha_mismatch_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    ctx = _context(tmp_path, *installed, passed=["step0"], archive_sha256="0" * 64)
    assert gate.gate1_archive_sha256(ctx).status == gate.FAIL


def test_tampered_archive_sha256_record_fails(tmp_path: Path, installed: tuple[Path, Path]
                                              ) -> None:
    archive, runtime_dir = installed
    (runtime_dir / "ARCHIVE_SHA256").write_text("0" * 64, encoding="utf-8")
    ctx = _context(tmp_path, archive, runtime_dir, passed=["step0"])
    assert gate.gate1_archive_sha256(ctx).status == gate.FAIL


def test_missing_archive_is_blocked(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    ctx = _context(tmp_path, tmp_path / "absent.tar.gz", installed[1], passed=["step0"])
    with pytest.raises(gate.GateError):
        gate.gate1_archive_sha256(ctx)


def test_release_digest_mismatch_fails(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    run = _FakeRun(**{"gh api": (0, "sha256:" + "0" * 64)})
    ctx = _context(tmp_path, *installed, passed=["step0", "1"], run=run)
    assert gate.gate2_release_digest(ctx).status == gate.FAIL


@pytest.mark.parametrize("answer", [
    (1, ""),
    (0, "not json"),
    (0, "[]"),
    (0, _attestation("0" * 64)),
    (0, "SIGNER"),
])
def test_invalid_provenance_fails(tmp_path: Path, installed: tuple[Path, Path],
                                  answer: tuple[int, str]) -> None:
    archive, runtime_dir = installed
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if answer[1] == "SIGNER":
        answer = (0, _attestation(digest, signer="https://github.com/evil/fork/.github/x.yml@x"))
    ctx = _context(tmp_path, archive, runtime_dir, passed=["step0", "1", "2"],
                   run=_FakeRun(**{"gh attestation": answer}))
    assert gate.gate3_provenance(ctx).status == gate.FAIL


# --- gates 5-8 and rollback --------------------------------------------------------------------


def _after_gate4(tmp: Path, installed: tuple[Path, Path], run: Any, upto: str) -> Any:
    ids = list(gate.GATE_IDS[:gate.GATE_IDS.index(upto)])
    return _context(tmp, *installed, passed=ids, run=run, new_venv=tmp / "venv",
                    legacy_venv=tmp / "legacy")


def test_gate5_checks_the_venv_resolution(tmp_path: Path, installed: tuple[Path, Path]) -> None:
    runtime_root = installed[1] / "python"
    venv = tmp_path / "venv"
    good = f"{venv}/bin/python\n{venv}\n{runtime_root}\n"
    identity = (0, f"CPython {platform.python_version()} arm64\n")
    run = _FakeRun(**{"python3.14": identity, "venv/bin/python": (0, good)})
    ok = gate.gate5_venv_resolution(_after_gate4(tmp_path, installed, run, "5"))
    assert ok.status == gate.PASS
    wrong = f"/usr/bin/python3\n/usr\n{runtime_root}\n"
    run = _FakeRun(**{"python3.14": identity, "venv/bin/python": (0, wrong)})
    bad = gate.gate5_venv_resolution(_after_gate4(tmp_path, installed, run, "5"))
    assert bad.status == gate.FAIL


def test_gate6_7_fails_when_the_lock_check_fails(tmp_path: Path, installed: tuple[Path, Path]
                                                 ) -> None:
    ctx = _after_gate4(tmp_path, installed, _FakeRun(**{"check_local_runtime": (1, "")}), "6-7")
    assert gate.gate6_7_lock_and_pip_check(ctx).status == gate.FAIL


def test_gate8_requires_the_documented_legacy_state(
        tmp_path: Path, installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(**{"--version": (0, gate.LEGACY_VERSION + "\n"), "freeze": (0, "a==1\n")})
    ctx = _after_gate4(tmp_path, installed, run, "8")
    assert gate.gate8_legacy(ctx).status == gate.FAIL  # legacy environment unavailable
    (tmp_path / "legacy" / "bin").mkdir(parents=True)
    (tmp_path / "legacy" / "bin" / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(gate, "legacy_fingerprint", lambda run, venv: gate.LEGACY_FINGERPRINT)
    monkeypatch.setattr(gate, "LEGACY_FREEZE", gate.freeze_hash("a==1\n"))
    assert gate.gate8_legacy(ctx).status == gate.PASS
    monkeypatch.setattr(gate, "legacy_fingerprint", lambda run, venv: "0" * 64)
    assert gate.gate8_legacy(ctx).status == gate.FAIL


def test_freeze_hash_ignores_only_the_editable_lines() -> None:
    plain = "a==1\nb==2\n"
    assert gate.freeze_hash(plain) == hashlib.sha256(plain.encode()).hexdigest()
    assert gate.freeze_hash("a==1\n-e git+https://x@abc#egg=y\nb==2\n") == gate.freeze_hash(plain)
    assert gate.freeze_hash("a==1\nb==3\n") != gate.freeze_hash(plain)


def _legacy(tmp: Path, shebang_target: Path | None = None) -> Path:
    legacy = tmp / "legacy"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python3").write_text("", encoding="utf-8")
    target = shebang_target or legacy / "bin" / "python3"
    (legacy / "bin" / "mtg-notion-manager").write_text(
        f"#!{target}\nimport sys\n", encoding="utf-8")
    return legacy


def test_rollback_readiness_rehearsal(tmp_path: Path) -> None:
    legacy = _legacy(tmp_path)
    run = _FakeRun(**{"--legacy-cli-worker": (0, ""), "--version": (0, gate.LEGACY_VERSION)})
    settings = gate.Settings(archive=tmp_path, repo=REPO_ROOT, legacy_venv=legacy)
    ready = gate.rollback_readiness(settings, run)
    assert ready["status"] == gate.PASS, ready
    assert any("--legacy-cli-worker" in command for command in run.commands)


@pytest.mark.parametrize("broken", ["no-launcher", "foreign-shebang", "version", "help"])
def test_rollback_prerequisite_missing_fails(tmp_path: Path, broken: str) -> None:
    other = tmp_path / "other" / "bin"
    other.mkdir(parents=True)
    (other / "python3").write_text("", encoding="utf-8")
    legacy = _legacy(tmp_path, other / "python3" if broken == "foreign-shebang" else None)
    if broken == "no-launcher":
        (legacy / "bin" / "mtg-notion-manager").unlink()
    run = _FakeRun(**{"--legacy-cli-worker": (1 if broken == "help" else 0, ""),
                      "--version": (0, "Python 3.14.10" if broken == "version"
                                    else gate.LEGACY_VERSION)})
    settings = gate.Settings(archive=tmp_path, repo=REPO_ROOT, legacy_venv=legacy)
    assert gate.rollback_readiness(settings, run)["status"] == gate.FAIL


def test_environment_notice_lists_names_only() -> None:
    notice = gate.environment_notice({"PYTHONPATH": "/x", "NOTION_API_KEY": "synthetic-value",
                                      "HOME": "/h"})
    assert notice == {"python": ["PYTHONPATH"], "notion": ["NOTION_API_KEY"]}
    assert "synthetic-value" not in json.dumps(notice)


def test_report_file_is_never_overwritten(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("keep", encoding="utf-8")
    assert gate.main(["--archive", str(tmp_path / "a.tar.gz"), "--report", str(report)]) == 2
    assert report.read_text(encoding="utf-8") == "keep"


def test_expected_values_are_the_documented_ones() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for value in (gate.ARCHIVE_NAME, gate.ARCHIVE_SHA256, gate.LEGACY_FINGERPRINT,
                  gate.LEGACY_FREEZE, gate.LEGACY_VERSION, gate.RELEASE_TAG,
                  "release.yml@refs/heads/main", "scripts/verify_cutover_gate.py",
                  "git status --short -- " + " ".join(gate.STEP0_PATHS)):
        assert value in doc, value
