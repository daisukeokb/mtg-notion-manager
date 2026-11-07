"""Phase 3H-R6: run the Cutover Safety Gate of docs/local-python-runtime.md in order.

    /usr/bin/python3 -I -B scripts/verify_cutover_gate.py --archive ARCHIVE [--report FILE]

ARCHIVE is the python-build-standalone archive downloaded again, as gate item 1
describes, into a new empty directory outside the repository. The script is run
with an interpreter other than the installed CPython 3.14 (the macOS system
Python is enough; only the standard library is used): the installed runtime is
not executed before its files have been verified (item 4).

The gates run strictly in the documented order: Step 0, items 1-8. Each gate
declares the gates it requires and refuses to run (GateOrderError, BLOCKED)
unless they have all passed in this run; after the first result other than PASS
the remaining gates are NOT_RUN. Nothing from an earlier run is reused.

Item 4 compares every archive member with the installed tree (content, type,
symlink target, executable bit; missing and unexpected entries) without
following symlinks. Every installed .pyc is then recompiled from its verified
source by the attested interpreter of a fresh extraction of the archive in a
temporary directory, inside a macOS sandbox (sandbox-exec) that denies network
access, reads of the home directory and writes outside the temporary directory;
the sandbox is probed before use. The result must be byte-for-byte identical to
the installed .pyc, header included. The installed runtime, its .pyc files and
both virtual environments are only read. If the sandbox is not available or the
probe does not show it working, item 4 is BLOCKED.

The script reads no .env file and no credentials and does not contact Notion.
It uses the network only through `gh` (items 2 and 3). It is a check, not an
enforcement: nothing prevents starting the CLI without it.

Exit status: 0 = every gate PASS, 1 = a gate FAIL, 2 = BLOCKED / NOT_VERIFIED
or a usage error. The report (JSON) is printed and, with --report, written to
a new file.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent

# The expected values of docs/local-python-runtime.md (tested to match it).
ARCHIVE_NAME = "cpython-3.14.10+20261026-aarch64-apple-darwin-install_only.tar.gz"
ARCHIVE_SHA256 = "30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4"
RELEASE_REPO = "astral-sh/python-build-standalone"
RELEASE_TAG = "20261026"
SIGNER_WORKFLOW = f"{RELEASE_REPO}/.github/workflows/release.yml"
SIGNER_URI = f"https://github.com/{SIGNER_WORKFLOW}@refs/heads/main"
RUNTIME_DIR = Path.home() / ".local/share/mtg-notion-manager/runtimes/cpython-3.14.10+20261026"
PYTHON_VERSION = "3.14.10"
CACHE_TAG = "cpython-314"
LEGACY_VERSION = "Python 3.9.6"
LEGACY_FINGERPRINT = "c5e0c2be7730366ea961e699524232522383890d3d86f92eb2647cc851ee5087"
LEGACY_FREEZE = "54d878d575ac887517cd30433dec08e1c4b03cb9862999b54ae498617680e7de"
#: Step 0: no uncommitted change that the cutover would use.
STEP0_PATHS = ("src", "tools", "requirements", "scripts", "pyproject.toml", "config")

PASS, FAIL, BLOCKED, NOT_VERIFIED, NOT_RUN = "PASS", "FAIL", "BLOCKED", "NOT_VERIFIED", "NOT_RUN"
ARCHIVE_ROOT = "python"
_GENERATED_PYC = re.compile(
    r"^(?P<dir>.+)/__pycache__/(?P<stem>[A-Za-z_][A-Za-z0-9_]*)\.(?P<tag>[a-z]+-\d+)"
    r"(?:\.opt-(?P<opt>[12]))?\.pyc$"
)


class GateOrderError(RuntimeError):
    """A gate was called before every gate it requires had passed."""


class GateError(Exception):
    """A check that cannot be completed (the gate is BLOCKED)."""


@dataclasses.dataclass
class Settings:
    archive: Path
    repo: Path = REPO_ROOT
    runtime_dir: Path = RUNTIME_DIR
    new_venv: Path | None = None
    legacy_venv: Path | None = None
    work_parent: Path | None = None
    python_version: str = PYTHON_VERSION
    cache_tag: str = CACHE_TAG
    archive_sha256: str = ARCHIVE_SHA256

    @property
    def runtime_root(self) -> Path:
        return self.runtime_dir / ARCHIVE_ROOT


@dataclasses.dataclass
class Context:
    settings: Settings
    run: Callable[..., subprocess.CompletedProcess]
    isolation: Callable[[Path], Isolation]
    #: The interpreter of the fresh extraction (its bin/python3).
    interpreter: Callable[[Path], Path] = lambda fresh: fresh / ARCHIVE_ROOT / "bin" / "python3"
    passed: list[str] = dataclasses.field(default_factory=list)
    manifest: Manifest | None = None


@dataclasses.dataclass
class GateResult:
    gate: str
    name: str
    status: str
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)


Check = Callable[["Context"], "GateResult"]


def requires(*gates: str) -> Callable[[Check], Check]:
    """Refuse to run the gate unless every gate in `gates` passed in this run."""

    def decorate(check: Check) -> Check:
        def guarded(ctx: Context) -> GateResult:
            missing = [gate for gate in gates if gate not in ctx.passed]
            if missing:
                raise GateOrderError(f"{check.__name__} requires {missing} to have passed first")
            return check(ctx)

        guarded.__name__ = check.__name__
        guarded.requires = gates  # type: ignore[attr-defined]
        return guarded

    return decorate


def default_run(command: list[str], text: bool = True, **kwargs: Any
                ) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=text, check=False, timeout=600,
                          **kwargs)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- Step 0, items 1-3 ----------------------------------------------------------------


@requires()
def step0_clean_tree(ctx: Context) -> GateResult:
    result = ctx.run(["git", "status", "--short", "--", *STEP0_PATHS], cwd=str(ctx.settings.repo))
    if result.returncode != 0:
        raise GateError(f"git status failed ({result.returncode})")
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    return GateResult("step0", "clean tree", FAIL if changed else PASS,
                      {"paths": list(STEP0_PATHS), "changed": changed})


@requires("step0")
def gate1_archive_sha256(ctx: Context) -> GateResult:
    archive = ctx.settings.archive
    if not archive.is_file() or archive.is_symlink():
        raise GateError(f"archive not found (or not a regular file): {archive}")
    actual = sha256_file(archive)
    record = ctx.settings.runtime_dir / "ARCHIVE_SHA256"
    recorded = record.read_text(encoding="utf-8").strip() if record.is_file() else None
    ok = archive.name == ARCHIVE_NAME and actual == ctx.settings.archive_sha256 \
        and recorded == ctx.settings.archive_sha256
    return GateResult("1", "archive SHA-256", PASS if ok else FAIL,
                      {"name": archive.name, "sha256": actual,
                       "expected": ctx.settings.archive_sha256, "ARCHIVE_SHA256": recorded})


@requires("step0", "1")
def gate2_release_digest(ctx: Context) -> GateResult:
    query = f'.assets[] | select(.name=="{ARCHIVE_NAME}") | .digest'
    result = ctx.run(["gh", "api", f"repos/{RELEASE_REPO}/releases/tags/{RELEASE_TAG}",
                      "--jq", query])
    if result.returncode != 0:
        raise GateError(f"gh api failed ({result.returncode})")
    digest = result.stdout.strip()
    return GateResult("2", "release asset digest",
                      PASS if digest == f"sha256:{ctx.settings.archive_sha256}" else FAIL,
                      {"digest": digest})


@requires("step0", "1", "2")
def gate3_provenance(ctx: Context) -> GateResult:
    result = ctx.run(["gh", "attestation", "verify", str(ctx.settings.archive), "-R",
                      RELEASE_REPO, "--signer-workflow", SIGNER_WORKFLOW, "--format", "json"])
    if result.returncode != 0:
        return GateResult("3", "provenance", FAIL, {"exit": result.returncode})
    try:
        verified = json.loads(result.stdout)
        subjects = {s["digest"]["sha256"] for entry in verified
                    for s in entry["verificationResult"]["statement"]["subject"]}
        signers = {entry["verificationResult"]["signature"]["certificate"]["buildSignerURI"]
                   for entry in verified}
    except (ValueError, KeyError, TypeError) as exc:
        return GateResult("3", "provenance", FAIL, {"error": f"unreadable output: {exc}"})
    ok = bool(verified) and ctx.settings.archive_sha256 in subjects and signers == {SIGNER_URI}
    return GateResult("3", "provenance", PASS if ok else FAIL,
                      {"attestations": len(verified), "signers": sorted(signers),
                       "subject_matches": ctx.settings.archive_sha256 in subjects})


# --- item 4: archive validation and tree comparison -------------------------------------


@dataclasses.dataclass
class Entry:
    kind: str  # "file", "dir", "link"
    sha256: str = ""
    executable: bool = False
    mode: int = 0
    mtime: int = 0
    size: int = 0
    target: str = ""


Manifest = dict  # archive path -> Entry


def validate_archive(archive: Path) -> Manifest:
    """Every member of the archive, after the safety checks; raises GateError otherwise."""
    manifest: dict[str, Entry] = {}
    problems: list[str] = []
    try:
        tar = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveRejected([f"unreadable archive: {exc}"]) from exc
    with tar:
        for member in tar:
            name = member.name
            parts = name.rstrip("/").split("/")
            normalized = "/".join(parts)
            if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in parts) \
                    or parts[0] != ARCHIVE_ROOT:
                problems.append(f"unsafe path: {name!r}")
                continue
            if normalized in manifest:
                problems.append(f"duplicate member: {normalized}")
                continue
            for i in range(1, len(parts)):
                parent = manifest.get("/".join(parts[:i]))
                if parent is not None and parent.kind != "dir":
                    problems.append(f"member below a non-directory: {normalized}")
            if member.mode & 0o7002:
                problems.append(f"special or world-writable mode {oct(member.mode)}: {normalized}")
            if member.isreg():
                handle = tar.extractfile(member)
                assert handle is not None
                manifest[normalized] = Entry("file", hashlib.sha256(handle.read()).hexdigest(),
                                             bool(member.mode & 0o100), member.mode & 0o777,
                                             int(member.mtime), member.size)
            elif member.isdir():
                manifest[normalized] = Entry("dir", mode=member.mode & 0o777)
            elif member.issym():
                target = os.path.normpath(
                    os.path.join(os.path.dirname(normalized), member.linkname))
                if member.linkname.startswith("/") or not (
                        target == ARCHIVE_ROOT or target.startswith(ARCHIVE_ROOT + "/")):
                    problems.append(f"symlink escapes the archive: {normalized}")
                manifest[normalized] = Entry("link", target=member.linkname)
            else:
                problems.append(f"unexpected member type {member.type!r}: {normalized}")
    if problems:
        raise ArchiveRejected(problems)
    return manifest


class ArchiveRejected(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} unsafe archive members: {problems[:5]}")
        self.problems = problems


def implied_directories(manifest: Manifest) -> set[str]:
    implied = {ARCHIVE_ROOT}
    for name in manifest:
        parts = name.split("/")
        implied.update("/".join(parts[:i]) for i in range(1, len(parts)))
    return implied


def installed_entry(path: Path) -> Entry:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return Entry("link", target=os.readlink(path))
    if stat.S_ISDIR(info.st_mode):
        return Entry("dir")
    if stat.S_ISREG(info.st_mode):
        return Entry("file", sha256_file(path), bool(info.st_mode & 0o100), info.st_mode & 0o777,
                     int(info.st_mtime), info.st_size)
    return Entry("other")


def scan_installed(root: Path) -> dict[str, Entry]:
    """The installed tree as archive paths, without following any symlink."""
    entries = {ARCHIVE_ROOT: installed_entry(root)}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(directory) / name
            entries[ARCHIVE_ROOT + "/" + path.relative_to(root).as_posix()] = installed_entry(path)
    return entries


def _same(archived: Entry, installed: Entry) -> bool:
    if archived.kind != installed.kind:
        return False
    if archived.kind == "file":
        return (archived.sha256, archived.executable) == (installed.sha256, installed.executable)
    if archived.kind == "link":
        return archived.target == installed.target
    return True


@dataclasses.dataclass
class Comparison:
    missing: list[str]
    different: list[str]
    unexpected: list[str]
    generated_pyc: list[str]
    archived_pyc: list[str]
    cache_dirs: list[str]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.different or self.unexpected)


def compare_tree(manifest: Manifest, installed: dict[str, Entry], cache_tag: str) -> Comparison:
    """Regular comparison for every member except .pyc (those are recompiled)."""
    missing, different, unexpected, generated, archived_pyc, cache_dirs = [], [], [], [], [], []
    for name, entry in sorted(manifest.items()):
        if name not in installed:
            missing.append(name)
        elif name.endswith(".pyc") and entry.kind == "file":
            if installed[name].kind != "file" or installed[name].executable:
                different.append(name)
            else:
                archived_pyc.append(name)
        elif not _same(entry, installed[name]):
            different.append(name)
    implied = implied_directories(manifest)
    for name, entry in sorted(installed.items()):
        if name in manifest or (name in implied and entry.kind == "dir"):
            continue
        parent = name.rsplit("/", 1)[0]
        if entry.kind == "dir" and name.endswith("/__pycache__") \
                and not parent.endswith("/__pycache__") \
                and (parent in implied or manifest.get(parent, Entry("")).kind == "dir"):
            cache_dirs.append(name)
            continue
        match = _GENERATED_PYC.match(name)
        source = f"{match['dir']}/{match['stem']}.py" if match else ""
        if match and match["tag"] == cache_tag and entry.kind == "file" and not entry.executable \
                and manifest.get(source, Entry("")).kind == "file":
            generated.append(name)
        else:
            unexpected.append(name)
    return Comparison(missing, different, unexpected, generated, archived_pyc, cache_dirs)


# --- item 4: isolated recompilation --------------------------------------------------------


def safe_extract(archive: Path, manifest: Manifest, destination: Path) -> None:
    """Extract the validated archive into an empty directory (no overwrite, no link following)."""
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        members = {member.name.rstrip("/"): member for member in tar}
        for name in sorted(manifest, key=lambda n: (n.count("/"), n)):
            entry = manifest[name]
            target = destination / name
            if entry.kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or not target.is_dir():
                    raise GateError(f"extraction conflict at {name}")
            elif entry.kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(members[name])
                assert handle is not None
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600)
                with os.fdopen(descriptor, "wb") as out:
                    shutil.copyfileobj(handle, out)
                os.chmod(target, entry.mode & 0o755)
                os.utime(target, (entry.mtime, entry.mtime))
        for name, entry in manifest.items():
            if entry.kind == "link":
                (destination / name).parent.mkdir(parents=True, exist_ok=True)
                os.symlink(entry.target, destination / name)
    for directory, dirnames, filenames in os.walk(destination, followlinks=False):
        for name in dirnames + filenames:
            path = Path(directory) / name
            if not str(path.resolve()).startswith(str(destination.resolve()) + os.sep) \
                    and not path.is_symlink():
                raise GateError(f"extraction left the destination: {path}")


@dataclasses.dataclass
class Job:
    installed: str  # installed .pyc, archive path
    source: str  # verified source, archive path
    optimize: int
    dfile: str  # the source path the installed runtime compiles with
    cfile: str  # output inside the work directory


def plan_jobs(comparison: Comparison, runtime_root: Path, out_dir: Path) -> list[Job]:
    jobs = []
    for index, name in enumerate(sorted(comparison.archived_pyc + comparison.generated_pyc)):
        match = _GENERATED_PYC.match(name)
        if not match:
            raise GateError(f"unrecognized .pyc name: {name}")
        source = f"{match['dir']}/{match['stem']}.py"
        dfile = str(runtime_root / source[len(ARCHIVE_ROOT) + 1:])
        jobs.append(Job(name, source, int(match["opt"] or 0), dfile, str(out_dir / f"{index}.pyc")))
    return jobs


SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-read* (subpath "{home}"))
(deny file-write*)
(allow file-write* (subpath "{work}"))
(allow file-write-data (literal "/dev/null"))
"""


@dataclasses.dataclass
class Isolation:
    """Runs a command inside the sandbox for one work directory."""

    work: Path
    command: list[str]
    environment: dict[str, str]

    def run(self, run: Callable[..., subprocess.CompletedProcess],
            argv: list[str]) -> subprocess.CompletedProcess:
        return run([*self.command, *argv], env=self.environment, cwd=str(self.work))

    def verify(self, ctx: Context, python: Path, home_file: Path) -> dict:
        return probe_isolation(ctx, self, python, home_file)


def macos_sandbox(work: Path) -> Isolation:
    tool = "/usr/bin/sandbox-exec"
    if sys.platform != "darwin" or not os.access(tool, os.X_OK):
        raise IsolationUnavailable(f"{tool} is not available on {sys.platform}")
    profile = SANDBOX_PROFILE.format(home=os.path.realpath(Path.home()),
                                     work=os.path.realpath(work))
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(work), "TMPDIR": str(work / "tmp"),
                   "LC_ALL": "C"}
    return Isolation(work, [tool, "-p", profile], environment)


class IsolationUnavailable(Exception):
    pass


PROBE = r"""
import errno, json, socket, sys
work, outside, private = sys.argv[1], sys.argv[2], sys.argv[3]
result = {}
try:
    socket.create_connection(("192.0.2.1", 9), timeout=3).close()
    result["network"] = "allowed"
except OSError as exc:
    denied = exc.errno in (errno.EPERM, errno.EACCES)
    result["network"] = "denied" if denied else f"error {exc.errno}"
for key, path, mode in (("write_outside", outside, "w"), ("read_home", private, "r"),
                        ("write_work", work, "w")):
    try:
        with open(path, mode) as handle:
            handle.write("probe") if mode == "w" else handle.read(1)
        result[key] = "allowed"
    except OSError as exc:
        result[key] = "denied" if exc.errno in (errno.EPERM, errno.EACCES) else f"error {exc.errno}"
print(json.dumps(result))
"""

PROBE_EXPECTED = {"network": "denied", "write_outside": "denied", "read_home": "denied",
                  "write_work": "allowed"}


def probe_isolation(ctx: Context, isolation: Isolation, python: Path, home_file: Path) -> dict:
    """Show that the sandbox denies network, home reads and outside writes (BLOCKED if not)."""
    outside_dir = Path(tempfile.mkdtemp(prefix="r6-outside-"))
    try:
        probe = isolation.work / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        outside = outside_dir / "must-not-exist"
        result = isolation.run(ctx.run, [str(python), "-I", "-B", "-S", str(probe),
                                         str(isolation.work / "probe.out"), str(outside),
                                         str(home_file)])
        try:
            observed = json.loads(result.stdout)
        except ValueError:
            observed = {"error": (result.stderr or "")[-300:]}
        if outside.exists():
            observed["write_outside"] = "allowed"
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)
    if observed != PROBE_EXPECTED:
        raise IsolationUnavailable(f"sandbox probe did not show isolation: {observed}")
    return observed


def compile_worker(jobs_file: str, result_file: str, out_dir: str) -> int:
    """Run by the isolated interpreter: compile each job (timestamp-based) into out_dir."""
    import importlib.util
    import py_compile

    out = os.path.realpath(out_dir)
    jobs = json.loads(Path(jobs_file).read_text(encoding="utf-8"))
    errors = []
    for job in jobs:
        cfile = os.path.realpath(job["cfile"])
        if os.path.dirname(cfile) != out:
            errors.append(f"refused output outside the work directory: {job['cfile']}")
            continue
        try:
            py_compile.compile(job["fresh_source"], cfile=cfile, dfile=job["dfile"], doraise=True,
                               optimize=job["optimize"],
                               invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        except py_compile.PyCompileError as exc:
            errors.append(f"{job['installed']}: {exc.msg}")
    Path(result_file).write_text(json.dumps({
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "magic": importlib.util.MAGIC_NUMBER.hex(),
        "cache_tag": sys.implementation.cache_tag,
        "errors": errors,
    }), encoding="utf-8")
    return 1 if errors else 0


def first_difference(a: bytes, b: bytes) -> dict:
    """Where two .pyc files differ (diagnostics only; any difference is a FAIL).

    `flag_ref_bit_only` is true when the lengths are equal and every differing
    byte differs only in 0x80, marshal's FLAG_REF bit, which depends on the
    reference counts of the compiling process rather than on the code.
    """
    offsets = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(a) != len(b):
        offsets.append(min(len(a), len(b)))
    offset = offsets[0] if offsets else min(len(a), len(b))
    end = offset
    while end < min(len(a), len(b)) and a[end] != b[end]:
        end += 1
    return {"offset": offset, "range": [offset, max(end, offset + 1)], "header": offset < 16,
            "differing_bytes": len(offsets), "lengths": [len(a), len(b)],
            "bytes": [[i, a[i:i + 1].hex(), b[i:i + 1].hex()] for i in offsets[:20]],
            "flag_ref_bit_only": len(a) == len(b) and bool(offsets)
            and all(a[i] ^ b[i] == 0x80 for i in offsets)}


def recompile_and_compare(ctx: Context, comparison: Comparison, manifest: Manifest) -> dict:
    settings = ctx.settings
    work = Path(tempfile.mkdtemp(prefix="r6-gate4-", dir=settings.work_parent))
    try:
        (work / "tmp").mkdir()
        out_dir = work / "out"
        out_dir.mkdir()
        fresh = work / "fresh"
        safe_extract(settings.archive, manifest, fresh)
        python = ctx.interpreter(fresh)
        isolation = ctx.isolation(work)
        probe = isolation.verify(ctx, python, settings.repo / "pyproject.toml")
        jobs = plan_jobs(comparison, settings.runtime_root, out_dir)
        runtime_rows = []
        for job in jobs:
            # The source is byte-identical (checked above); its mtime is the installed one,
            # which is what the installed runtime wrote into the header. Only the copy in
            # the work directory is touched.
            installed_source = settings.runtime_root / job.source[len(ARCHIVE_ROOT) + 1:]
            info = os.lstat(installed_source)
            fresh_source = fresh / job.source
            os.utime(fresh_source, ns=(info.st_mtime_ns, info.st_mtime_ns))
            row = dataclasses.asdict(job)
            row["fresh_source"] = str(fresh_source)
            runtime_rows.append(row)
        jobs_file, result_file = work / "jobs.json", work / "result.json"
        jobs_file.write_text(json.dumps(runtime_rows), encoding="utf-8")
        worker = work / "verify_cutover_gate.py"
        shutil.copyfile(Path(__file__), worker)
        done = isolation.run(ctx.run, [str(python), "-I", "-B", "-S", str(worker),
                                       "--compile-worker", str(jobs_file), str(result_file),
                                       str(out_dir)])
        if not result_file.is_file():
            raise GateError(f"compile worker failed ({done.returncode}): {done.stderr[-300:]}")
        report = json.loads(result_file.read_text(encoding="utf-8"))
        mismatches = []
        identical = 0
        for job in jobs:
            installed = (settings.runtime_root / job.installed[len(ARCHIVE_ROOT) + 1:]).read_bytes()
            produced = Path(job.cfile).read_bytes() if Path(job.cfile).is_file() else b""
            if installed == produced:
                identical += 1
                continue
            mismatches.append({
                "pyc": job.installed, "installed_sha256": hashlib.sha256(installed).hexdigest(),
                "recompiled_sha256": hashlib.sha256(produced).hexdigest(),
                "difference": first_difference(installed, produced),
                "conditions": {"optimize": job.optimize, "dfile": job.dfile,
                               "invalidation_mode": "TIMESTAMP"},
                "flags": struct.unpack("<I", installed[4:8])[0] if len(installed) >= 8 else None,
            })
        return {"probe": probe, "interpreter": report, "jobs": len(jobs), "identical": identical,
                "mismatches": mismatches}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@requires("step0", "1", "2", "3")
def gate4_runtime(ctx: Context) -> GateResult:
    settings = ctx.settings
    try:
        manifest = validate_archive(settings.archive)
    except ArchiveRejected as exc:
        return GateResult("4", "local Python runtime", FAIL, {"archive": exc.problems})
    ctx.manifest = manifest
    if not settings.runtime_root.is_dir() or settings.runtime_root.is_symlink():
        raise GateError(f"installed runtime not found: {settings.runtime_root}")
    comparison = compare_tree(manifest, scan_installed(settings.runtime_root), settings.cache_tag)
    evidence: dict[str, Any] = {
        "archive_members": len(manifest),
        "regular_entries": sum(1 for name in manifest if not name.endswith(".pyc")),
        "missing": comparison.missing, "different": comparison.different,
        "unexpected": comparison.unexpected, "archived_pyc": len(comparison.archived_pyc),
        "generated_pyc": len(comparison.generated_pyc), "cache_dirs": len(comparison.cache_dirs),
    }
    if not comparison.ok:
        return GateResult("4", "local Python runtime", FAIL, evidence)
    try:
        recompiled = recompile_and_compare(ctx, comparison, manifest)
    except IsolationUnavailable as exc:
        evidence["isolation"] = str(exc)
        return GateResult("4", "local Python runtime", BLOCKED, evidence)
    evidence["recompilation"] = recompiled
    interpreter = recompiled["interpreter"]
    installed_magic = {
        (settings.runtime_root / name[len(ARCHIVE_ROOT) + 1:]).read_bytes()[:4].hex()
        for name in comparison.archived_pyc + comparison.generated_pyc}
    identity = (interpreter["implementation"] == "CPython"
                and interpreter["version"] == settings.python_version
                and interpreter["cache_tag"] == settings.cache_tag
                and installed_magic <= {interpreter["magic"]})
    evidence["identity"] = identity
    ok = identity and not interpreter["errors"] and not recompiled["mismatches"] \
        and recompiled["identical"] == recompiled["jobs"]
    return GateResult("4", "local Python runtime", PASS if ok else FAIL, evidence)


# --- items 5-8 ------------------------------------------------------------------------------


def _venv(ctx: Context, name: str) -> Path:
    path = getattr(ctx.settings, name)
    return path if path is not None else ctx.settings.repo / {
        "new_venv": ".venv-py314-r5", "legacy_venv": ".venv"}[name]


@requires("step0", "1", "2", "3", "4")
def gate5_venv_resolution(ctx: Context) -> GateResult:
    runtime_python = ctx.settings.runtime_root / "bin" / "python3.14"
    identity = ctx.run([str(runtime_python), "-I", "-B", "-c",
                        "import platform; print(platform.python_implementation(), "
                        "platform.python_version(), platform.machine())"])
    venv = _venv(ctx, "new_venv")
    resolved = ctx.run([str(venv / "bin" / "python"), "-I", "-B", "-c",
                        "import sys; print(sys.executable); print(sys.prefix); "
                        "print(sys.base_prefix)"])
    lines = resolved.stdout.splitlines()
    expected_venv = os.path.realpath(venv)
    ok = (identity.returncode == 0
          and identity.stdout.split()[:2] == ["CPython", ctx.settings.python_version]
          and resolved.returncode == 0 and len(lines) == 3
          and os.path.realpath(os.path.dirname(os.path.dirname(lines[0]))) == expected_venv
          and os.path.realpath(lines[1]) == expected_venv
          and os.path.realpath(lines[2]) == os.path.realpath(ctx.settings.runtime_root))
    return GateResult("5", "venv resolution", PASS if ok else FAIL,
                      {"runtime": identity.stdout.strip(), "executable_prefix_base": lines})


@requires("step0", "1", "2", "3", "4", "5")
def gate6_7_lock_and_pip_check(ctx: Context) -> GateResult:
    venv = _venv(ctx, "new_venv")
    result = ctx.run([str(venv / "bin" / "python"), "-I", "-B",
                      str(ctx.settings.repo / "scripts" / "check_local_runtime.py")],
                     cwd=str(ctx.settings.repo))
    return GateResult("6-7", "lock consistency and pip check",
                      PASS if result.returncode == 0 else FAIL,
                      {"exit": result.returncode, "output": result.stdout.strip().splitlines()})


def legacy_fingerprint(run: Callable[..., subprocess.CompletedProcess], venv: Path) -> str:
    """The documented pipeline, run with the same tools (find | sort -z | xargs shasum)."""
    found = run(["find", ".", "-path", "./lib/python3.9/site-packages/__pycache__", "-prune",
                 "-o", "-name", "*.pyc", "-prune", "-o", "-type", "f", "-print0"],
                cwd=str(venv), text=False)
    ordered = subprocess.run(["sort", "-z"], input=found.stdout, stdout=subprocess.PIPE,
                             check=True)
    listed = subprocess.run(["xargs", "-0", "shasum", "-a", "256"], input=ordered.stdout,
                            stdout=subprocess.PIPE, cwd=str(venv), check=True)
    return hashlib.sha256(listed.stdout).hexdigest()


def freeze_hash(text: str) -> str:
    kept = [line for line in text.splitlines() if not line.startswith("-e ")]
    return hashlib.sha256("".join(f"{line}\n" for line in kept).encode()).hexdigest()


@requires("step0", "1", "2", "3", "4", "5", "6-7")
def gate8_legacy(ctx: Context) -> GateResult:
    venv = _venv(ctx, "legacy_venv")
    python = venv / "bin" / "python"
    if not python.exists():
        return GateResult("8", "legacy environment", FAIL, {"error": f"{python} not found"})
    version = ctx.run([str(python), "--version"])
    freeze = ctx.run([str(python), "-I", "-B", "-m", "pip", "freeze", "--all"])
    fingerprint = legacy_fingerprint(ctx.run, venv)
    observed = {"version": (version.stdout or version.stderr).strip(),
                "fingerprint": fingerprint,
                "freeze": freeze_hash(freeze.stdout) if freeze.returncode == 0 else None}
    ok = observed == {"version": LEGACY_VERSION, "fingerprint": LEGACY_FINGERPRINT,
                      "freeze": LEGACY_FREEZE}
    return GateResult("8", "legacy environment", PASS if ok else FAIL, observed)


GATES: tuple[Callable[[Context], GateResult], ...] = (
    step0_clean_tree, gate1_archive_sha256, gate2_release_digest, gate3_provenance,
    gate4_runtime, gate5_venv_resolution, gate6_7_lock_and_pip_check, gate8_legacy,
)
GATE_IDS = ("step0", "1", "2", "3", "4", "5", "6-7", "8")


def run_gates(ctx: Context, gates: tuple[Callable[[Context], GateResult], ...] = GATES
              ) -> list[GateResult]:
    results: list[GateResult] = []
    stopped = False
    for gate_id, gate in zip(GATE_IDS, gates):
        if stopped:
            results.append(GateResult(gate_id, gate.__name__, NOT_RUN))
            continue
        try:
            result = gate(ctx)
        except GateOrderError as exc:
            result = GateResult(gate_id, gate.__name__, BLOCKED, {"order": str(exc)})
        except Exception as exc:  # fail closed: anything unexpected stops the run
            result = GateResult(gate_id, gate.__name__, BLOCKED,
                                {"error": f"{type(exc).__name__}: {exc}"})
        results.append(result)
        if result.status == PASS:
            ctx.passed.append(result.gate)
        else:
            stopped = True
    return results


# --- rollback readiness (after the gates) and environment notice -----------------------------


def legacy_cli_worker(check_script: str) -> int:
    """Run by the legacy interpreter: render the CLI's --help under the audit hook."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_local_runtime", check_script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    problems = module.check_cli()
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    return 1 if problems else 0


def rollback_readiness(settings: Settings, run: Callable[..., subprocess.CompletedProcess]
                       ) -> dict[str, Any]:
    """Read-only: can the CLI be started from the legacy environment again?"""
    legacy = settings.legacy_venv or settings.repo / ".venv"
    launcher = legacy / "bin" / "mtg-notion-manager"
    checks: dict[str, Any] = {"launcher": launcher.is_file() and not launcher.is_symlink()}
    interpreter = None
    if checks["launcher"]:
        first = launcher.read_text(encoding="utf-8").splitlines()[:1]
        shebang = first[0][2:].strip() if first and first[0].startswith("#!") else ""
        candidate = Path(shebang)
        checks["shebang_is_legacy_python"] = bool(shebang) and candidate.name.startswith(
            "python") and candidate.parent.exists() and os.path.samefile(
            candidate.parent, legacy / "bin")
        if checks["shebang_is_legacy_python"] and candidate.exists():
            interpreter = candidate
    checks["interpreter"] = interpreter is not None
    if interpreter is not None:
        version = run([str(interpreter), "--version"])
        checks["version"] = (version.stdout or version.stderr).strip()
        helped = run([str(interpreter), "-I", "-B", str(Path(__file__).resolve()),
                      "--legacy-cli-worker",
                      str(settings.repo / "scripts" / "check_local_runtime.py")],
                     cwd=str(settings.repo))
        checks["cli_help_under_audit_hook"] = helped.returncode == 0
    ready = checks["launcher"] and checks.get("shebang_is_legacy_python") is True \
        and checks["interpreter"] and checks.get("version") == LEGACY_VERSION \
        and checks.get("cli_help_under_audit_hook") is True
    return {"status": PASS if ready else FAIL, "checks": checks}


def environment_notice(environ: dict[str, str]) -> dict[str, list[str]]:
    """Names only (never values) of variables that change how the CLI starts or connects."""
    return {"python": sorted(k for k in environ if k.startswith("PYTHON")),
            "notion": sorted(k for k in environ if k.startswith("NOTION_"))}


def exit_status(results: list[GateResult]) -> int:
    statuses = {result.status for result in results}
    if statuses == {PASS}:
        return 0
    return 1 if FAIL in statuses else 2


def main(argv: list[str]) -> int:
    if argv[:1] == ["--compile-worker"] and len(argv) == 4:
        return compile_worker(*argv[1:])
    if argv[:1] == ["--legacy-cli-worker"] and len(argv) == 2:
        return legacy_cli_worker(argv[1])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.report is not None and (args.report.exists() or args.report.is_symlink()):
        print(f"refusing to overwrite {args.report}", file=sys.stderr)
        return 2
    ctx = Context(Settings(archive=args.archive.absolute()), default_run, macos_sandbox)
    results = run_gates(ctx)
    status = exit_status(results)
    rollback = rollback_readiness(ctx.settings, ctx.run) if status == 0 else {"status": NOT_RUN}
    if status == 0 and rollback["status"] != PASS:
        status = 1
    report = json.dumps({"gates": [dataclasses.asdict(result) for result in results],
                         "rollback_readiness": rollback,
                         "environment_notice": environment_notice(dict(os.environ))},
                        indent=2, ensure_ascii=False)
    print(report)
    if args.report is not None:
        with open(args.report, "x", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
