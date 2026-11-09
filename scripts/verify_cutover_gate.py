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
the installed .pyc, header included (BYTE_IDENTICAL). A .pyc that is not is
parsed, without being unmarshalled or run, by the CPython 3.14.10 marshal
reader below (Human Acceptance Decision 03); it is accepted as
MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF only if the headers are identical, both
streams parse completely, their trees are identical once TYPE_REF is resolved,
the reader has just been checked against the isolated interpreter's own marshal
output, and the file is one of the four of REF_LAYOUT_APPROVED with the content
the decision was taken on. Anything else is STRUCTURAL_MISMATCH or REJECTED,
and FAIL. The installed runtime, its .pyc files and
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
    ref_layout_approved: dict[str, str] = dataclasses.field(
        default_factory=lambda: dict(REF_LAYOUT_APPROVED))

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
    #: The reader conformance check (run_oracle when None).
    oracle: Callable[..., dict] | None = None


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


# --- item 4: structural marshal comparison (Human Acceptance Decision 03) -------------------
#
# A reader for the marshal stream of a CPython 3.14.10 .pyc, written against
# Python/marshal.c, Objects/codeobject.c and Include/internal/pycore_code.h at tag
# v3.14.10 (PYC_MAGIC_NUMBER 3627, header bytes 2b0e0d0a, Py_MARSHAL_VERSION 5).
# It turns the bytes into inert tuples: nothing is unmarshalled, compiled or run
# (no marshal.loads, pickle, eval or exec), and it is only used on a .pyc that is
# not byte-identical to its recompilation.
#
# Only what a compiled module can contain is accepted: None, True, False,
# Ellipsis, int (TYPE_INT, TYPE_LONG), float and complex (binary), bytes, the six
# str encodings, tuple, frozenset, slice and code. Anything else - an unknown tag,
# TYPE_NULL, a list, dict or set, FLAG_REF on a singleton or on TYPE_REF, a
# TYPE_REF to an object still being read (CPython would accept a cycle through a
# tuple; the writer cannot produce one) - is rejected.
#
# Every object becomes a node (tag, scalars, children) in a table shared by the
# two streams compared, so two nodes have the same id exactly when their resolved
# trees are equal; TYPE_REF is resolved to the node it designates in its own
# stream. FLAG_REF and the reference indices are the only information dropped:
# the tag keeps the rest (interned or not, ASCII, short or long string, small or
# large tuple), floats keep their 8 bytes and frozensets their stream order.

MARSHAL_FORMAT = {"python": PYTHON_VERSION, "pyc_magic_number": 3627, "magic": "2b0e0d0a",
                  "marshal_version": 5,
                  "source": "CPython v3.14.10: Python/marshal.c, Objects/codeobject.c"}

BYTE_IDENTICAL = "BYTE_IDENTICAL"
REF_LAYOUT_EQUIVALENT = "MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF"
STRUCTURAL_MISMATCH = "STRUCTURAL_MISMATCH"
REJECTED = "REJECTED"

#: The only files to which MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF may apply (Decision 03):
#: archive path -> sha256 of the installed file the decision was taken on. Another
#: file, or another content, is REJECTED even if it is equivalent.
REF_LAYOUT_APPROVED: dict[str, str] = {
    "python/lib/python3.14/__pycache__/argparse.cpython-314.pyc":
        "456c9a04733335ac851b27c181b164ebf53fb802467cc8c4aafef2fb073bcc9f",
    "python/lib/python3.14/__pycache__/inspect.cpython-314.pyc":
        "6e934df79a444dc62ca886c4a5e7b04b769dc5358a30e94bd5c58d4f8c772b32",
    "python/lib/python3.14/__pycache__/typing.cpython-314.pyc":
        "83aba4fe3e19880f888a057b72c1bcb3da75b0ad9ff3d657daa76fe157e63573",
    "python/lib/python3.14/__pycache__/weakref.cpython-314.pyc":
        "209a12ba13956f14df6f1935608f01439a464b202acd6a6bf5720a26dc7e1a33",
}


@dataclasses.dataclass(frozen=True)
class MarshalLimits:
    """Bounds of the reader. The four files of Decision 03 are 27,707-171,917 bytes."""

    input_bytes: int = 8 << 20  # over 45 times the largest of the four
    objects: int = 1_000_000  # every object is at least one byte of input
    # Nesting while reading (CPython's own limit is 2000) and of the resolved tree (a
    # TYPE_REF can graft a subtree). The reader and the tree walks are recursive, with
    # at most five Python frames per level (a code record: read_object, _object,
    # _value, _code, _children), so both stay under the default recursion limit of
    # 1000; a RecursionError is a rejection anyway.
    depth: int = 100
    height: int = 150
    container: int = 1_000_000  # items of one tuple or frozenset
    string: int = 8 << 20  # bytes of one str or bytes object
    refs: int = 1_000_000  # FLAG_REF entries
    long_digits: int = 2_000  # 15-bit digits of one TYPE_LONG (30,000 bits)


MARSHAL_LIMITS = MarshalLimits()
FLAG_REF = 0x80
SIZE32_MAX = 0x7FFFFFFF
_SINGLETONS = frozenset("NFT.")
_ASCII_TAGS = frozenset("aAzZ")
STRING_TAGS = frozenset("aAzZut")
_TUPLE_TAGS = frozenset("()")
_SUPPORTED = frozenset("ilgys>:c") | STRING_TAGS | _TUPLE_TAGS
CODE_SCALARS = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_stacksize",
                "co_flags", "co_firstlineno")
CODE_CHILDREN = ("co_code", "co_consts", "co_names", "co_localsplusnames",
                 "co_localspluskinds", "co_filename", "co_name", "co_qualname", "co_linetable",
                 "co_exceptiontable")
CO_VARARGS, CO_VARKEYWORDS = 0x04, 0x08
CO_FAST_LOCAL, CO_FAST_CELL, CO_FAST_FREE = 0x20, 0x40, 0x80


class MarshalRejected(Exception):
    def __init__(self, reason: str, offset: int) -> None:
        super().__init__(f"{reason} at offset {offset}")
        self.reason, self.offset = reason, offset


Node = tuple  # (tag, scalars, children): children are node ids


class NodeTable:
    """Hash-consed nodes: equal resolved trees get the same id."""

    def __init__(self, limits: MarshalLimits = MARSHAL_LIMITS) -> None:
        self.limits = limits
        self.ids: dict[Node, int] = {}
        self.nodes: list[Node] = []
        self.heights: list[int] = []
        #: tuple nodes already found to hold only str (see MarshalReader._validate_code)
        self.string_tuples: set[int] = set()

    def intern(self, tag: str, scalars: tuple, children: tuple, offset: int) -> int:
        key = (tag, scalars, children)
        found = self.ids.get(key)
        if found is not None:
            return found
        height = 1 + max((self.heights[child] for child in children), default=0)
        if height > self.limits.height:
            raise MarshalRejected("resolved structure nested too deeply", offset)
        self.ids[key] = len(self.nodes)
        self.nodes.append(key)
        self.heights.append(height)
        return len(self.nodes) - 1


class MarshalReader:
    def __init__(self, data: bytes, start: int, table: NodeTable) -> None:
        self.data, self.pos, self.table, self.limits = data, start, table, table.limits
        self.refs: list[int | None] = []
        self.depth = self.max_depth = self.objects = self.type_refs = 0
        self.tags: dict[str, int] = {}

    def _take(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.pos:
            raise MarshalRejected("truncated stream", self.pos)
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def _long(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def _size(self, size: int, what: str, limit: int, offset: int) -> int:
        if size < 0 or size > SIZE32_MAX:
            raise MarshalRejected(f"{what} size out of range", offset)
        if size > limit:
            raise MarshalRejected(f"{what} size {size} over the limit {limit}", offset)
        if size > len(self.data) - self.pos:  # every item takes at least one byte
            raise MarshalRejected(f"{what} size {size} beyond the end of the stream", offset)
        return size

    def read_object(self) -> int:
        offset = self.pos
        code = self._take(1)[0]
        self.objects += 1
        if self.objects > self.limits.objects:
            raise MarshalRejected("too many objects", offset)
        self.depth += 1
        if self.depth > self.limits.depth:
            raise MarshalRejected("nested too deeply", offset)
        self.max_depth = max(self.max_depth, self.depth)
        try:
            return self._object(code, offset)
        finally:
            self.depth -= 1

    def _object(self, code: int, offset: int) -> int:
        flag, tag = code & FLAG_REF, chr(code & ~FLAG_REF)
        self.tags[tag] = self.tags.get(tag, 0) + 1
        if tag == "r":
            if flag:
                raise MarshalRejected("FLAG_REF on TYPE_REF", offset)
            self.type_refs += 1
            index = self._long()
            if index < 0 or index >= len(self.refs):
                raise MarshalRejected(f"invalid reference {index}", offset)
            target = self.refs[index]
            if target is None:
                raise MarshalRejected(f"reference {index} to an object still being read", offset)
            return target
        if tag in _SINGLETONS:
            if flag:
                raise MarshalRejected(f"FLAG_REF on {tag!r}", offset)
            return self.table.intern(tag, (), (), offset)
        if tag not in _SUPPORTED:
            raise MarshalRejected(f"unsupported marshal type 0x{code & ~FLAG_REF:02x}", offset)
        slot = None
        if flag:  # the index is reserved before the children are read, as CPython does
            if len(self.refs) >= self.limits.refs:
                raise MarshalRejected("too many references", offset)
            slot = len(self.refs)
            self.refs.append(None)
        node = self._value(tag, offset)
        if slot is not None:
            self.refs[slot] = node
        return node

    def _children(self, count: int) -> tuple:
        items = []
        for _ in range(count):
            items.append(self.read_object())
        return tuple(items)

    def _value(self, tag: str, offset: int) -> int:
        limits = self.limits
        scalars: tuple = ()
        children: tuple = ()
        if tag == "i":
            scalars = (self._long(),)
        elif tag == "l":
            count = self._long()
            if count == 0 or abs(count) > limits.long_digits:
                raise MarshalRejected(f"long size {count} out of range", offset)
            digits = struct.unpack(f"<{abs(count)}H", self._take(2 * abs(count)))
            if any(digit >= 1 << 15 for digit in digits):
                raise MarshalRejected("digit out of range in long", offset)
            if digits[-1] == 0:
                raise MarshalRejected("unnormalized long", offset)
            value = sum(digit << (15 * i) for i, digit in enumerate(digits))
            scalars = (-value if count < 0 else value,)
        elif tag == "g":
            scalars = (self._take(8),)
        elif tag == "y":
            scalars = (self._take(16),)
        elif tag == "s":
            scalars = (self._take(self._size(self._long(), "bytes", limits.string, offset)),)
        elif tag in STRING_TAGS:
            size = self._take(1)[0] if tag in "zZ" else self._long()
            payload = self._take(self._size(size, "str", limits.string, offset))
            if tag in _ASCII_TAGS and any(byte >= 0x80 for byte in payload):
                raise MarshalRejected("non-ASCII data in an ASCII string", offset)
            if tag in "ut":
                try:
                    payload.decode("utf-8", "surrogatepass")
                except UnicodeDecodeError:
                    raise MarshalRejected("invalid UTF-8 in a string", offset) from None
            scalars = (payload,)
        elif tag in _TUPLE_TAGS or tag == ">":
            size = self._take(1)[0] if tag == ")" else self._long()
            children = self._children(self._size(size, "container", limits.container, offset))
        elif tag == ":":
            children = self._children(3)
        else:  # "c"
            return self._code(offset)
        return self.table.intern(tag, scalars, children, offset)

    def _code(self, offset: int) -> int:
        head = tuple(self._long() for _ in range(5))  # argcount ... flags
        before = self._children(8)  # co_code ... co_qualname
        firstlineno = self._long()
        after = self._children(2)  # co_linetable, co_exceptiontable
        scalars, children = head + (firstlineno,), before + after
        self._validate_code(scalars, children, offset)
        return self.table.intern("c", scalars, children, offset)

    def _validate_code(self, scalars: tuple, children: tuple, offset: int) -> None:
        """The checks of _PyCode_Validate() and the field types the code constructor needs."""
        argcount, posonly, kwonly, stacksize, flags, _ = scalars
        nodes = self.table.nodes
        field = dict(zip(CODE_CHILDREN, (nodes[child] for child in children)))

        def fail(problem: str) -> None:
            raise MarshalRejected(f"invalid code record: {problem}", offset)

        if argcount < posonly or posonly < 0 or kwonly < 0 or stacksize < 0 or flags < 0:
            fail("negative or inconsistent counts")
        for name in ("co_code", "co_localspluskinds", "co_linetable", "co_exceptiontable"):
            if field[name][0] != "s":
                fail(f"{name} is not bytes")
        for name in ("co_filename", "co_name", "co_qualname"):
            if field[name][0] not in STRING_TAGS:
                fail(f"{name} is not a str")
        for name in ("co_consts", "co_names", "co_localsplusnames"):
            if field[name][0] not in _TUPLE_TAGS:
                fail(f"{name} is not a tuple")
        for name in ("co_names", "co_localsplusnames"):
            # Each tuple node is checked once: a shared names tuple must not make the
            # validation quadratic (independent review finding).
            tuple_id = children[CODE_CHILDREN.index(name)]
            if tuple_id in self.table.string_tuples:
                continue
            if any(nodes[item][0] not in STRING_TAGS for item in field[name][2]):
                fail(f"{name} holds a non-str")
            self.table.string_tuples.add(tuple_id)
        if len(field["co_code"][1][0]) % 2:
            fail("co_code has an odd length")
        kinds = field["co_localspluskinds"][1][0]
        if len(kinds) != len(field["co_localsplusnames"][2]):
            fail("co_localsplusnames and co_localspluskinds differ in length")
        locals_count = sum(1 for kind in kinds if kind & CO_FAST_LOCAL)
        if locals_count < argcount + kwonly + bool(flags & CO_VARARGS) \
                + bool(flags & CO_VARKEYWORDS):
            fail("co_varnames is too small")


@dataclasses.dataclass
class ParsedPyc:
    header: bytes
    root: int
    stats: dict[str, Any]


def parse_marshal(data: bytes, table: NodeTable, start: int = 0) -> tuple[int, MarshalReader]:
    """One complete marshal object filling data[start:] (trailing bytes are rejected)."""
    if len(data) > table.limits.input_bytes:
        raise MarshalRejected(f"input of {len(data)} bytes over the limit", 0)
    reader = MarshalReader(data, start, table)
    root = reader.read_object()
    if reader.pos != len(data):
        raise MarshalRejected(f"{len(data) - reader.pos} trailing bytes", reader.pos)
    return root, reader


def parse_pyc(data: bytes, table: NodeTable) -> ParsedPyc:
    if len(data) < 16:
        raise MarshalRejected("truncated header", len(data))
    if data[:4].hex() != MARSHAL_FORMAT["magic"]:
        raise MarshalRejected(f"incorrect magic number {data[:4].hex()}", 0)
    root, reader = parse_marshal(data, table, 16)
    if table.nodes[root][0] != "c":
        raise MarshalRejected("the top-level object is not a code record", 16)
    return ParsedPyc(data[:16], root, {
        "objects": reader.objects, "flag_refs": len(reader.refs), "type_refs": reader.type_refs,
        "max_depth": reader.max_depth, "tags": dict(sorted(reader.tags.items()))})


def _child_label(tag: str, path: str, index: int) -> str:
    if tag == "c":
        return f"{path}.{CODE_CHILDREN[index]}"
    if tag == ":":
        return f"{path}.{('start', 'stop', 'step')[index]}"
    return f"{path}{{{index}}}" if tag == ">" else f"{path}[{index}]"


def _summary(node: Node) -> dict:
    tag, scalars, children = node
    shown: list[Any] = []
    for value in scalars:
        if isinstance(value, bytes):
            text = value.decode("utf-8", "replace") if tag in STRING_TAGS else value.hex()
            shown.append(text[:80] + ("..." if len(text) > 80 else ""))
        else:
            shown.append(value)
    return {"tag": tag, "scalars": shown, "children": len(children)}


def diff_nodes(table: NodeTable, a: int, b: int, limit: int = 20,
               max_pairs: int = 200_000) -> list[dict]:
    """Where two resolved trees differ (diagnostics only). Pairs are visited once, and at
    most `max_pairs` of them: two hostile DAGs could align into exponentially many."""
    found: list[dict] = []
    seen: set[tuple[int, int]] = set()

    def walk(x: int, y: int, path: str) -> None:
        if x == y or (x, y) in seen or len(found) >= limit:
            return
        if len(seen) >= max_pairs:
            found.append({"path": path, "kind": "truncated",
                          "note": f"stopped after {max_pairs} node pairs"})
            return
        seen.add((x, y))
        (tx, sx, cx), (ty, sy, cy) = table.nodes[x], table.nodes[y]
        if tx != ty or len(cx) != len(cy):
            kind = "tag" if len(cx) == len(cy) and sx == sy else "structure"
            found.append({"path": path, "kind": kind, "installed": _summary(table.nodes[x]),
                          "recompiled": _summary(table.nodes[y])})
            return
        if sx != sy:
            fields = ([name for name, p, q in zip(CODE_SCALARS, sx, sy) if p != q]
                      if tx == "c" else ["value"])
            found.append({"path": path, "kind": "value", "fields": fields,
                          "installed": _summary(table.nodes[x]),
                          "recompiled": _summary(table.nodes[y])})
        for index, (p, q) in enumerate(zip(cx, cy)):
            walk(p, q, _child_label(tx, path, index))

    walk(a, b, "<module>")
    return found


def without_interned_flag(table: NodeTable, root: int, into: NodeTable,
                          memo: dict[int, int]) -> int:
    """Diagnostics only: the tree with interned and non-interned string tags merged."""
    if root in memo:
        return memo[root]
    tag, scalars, children = table.nodes[root]
    mapped = tuple(without_interned_flag(table, child, into, memo) for child in children)
    memo[root] = into.intern({"Z": "z", "A": "a", "t": "u"}.get(tag, tag), scalars, mapped, 0)
    return memo[root]


def compare_structure(installed: bytes, recompiled: bytes,
                      limits: MarshalLimits = MARSHAL_LIMITS) -> dict:
    """Parse both .pyc independently and compare their resolved trees."""
    table = NodeTable(limits)
    parsed = {}
    for side, data in (("installed", installed), ("recompiled", recompiled)):
        try:
            parsed[side] = parse_pyc(data, table)
        except MarshalRejected as exc:
            return {"parsed": False, "rejected": side, "reason": exc.reason,
                    "offset": exc.offset}
        except RecursionError:
            return {"parsed": False, "rejected": side, "reason": "recursion limit reached",
                    "offset": None}
    a, b = parsed["installed"], parsed["recompiled"]
    result: dict[str, Any] = {
        "parsed": True, "header_identical": a.header == b.header,
        "equivalent": a.header == b.header and a.root == b.root,
        "installed": a.stats, "recompiled": b.stats}
    if a.root != b.root:  # diagnostics only: the classification is already decided
        try:
            result["differences"] = diff_nodes(table, a.root, b.root)
            merged, memo = NodeTable(limits), {}  # type: NodeTable, dict[int, int]
            result["equal_without_interned_flag"] = (
                without_interned_flag(table, a.root, merged, memo)
                == without_interned_flag(table, b.root, merged, memo))
        except RecursionError:
            result["differences"] = "recursion limit reached while describing the difference"
    return result


def classify_pyc(name: str, installed: bytes, recompiled: bytes, approved: dict[str, str],
                 reader_verified: bool | None) -> dict:
    """Decision 03: BYTE_IDENTICAL, MARSHAL_EQUIVALENT_REF_LAYOUT_DIFF, STRUCTURAL_MISMATCH
    or REJECTED. `reader_verified` is the reader conformance result (None: not run)."""
    if installed == recompiled:
        return {"classification": BYTE_IDENTICAL}
    structure = compare_structure(installed, recompiled)
    if not structure["parsed"]:
        return {"classification": REJECTED, "reason": "not parsable", "structure": structure}
    if not structure["equivalent"]:
        reason = "header differs" if not structure["header_identical"] else "code differs"
        return {"classification": STRUCTURAL_MISMATCH, "reason": reason, "structure": structure}
    if approved.get(name) != hashlib.sha256(installed).hexdigest():
        return {"classification": REJECTED, "reason": "equivalent, but not approved by Decision 03",
                "structure": structure}
    if reader_verified is not True:
        return {"classification": REJECTED, "reason": "reader conformance not established",
                "structure": structure}
    return {"classification": REF_LAYOUT_EQUIVALENT,
            "reason": "header identical; resolved trees identical; only FLAG_REF / TYPE_REF differ",
            "structure": structure}


# The reader is checked against the 3.14.10 writer itself: the isolated interpreter
# compiles this synthetic module (it is not run), marshals it, then marshals it
# again while holding extra references to its constants, which changes FLAG_REF
# and the TYPE_REF indices only. The reader must recover every code field from
# both, find them equivalent, and find a different file name or constant different.
ORACLE_SOURCE = '''\
"""Oracle module: compiled, never run."""
import os as _os
SHARED = "shared constant, not an identifier!"
WIDE = "été 中"
LONG = """%s"""
BIG = 1 << 100
NEG = -(1 << 70)
VALUES = (1, 2.5, -0.0, 3j, b"\\x00\\xff", None, True, False, ..., (1, (2, "z")))
def function(a, b=SHARED, /, c=1, *args, d, e=2, **kwargs):
    total = a + c
    def inner(x):
        nonlocal total
        total += x
        return total, SHARED
    try:
        return inner(d)[0:2:1], a in {1, 2, 3}, "shared constant, not an identifier!"
    except (KeyError, ValueError) as exc:
        raise RuntimeError(exc) from None
    finally:
        del total
class Klass(dict):
    attribute: int = 3
    def method(self, *, flag=False):
        return [n * 2 for n in range(4) if n] + [lambda: self]
''' % ("y" * 300)
ORACLE_VARIANT = ORACLE_SOURCE.replace("e=2", "e=3")
#: Marshal types ORACLE_SOURCE produces: code, small tuple, interned / plain short and
#: long ASCII, non-ASCII str, int, long, float, complex, bytes, frozenset ({1, 2, 3}),
#: slice ([0:2:1], folded since 3.14), the singletons and TYPE_REF.
ORACLE_TAGS = frozenset("c)Zzauilgys>:NTF.r")


def describe_live(obj: Any) -> Any:
    """Run by the isolated interpreter: a JSON description of a compiled constant."""
    import types

    if obj is None or obj is True or obj is False or obj is Ellipsis:
        return ["const", repr(obj)]
    if isinstance(obj, types.CodeType):
        return ["code", {
            "argcount": obj.co_argcount, "posonlyargcount": obj.co_posonlyargcount,
            "kwonlyargcount": obj.co_kwonlyargcount, "stacksize": obj.co_stacksize,
            "flags": obj.co_flags, "code": obj.co_code.hex(),
            "consts": [describe_live(item) for item in obj.co_consts],
            "names": list(obj.co_names), "varnames": list(obj.co_varnames),
            "cellvars": list(obj.co_cellvars), "freevars": list(obj.co_freevars),
            "filename": obj.co_filename, "name": obj.co_name, "qualname": obj.co_qualname,
            "firstlineno": obj.co_firstlineno, "linetable": obj.co_linetable.hex(),
            "exceptiontable": obj.co_exceptiontable.hex()}]
    if type(obj) is int:
        return ["int", str(obj)]
    if type(obj) is float:
        return ["float", struct.pack("<d", obj).hex()]
    if type(obj) is complex:
        return ["complex", struct.pack("<dd", obj.real, obj.imag).hex()]
    if type(obj) is bytes:
        return ["bytes", obj.hex()]
    if type(obj) is str:
        return ["str", obj]
    if type(obj) is tuple:
        return ["tuple", [describe_live(item) for item in obj]]
    if type(obj) is frozenset:
        return ["frozenset", sorted((describe_live(item) for item in obj), key=json.dumps)]
    if type(obj) is slice:
        return ["slice", [describe_live(obj.start), describe_live(obj.stop),
                          describe_live(obj.step)]]
    raise TypeError(f"unexpected constant type {type(obj).__name__}")


def describe_node(table: NodeTable, node: int) -> Any:
    """The same description, from the reader's tree."""
    tag, scalars, children = table.nodes[node]
    if tag in _SINGLETONS:
        return ["const", {"N": "None", "T": "True", "F": "False", ".": "Ellipsis"}[tag]]
    if tag in "il":
        return ["int", str(scalars[0])]
    if tag == "g":
        return ["float", scalars[0].hex()]
    if tag == "y":
        return ["complex", scalars[0].hex()]
    if tag == "s":
        return ["bytes", scalars[0].hex()]
    if tag in STRING_TAGS:
        return ["str", scalars[0].decode("utf-8", "surrogatepass")]
    if tag in _TUPLE_TAGS:
        return ["tuple", [describe_node(table, child) for child in children]]
    if tag == ">":
        return ["frozenset", sorted((describe_node(table, child) for child in children),
                                    key=json.dumps)]
    if tag == ":":
        return ["slice", [describe_node(table, child) for child in children]]
    field = dict(zip(CODE_CHILDREN, children))

    def strings(name: str) -> list[str]:
        return [describe_node(table, child)[1] for child in table.nodes[field[name]][2]]

    names = strings("co_localsplusnames")
    kinds = table.nodes[field["co_localspluskinds"]][1][0]

    def text(name: str) -> str:
        return describe_node(table, field[name])[1]

    def raw(name: str) -> str:
        return table.nodes[field[name]][1][0].hex()

    argcount, posonly, kwonly, stacksize, flags, firstlineno = scalars
    return ["code", {
        "argcount": argcount, "posonlyargcount": posonly, "kwonlyargcount": kwonly,
        "stacksize": stacksize, "flags": flags, "code": raw("co_code"),
        "consts": describe_node(table, field["co_consts"])[1],
        "names": strings("co_names"),
        "varnames": [n for n, k in zip(names, kinds) if k & CO_FAST_LOCAL],
        "cellvars": [n for n, k in zip(names, kinds) if k & CO_FAST_CELL],
        "freevars": [n for n, k in zip(names, kinds) if k & CO_FAST_FREE],
        "filename": text("co_filename"), "name": text("co_name"), "qualname": text("co_qualname"),
        "firstlineno": firstlineno, "linetable": raw("co_linetable"),
        "exceptiontable": raw("co_exceptiontable")}]


def oracle_worker(out_dir: str) -> int:
    """Run by the isolated interpreter: marshal the synthetic module (never run) into out_dir."""
    import importlib.util
    import marshal
    import types

    out = Path(os.path.realpath(out_dir))
    header = importlib.util.MAGIC_NUMBER + bytes(12)
    base = compile(ORACLE_SOURCE, "/oracle/module.py", "exec", dont_inherit=True, optimize=0)
    renamed = compile(ORACLE_SOURCE, "/oracle/other.py", "exec", dont_inherit=True, optimize=0)
    variant = compile(ORACLE_VARIANT, "/oracle/module.py", "exec", dont_inherit=True, optimize=0)
    streams = {"base": marshal.dumps(base), "renamed": marshal.dumps(renamed),
               "variant": marshal.dumps(variant)}
    held = []  # extra references: FLAG_REF on every constant that had a single one

    def hold(code: types.CodeType) -> None:
        for item in code.co_consts:
            held.append(item)
            if isinstance(item, types.CodeType):
                hold(item)
            elif isinstance(item, tuple):
                held.extend(item)

    hold(base)
    streams["base_extra_refs"] = marshal.dumps(base)
    for name, data in streams.items():
        (out / f"oracle-{name}.pyc").write_bytes(header + data)
    descriptions = {"base": describe_live(base), "renamed": describe_live(renamed),
                    "variant": describe_live(variant)}
    (out / "oracle.json").write_text(json.dumps(descriptions), encoding="utf-8")
    return 0


def verify_reader(out_dir: Path) -> dict:
    """Check the reader on the oracle output of the 3.14.10 writer (see ORACLE_SOURCE)."""
    expected = json.loads((out_dir / "oracle.json").read_text(encoding="utf-8"))
    data = {name: (out_dir / f"oracle-{name}.pyc").read_bytes()
            for name in ("base", "base_extra_refs", "renamed", "variant")}
    table = NodeTable()
    try:
        parsed = {name: parse_pyc(value, table) for name, value in data.items()}
    except MarshalRejected as exc:
        return {"status": FAIL, "reason": f"oracle stream rejected: {exc}"}
    checks = {
        "fields_base": describe_node(table, parsed["base"].root) == expected["base"],
        "fields_extra_refs": describe_node(table, parsed["base_extra_refs"].root)
        == expected["base"],
        "fields_renamed": describe_node(table, parsed["renamed"].root) == expected["renamed"],
        "fields_variant": describe_node(table, parsed["variant"].root) == expected["variant"],
        "layout_differs": data["base"] != data["base_extra_refs"]
        and parsed["base"].stats["flag_refs"] < parsed["base_extra_refs"].stats["flag_refs"],
        "type_refs_present": parsed["base"].stats["type_refs"] > 0,
        # Not vacuous: the oracle streams must contain every type the source is written
        # to produce (independent review finding).
        "types_covered": ORACLE_TAGS <= set(parsed["base"].stats["tags"]),
        "extra_refs_equivalent": parsed["base"].root == parsed["base_extra_refs"].root,
        "renamed_different": parsed["base"].root != parsed["renamed"].root,
        "variant_different": parsed["base"].root != parsed["variant"].root,
    }
    return {"status": PASS if all(checks.values()) else FAIL, "checks": checks,
            "stats": {name: value.stats for name, value in parsed.items()},
            "sha256": {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}}


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
        identical = 0
        pairs = []
        for job in jobs:
            installed = (settings.runtime_root / job.installed[len(ARCHIVE_ROOT) + 1:]).read_bytes()
            produced = Path(job.cfile).read_bytes() if Path(job.cfile).is_file() else b""
            if installed == produced:
                identical += 1
            else:
                pairs.append((job, installed, produced))
        # The reader is only needed, and only checked, when a .pyc is not byte-identical.
        oracle = {"status": NOT_RUN, "reason": "every .pyc is byte-identical"}
        if pairs:
            oracle = (ctx.oracle or run_oracle)(ctx, isolation, python, work)
        verified = None if oracle["status"] == NOT_RUN else oracle["status"] == PASS
        mismatches = []
        for job, installed, produced in pairs:
            row = {
                "pyc": job.installed, "installed_sha256": hashlib.sha256(installed).hexdigest(),
                "recompiled_sha256": hashlib.sha256(produced).hexdigest(),
                "difference": first_difference(installed, produced),
                "conditions": {"optimize": job.optimize, "dfile": job.dfile,
                               "invalidation_mode": "TIMESTAMP"},
                "flags": struct.unpack("<I", installed[4:8])[0] if len(installed) >= 8 else None,
            }
            row.update(classify_pyc(job.installed, installed, produced,
                                    settings.ref_layout_approved, verified))
            mismatches.append(row)
        counts = {"total": len(jobs), BYTE_IDENTICAL: identical, REF_LAYOUT_EQUIVALENT: 0,
                  STRUCTURAL_MISMATCH: 0, REJECTED: 0}
        for row in mismatches:
            counts[row["classification"]] += 1
        return {"probe": probe, "interpreter": report, "jobs": len(jobs), "identical": identical,
                "classification": counts, "mismatches": mismatches,
                "reader": {"format": MARSHAL_FORMAT, "limits": dataclasses.asdict(MARSHAL_LIMITS),
                           "conformance": oracle}}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_oracle(ctx: Context, isolation: Isolation, python: Path, work: Path) -> dict:
    """Run oracle_worker in the isolated interpreter and check the reader on its output."""
    out = work / "oracle"
    out.mkdir()
    done = isolation.run(ctx.run, [str(python), "-I", "-B", "-S",
                                   str(work / "verify_cutover_gate.py"), "--oracle-worker",
                                   str(out)])
    if done.returncode != 0 or not (out / "oracle.json").is_file():
        return {"status": FAIL,
                "reason": f"oracle worker failed ({done.returncode}): {(done.stderr or '')[-300:]}"}
    try:
        return verify_reader(out)
    except Exception as exc:  # noqa: BLE001 - any failure of the check is a FAIL, never a PASS
        return {"status": FAIL, "reason": f"{type(exc).__name__}: {exc}"}


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
    counts = recompiled["classification"]
    accepted = counts[BYTE_IDENTICAL] + counts[REF_LAYOUT_EQUIVALENT]
    ok = identity and not interpreter["errors"] and counts[STRUCTURAL_MISMATCH] == 0 \
        and counts[REJECTED] == 0 and accepted == counts["total"] == recompiled["jobs"]
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
    if argv[:1] == ["--oracle-worker"] and len(argv) == 2:
        return oracle_worker(argv[1])
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
