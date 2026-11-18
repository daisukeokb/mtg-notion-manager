"""Phase 3H-R7: production cutover authorization readiness (a DISABLED, dry-run-only component).

Three read-only checks that a human needs before deciding on the Python 3.14 cutover. None of
them launches anything:

* ``verify_bootstrap`` (D-1, local runtime scope): before any Python of the runtime under test
  is started, check the runtime, the venv and the application source against a runtime
  manifest whose own sha256 must come from an independent, human-approved record. The
  manifest is a complete inventory of the three trees: every file is hashed, every symlink
  target compared (a symlink must resolve to a regular file inside its tree), and any entry
  the manifest does not list fails. ``pyvenv.cfg`` ``home`` must be the verified runtime's
  ``bin`` (getpath finds the stdlib from it). Every directory that will be on ``sys.path``
  (the stdlib, ``lib-dynload``, site-packages, and each ``.pth`` path line) must be a real
  directory inside a tree and is scanned for ``sitecustomize`` / ``usercustomize`` (``-I``
  does not disable them); a ``.pth`` import line, a ``.pth`` path outside the three trees, an
  unknown runtime version, an unexpected interpreter
  path, injected environment variables and any change of the trees during the run (they are
  walked again at the end) fail. A missing provenance record or a missing expected manifest
  hash is UNVERIFIED, never PASS: a matching sha256 alone does not make a third-party build
  trustworthy, and provenance PASS means only that the human-recorded attestation evidence is
  present and consistent.
* ``plan_launch`` (C-2 / C-3): the launch the production launcher *would* use - an explicit
  interpreter, ``-I -B -m mtg_notion_manager.cli``, an allowlisted environment, python-dotenv's
  automatic ``.env`` discovery disabled (``PYTHON_DOTENV_DISABLED=1``) and the credentials
  taken only from one explicit, owner-only config file. Inherited ``NOTION_*`` / ``PYTHON*`` /
  ``DYLD_*`` / ``LD_*`` / ``PIP_*`` variables are refused, not silently dropped. Secret values
  are held in ``Secret`` and never appear in a message, a report or a log. The plan is never
  executed: ``EXECUTION_ENABLED`` is False, every plan carries ``executable = False``, and this
  module has no exec, subprocess or socket code at all (tested).
* ``assess_rollback`` / ``simulate_cutover`` / ``simulate_rollback``: the rollback decision on
  an isolated model of the two runtimes. Local rollback (going back to the legacy launcher)
  is kept apart from external state: any CLI invocation after the cutover that could have
  written to Notion makes the rollback conditional (the external state is not restored by it).

Run with the system interpreter, not with the runtime under test::

    /usr/bin/python3 -I -B scripts/cutover_readiness.py bootstrap --manifest M --manifest-sha256 H \
        --python P
    /usr/bin/python3 -I -B scripts/cutover_readiness.py launch-plan --manifest M \
        --manifest-sha256 H --python P --config C -- <command> [args]

Phase 3H-R7 ran these only against synthetic fixtures. PRODUCTION CUTOVER IS NOT AUTHORIZED.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PASS, FAIL, BLOCKED, UNVERIFIED = "PASS", "FAIL", "BLOCKED", "UNVERIFIED"
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The launcher never executes its plan in Phase 3H-R7 (no code path does; tested). Every
#: plan reports ``executable`` from this constant.
EXECUTION_ENABLED = False
EXECUTION_NOTICE = ("DISABLED: Phase 3H-R7 builds and checks the plan only. "
                    "PRODUCTION CUTOVER NOT AUTHORIZED.")

MANIFEST_SCHEMA = "mtg-notion-manager/runtime-manifest/2"
#: Runtime versions this repository has verified (the marshal reader of
#: scripts/verify_cutover_gate.py is tied to 3.14.10, too).
KNOWN_VERSIONS = ("3.14.10",)
#: The archive verified by the Cutover Safety Gate (docs/local-python-runtime.md; tested to
#: equal scripts/verify_cutover_gate.py ARCHIVE_SHA256).
VERIFIED_ARCHIVE_SHA256 = "30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4"
FILE_LIMIT = 256 << 20
CONFIG_LIMIT = 64 << 10
TREES = ("runtime", "venv", "app")
_SHA256 = re.compile(r"[0-9a-f]{64}")
SYMLINK = "symlink:"

#: Inherited variables that must not be present when the launcher plans a launch, and that
#: the bootstrap check reports as injected. Refused, not dropped: a contaminated shell is a
#: reason to stop.
FORBIDDEN_PREFIXES = ("NOTION_", "PYTHON", "DYLD_", "LD_", "PIP_", "__PYVENV_LAUNCHER__")
#: The same allowlist as scripts/verify_cutover_gate.py PYTHON_ENVIRONMENT_KEEP (tested).
ENVIRONMENT_KEEP = ("HOME", "USER", "LOGNAME", "TMPDIR", "TZ", "LANG", "LC_ALL", "LC_CTYPE",
                    "LC_COLLATE", "LC_MESSAGES")
LAUNCH_PATH = ("/usr/bin", "/bin")
LAUNCH_ARGS = ("-I", "-B", "-m", "mtg_notion_manager.cli")

CONFIG_REQUIRED = ("NOTION_API_KEY", "NOTION_COMMANDER_DATA_SOURCE_ID")
CONFIG_OPTIONAL = ("NOTION_CARD_DATA_SOURCE_ID",)
_DATA_SOURCE_ID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}"
                             r"-?[0-9a-fA-F]{12}$")
_CONFIG_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]+$")

#: The options of src/mtg_notion_manager/cli.py, per command (tested against the Click
#: commands): options that take a value, and flags. ``import`` writes unless --dry-run; the
#: others write only with an --apply* flag; the read-only ones never write.
VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "apply-dedupe-plan": frozenset({"--audit-report", "--classification", "--limit", "--offset",
                                    "--output-dir"}),
    "apply-price-link-dedupe": frozenset({"--limit", "--manual-representative-page-id",
                                          "--offset", "--output-dir", "--scope",
                                          "--targets-report"}),
    "apply-single-title-update": frozenset({"--approval-digest", "--expected-count",
                                            "--manifest", "--max-updates", "--output-dir"}),
    "audit-duplicates": frozenset({"--card-name", "--output-dir"}),
    "dedupe-cards": frozenset({"--card-name", "--representative-page-id"}),
    "doctor": frozenset(),
    "import": frozenset({"--deck-name"}),
    "import-article": frozenset({"--confirmed-card-map", "--deck-page-map", "--exclude-deck",
                                 "--include-deck", "--output-dir",
                                 "--pending-manifest-output"}),
    "import-cards": frozenset({"--confirmed-card-map", "--deck-name", "--deck-page-id"}),
    "plan-title-updates": frozenset({"--expected-count", "--manifest", "--output-dir"}),
    "review-duplicate-conflicts": frozenset({"--card-name", "--category", "--output-dir"}),
    "verify-import": frozenset({"--confirmed-card-map", "--deck-page-map", "--include-deck",
                                "--output-dir"}),
}
FLAG_OPTIONS: dict[str, frozenset[str]] = {
    "apply-dedupe-plan": frozenset({"--apply", "--dry-run", "--error-json"}),
    "apply-price-link-dedupe": frozenset({"--apply", "--dry-run", "--error-json"}),
    "apply-single-title-update": frozenset({"--apply", "--error-json"}),
    "audit-duplicates": frozenset({"--error-json"}),
    "dedupe-cards": frozenset({"--apply", "--apply-all", "--apply-schema", "--dry-run",
                               "--error-json", "--yes"}),
    "doctor": frozenset({"--error-json"}),
    "import": frozenset({"--dry-run", "--error-json"}),
    "import-article": frozenset({"--apply", "--detail", "--dry-run", "--error-json",
                                 "--no-detail"}),
    "import-cards": frozenset({"--allow-count-mismatch", "--apply", "--detail", "--dry-run",
                               "--error-json", "--no-detail", "--update"}),
    "plan-title-updates": frozenset({"--error-json"}),
    "review-duplicate-conflicts": frozenset({"--error-json"}),
    "verify-import": frozenset({"--detail", "--error-json", "--no-detail"}),
}
CLI_COMMANDS = tuple(sorted(VALUE_OPTIONS))
READ_ONLY_COMMANDS = ("doctor", "verify-import", "audit-duplicates",
                      "review-duplicate-conflicts", "plan-title-updates")

#: python-dotenv versions whose load_dotenv() honours PYTHON_DOTENV_DISABLED (1.2.1 is the
#: version of requirements/ci-lock.txt).
DOTENV_DISABLE_VERSIONS = ("1.2.1",)


class Rejected(Exception):
    """A check failed. The message never contains a configuration value."""


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def overall(checks: list[Check]) -> str:
    statuses = {check.status for check in checks}
    if not checks:
        return UNVERIFIED
    for status in (FAIL, BLOCKED, UNVERIFIED):
        if status in statuses:
            return status
    return PASS


# --- reading files and trees without following links ------------------------------------


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


@dataclass
class Pinned:
    path: str
    sha256: str
    identity: tuple[int, int, int, int, int]
    data: bytes = field(repr=False, default=b"")


def read_pinned(path: Path, limit: int = FILE_LIMIT, keep: bool = False) -> Pinned:
    """Read a regular file once (no symlink at the last component), with its identity before
    and after the read; Rejected if it is not a regular file or changed while it was read."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Rejected(f"not readable, missing or a symlink: {path} ({exc.strerror})") from None
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Rejected(f"not a regular file: {path}")
        if before.st_size > limit:
            raise Rejected(f"larger than {limit} bytes: {path}")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if _identity(before) != _identity(after) or len(data) != before.st_size:
        raise Rejected(f"changed while it was read: {path}")
    return Pinned(str(path), hashlib.sha256(data).hexdigest(), _identity(before),
                  data if keep else b"")


@dataclass
class Tree:
    """A walk of one tree: the manifest form of every non-directory entry, and the identity
    of every entry (directories included, so that additions change a directory)."""

    entries: dict[str, str]
    identities: dict[str, tuple[int, int, int, int, int]]


def walk_tree(root: Path) -> Tree:
    """Every entry under root, without following symlinks. Files are hashed, symlinks are
    recorded with their target; any other type (fifo, socket, device) is Rejected."""
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise Rejected(f"tree root missing: {root} ({exc.strerror})") from None
    if not stat.S_ISDIR(info.st_mode):
        raise Rejected(f"tree root is not a directory: {root}")
    entries: dict[str, str] = {}
    identities = {".": _identity(info)}
    def unreadable(error: OSError) -> None:
        raise Rejected(f"unreadable directory in the tree: {error.filename}")

    for directory, dirnames, filenames in os.walk(root, onerror=unreadable, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            info = os.lstat(path)
            identities[relative] = _identity(info)
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                # Only links to a regular file inside the same tree: a link to a directory
                # (or out of the tree) would let unwalked content onto sys.path.
                target = os.path.realpath(path)
                if not _inside(Path(target), Path(os.path.realpath(root))) \
                        or not os.path.isfile(target) or os.path.isdir(target):
                    raise Rejected(f"symlink to a directory, outside the tree or dangling: "
                                   f"{path}")
                entries[relative] = SYMLINK + os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                pinned = read_pinned(path)
                if pinned.identity != identities[relative]:
                    raise Rejected(f"changed while the tree was walked: {path}")
                entries[relative] = pinned.sha256
            else:
                raise Rejected(f"unexpected file type in the tree: {path}")
    return Tree(entries, identities)


def compare_tree(name: str, expected: dict[str, str], tree: Tree) -> list[str]:
    problems = []
    unlisted = sorted(set(tree.entries) - set(expected))
    missing = sorted(set(expected) - set(tree.entries))
    changed = sorted(k for k in set(expected) & set(tree.entries) if expected[k] != tree.entries[k])
    if unlisted:
        problems.append(f"{name}: entries not in the manifest: {unlisted[:20]}")
    if missing:
        problems.append(f"{name}: missing entries: {missing[:20]}")
    if changed:
        problems.append(f"{name}: hash or link mismatch: {changed[:20]}")
    return problems


def fold(name: str) -> str:
    """How a case-insensitive, normalization-insensitive volume (macOS APFS by default) sees a
    name: every name check compares folded names, so a differently spelled file that the
    interpreter would still open under its fixed name cannot pass unnoticed."""
    return unicodedata.normalize("NFD", name).casefold()


#: Path components with a meaning to getpath / site.py; any other spelling that folds to the
#: same name fails.
SPECIAL_NAMES = ("bin", "lib", "site-packages", "lib-dynload", "pyvenv.cfg", "pybuilddir.txt",
                 "os.py", "os.pyc", "__pycache__")


def name_problems(name: str, tree: dict[str, str], major_minor: str) -> list[str]:
    """Collisions under folding, special names in another spelling, and lib/python* entries
    other than lib/python<major.minor>."""
    problems = []
    folded: dict[str, str] = {}
    canonical = {fold(n): n for n in SPECIAL_NAMES}
    expected = f"python{major_minor}"
    for key in sorted(tree):
        if fold(key) in folded:
            problems.append(f"{name}: names collide when case or Unicode form is ignored: "
                            f"{folded[fold(key)]!r} / {key!r}")
        folded[fold(key)] = key
        parts = key.split("/")
        for part in parts:
            if fold(part) in canonical and part != canonical[fold(part)]:
                problems.append(f"{name}: special name in another spelling: {key!r}")
                break
        if len(parts) > 1 and fold(parts[0]) == "lib" and fold(parts[1]).startswith("python") \
                and parts[1] != expected:
            problems.append(f"{name}: unexpected lib/python directory: {key!r}")
    return problems


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def real_directory_in(path: Path, roots: list[Path]) -> bool:
    """path is an existing directory inside one of the roots, reached without any symlink
    below that root (symlinks above a root are allowed: the root path is what was pinned)."""
    for root in roots:
        if _inside(path, root):
            expected = os.path.join(os.path.realpath(root), str(path.relative_to(root)))
            return os.path.isdir(path) and os.path.realpath(path) == os.path.normpath(expected)
    return False


# --- D-1: bootstrap trust boundary -------------------------------------------------------


@dataclass
class BootstrapResult:
    status: str
    checks: list[Check]
    manifest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": [c.as_dict() for c in self.checks]}


_MANIFEST_KEYS = {"schema", "python_version", "runtime_root", "venv", "app_root",
                  "interpreter", "trees", "provenance"}


def _relative_ok(relative: Any) -> bool:
    if not isinstance(relative, str) or not relative:
        return False
    parts = Path(relative).parts
    return not Path(relative).is_absolute() and ".." not in parts and "." not in parts


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise Rejected(f"manifest keys must be exactly {sorted(_MANIFEST_KEYS)}")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise Rejected(f"manifest schema is not {MANIFEST_SCHEMA}")
    if not isinstance(manifest["python_version"], str):
        raise Rejected("manifest python_version must be a string")
    for key in ("runtime_root", "venv", "app_root"):
        if not isinstance(manifest[key], str) or not os.path.isabs(manifest[key]) \
                or manifest[key] != os.path.normpath(manifest[key]) or "//" in manifest[key]:
            raise Rejected(f"manifest {key} must be a normalized absolute path")
    interpreter = manifest["interpreter"]
    if not (isinstance(interpreter, dict) and set(interpreter) == {"path", "sha256"}
            and _relative_ok(interpreter["path"]) and isinstance(interpreter["sha256"], str)
            and _SHA256.fullmatch(interpreter["sha256"])):
        raise Rejected("manifest interpreter must be a relative path and a sha256")
    trees = manifest["trees"]
    if not isinstance(trees, dict) or set(trees) != set(TREES):
        raise Rejected(f"manifest trees must be exactly {list(TREES)}")
    for name in TREES:
        table = trees[name]
        if not isinstance(table, dict):
            raise Rejected(f"manifest tree {name} must be an object")
        for relative, value in table.items():
            if not _relative_ok(relative) or not isinstance(value, str) or not (
                    _SHA256.fullmatch(value) or value.startswith(SYMLINK)):
                raise Rejected(f"manifest tree {name}: invalid entry {relative!r}")
    if not isinstance(manifest["provenance"], dict):
        raise Rejected("manifest provenance must be an object")
    return manifest


def check_provenance(provenance: dict[str, Any], evidence_root: Path) -> Check:
    """Integrity is not authenticity. PASS means: the manifest names the attested archive of
    the Cutover Safety Gate and a committed evidence file (pinned by sha256) that records that
    archive with every gate PASS. The attestation itself is human-asserted (it was verified by
    the gate run that the evidence records, not here); anything less is UNVERIFIED, and a
    contradiction is FAIL."""
    archive = provenance.get("archive_sha256")
    if archive is None:
        return Check("provenance", UNVERIFIED, "no archive sha256 recorded (untrusted provenance)")
    if archive != VERIFIED_ARCHIVE_SHA256:
        return Check("provenance", FAIL, "archive sha256 is not the archive verified by the "
                                         "Cutover Safety Gate")
    evidence, expected = provenance.get("evidence"), provenance.get("evidence_sha256")
    if provenance.get("attestation_verified") is not True or not _relative_ok(evidence) \
            or not isinstance(expected, str):
        return Check("provenance", UNVERIFIED, "no recorded attestation evidence")
    try:
        pinned = read_pinned(evidence_root / evidence, CONFIG_LIMIT * 16, keep=True)
        record = json.loads(pinned.data.decode("utf-8"))
    except (Rejected, ValueError) as exc:
        return Check("provenance", FAIL, f"attestation evidence not readable: {exc}")
    if pinned.sha256 != expected:
        return Check("provenance", FAIL, "attestation evidence sha256 differs from the manifest")
    try:
        recorded = record["archive"]["sha256"]
        gates = record["formal_gate_run"]["gates"]
    except (KeyError, TypeError):
        return Check("provenance", FAIL, "attestation evidence has no archive or gate record")
    if recorded != archive or not isinstance(gates, dict) or not gates \
            or any(v != PASS for v in gates.values()):
        return Check("provenance", FAIL, "attestation evidence does not record this archive "
                                         "with every gate PASS")
    return Check("provenance", PASS, f"human-asserted attestation; evidence {evidence} "
                                     f"(sha256 {pinned.sha256}) records the archive and "
                                     "every gate PASS")


_PYVENV_FORBIDDEN = re.compile("[\r\x00\x0b\x0c\x1c-\x1f\x85\u2028\u2029]")


def _parse_pyvenv_cfg(text: str) -> dict[str, str]:
    """A stricter parser than CPython's getpath (which takes the first ``home`` line and
    matches keys case-insensitively): only ``\n`` (or ``\r\n``) ends a line, every non-blank
    line is ``key = value``, keys are lower case and appear once. Rejected otherwise, so the
    two parsers cannot read different values from the same file."""
    values: dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if _PYVENV_FORBIDDEN.search(line):
            raise Rejected(f"pyvenv.cfg line {number}: unexpected control character")
        if not line.strip():
            continue
        if "=" not in line:
            raise Rejected(f"pyvenv.cfg line {number}: not key = value")
        key, value = (part.strip() for part in line.split("=", 1))
        if key != key.lower() or not key:
            raise Rejected(f"pyvenv.cfg line {number}: key is not lower case: {key!r}")
        if key in values:
            raise Rejected(f"pyvenv.cfg line {number}: duplicate key {key}")
        values[key] = value
    return values


def site_packages(root: Path) -> list[Path]:
    lib = root / "lib"
    if not lib.is_dir():
        return []
    return sorted(p / "site-packages" for p in lib.iterdir()
                  if p.name.startswith("python") and (p / "site-packages").is_dir())


STARTUP_HOOKS = ("sitecustomize", "usercustomize")
#: The keys venv writes into pyvenv.cfg (``command`` and ``prompt`` are informational).
PYVENV_KEYS = frozenset({"home", "include-system-site-packages", "version", "version_info",
                         "executable", "command", "prompt"})


def startup_hooks(directories: list[Path]) -> list[str]:
    """sitecustomize / usercustomize modules, packages or caches in the given directories."""
    found = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(os.listdir(directory)):
            if fold(entry.split(".", 1)[0]) in STARTUP_HOOKS:
                found.append(str(directory / entry))
        cache = directory / "__pycache__"
        if cache.is_dir():
            found += [str(cache / e) for e in sorted(os.listdir(cache))
                      if fold(e.split(".", 1)[0]) in STARTUP_HOOKS]
    return found


def executable_pth_lines(data: bytes) -> int:
    """Lines that site.py executes (``import`` followed by a space or a tab)."""
    return sum(1 for line in data.decode("utf-8", "replace").splitlines()
               if line.startswith(("import ", "import\t")))


def pth_paths(site: Path, data: bytes) -> list[Path]:
    """The directories a .pth adds to sys.path (site.py: every line that is not blank, a
    comment or an import line, relative to the site directory)."""
    paths = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.rstrip()
        if not line or line.startswith("#") or line.startswith(("import ", "import\t")):
            continue
        paths.append(Path(os.path.normpath(os.path.join(site, line))))
    return paths


def injected_variables(environ: dict[str, str]) -> list[str]:
    return sorted(name for name in environ if name.startswith(FORBIDDEN_PREFIXES))


def verify_bootstrap(manifest_path: Path, expected_manifest_sha256: str | None,
                     requested_python: str, environ: dict[str, str],
                     evidence_root: Path = REPO_ROOT) -> BootstrapResult:
    """All checks are read-only and run before the runtime under test is ever started."""
    checks: list[Check] = []

    def stop(name: str, status: str, detail: str) -> BootstrapResult:
        checks.append(Check(name, status, detail))
        return BootstrapResult(overall(checks), checks)

    try:
        manifest_file = read_pinned(manifest_path, CONFIG_LIMIT * 256, keep=True)
    except Rejected as exc:
        return stop("manifest_present", FAIL, f"missing expected manifest: {exc}")
    if expected_manifest_sha256 is None:
        checks.append(Check("manifest_integrity", UNVERIFIED,
                            "no independently recorded manifest sha256 was given"))
    elif manifest_file.sha256 != expected_manifest_sha256:
        return stop("manifest_integrity", FAIL, "manifest sha256 differs from the recorded value")
    else:
        checks.append(Check("manifest_integrity", PASS, manifest_file.sha256))
    try:
        manifest = _validate_manifest(json.loads(manifest_file.data.decode("utf-8")))
    except (Rejected, ValueError) as exc:
        return stop("manifest_schema", FAIL, str(exc) if isinstance(exc, Rejected)
                    else "manifest is not valid UTF-8 JSON")
    checks.append(Check("manifest_schema", PASS, MANIFEST_SCHEMA))
    checks.append(check_provenance(manifest["provenance"], evidence_root))

    roots = {"runtime": Path(manifest["runtime_root"]), "venv": Path(manifest["venv"]),
             "app": Path(manifest["app_root"])}
    venv, runtime = roots["venv"], roots["runtime"]
    expected_python = venv / manifest["interpreter"]["path"]
    if not os.path.isabs(requested_python) or Path(requested_python) != expected_python:
        return stop("interpreter_path", FAIL, f"unexpected Python path: {requested_python}")
    checks.append(Check("interpreter_path", PASS, str(expected_python)))

    try:
        interpreter = read_pinned(expected_python)
    except Rejected as exc:
        return stop("interpreter_integrity", FAIL, str(exc))
    if interpreter.sha256 != manifest["interpreter"]["sha256"] or \
            manifest["trees"]["venv"].get(manifest["interpreter"]["path"]) != interpreter.sha256:
        return stop("interpreter_integrity", FAIL, "executable hash mismatch")
    checks.append(Check("interpreter_integrity", PASS, interpreter.sha256))

    try:
        cfg = read_pinned(venv / "pyvenv.cfg", CONFIG_LIMIT, keep=True)
    except Rejected as exc:
        return stop("runtime_version", FAIL, str(exc))
    try:
        values = _parse_pyvenv_cfg(cfg.data.decode("utf-8"))
    except UnicodeDecodeError:
        return stop("runtime_version", FAIL, "pyvenv.cfg is not UTF-8")
    except Rejected as exc:
        return stop("runtime_version", FAIL, str(exc))
    version = values.get("version_info") or values.get("version")
    if version not in KNOWN_VERSIONS or version != manifest["python_version"]:
        return stop("runtime_version", FAIL, f"unknown runtime version: {version}")
    if values.get("include-system-site-packages", "false").lower() != "false":
        return stop("runtime_version", FAIL, "the venv includes the system site-packages")
    # getpath finds the stdlib from `home`: it must be the verified runtime's bin directory,
    # holding that runtime's interpreter, or the stdlib would load from an unchecked place.
    unknown_keys = sorted(set(values) - PYVENV_KEYS)
    if unknown_keys:
        return stop("runtime_version", FAIL, f"unexpected pyvenv.cfg keys: {unknown_keys}")
    major_minor = ".".join(version.split(".")[:2])
    runtime_bin = os.path.normpath(os.path.join(manifest["runtime_root"], "bin"))
    base_python = f"bin/python{major_minor}"
    # Exact strings: getpath uses home as written and walks up from it lexically, so
    # "/tmp/../<runtime>/bin" would search /tmp although it normalizes to the runtime's bin.
    if os.path.realpath(manifest["runtime_root"]) != manifest["runtime_root"]:
        return stop("runtime_version", FAIL, "runtime_root is reached through a symlink")
    if values.get("home") != runtime_bin:
        return stop("runtime_version", FAIL, "pyvenv.cfg home is not the verified runtime's bin")
    if "executable" in values and values["executable"] != \
            os.path.join(runtime_bin, f"python{major_minor}"):
        return stop("runtime_version", FAIL, "pyvenv.cfg executable is not the verified runtime's")
    if manifest["interpreter"]["path"] != base_python:
        return stop("runtime_version", FAIL, f"the interpreter must be {base_python} of the venv")
    runtime_tree = manifest["trees"]["runtime"]
    # ._pth files replace sys.path entirely (checked by getpath before home and venv);
    # a second pyvenv.cfg next to the interpreter is read by site.venv() first.
    replacing = sorted(f"{name}:{k}" for name in TREES for k in manifest["trees"][name]
                       if fold(k).endswith("._pth")) + \
        [f"venv:{k}" for k in manifest["trees"]["venv"] if fold(k) == "bin/pyvenv.cfg"]
    if replacing:
        return stop("runtime_version", FAIL, f"sys.path replacing files: {replacing}")
    if not _SHA256.fullmatch(runtime_tree.get(base_python, "")):
        return stop("runtime_version", FAIL, f"the runtime inventory has no {base_python}")
    # getpath searches upward from the interpreter's directory for the stdlib landmark
    # lib/pythonX.Y/os.py (prefix) and lib/pythonX.Y/lib-dynload (exec_prefix); a build
    # directory is detected by bin/pybuilddir.txt. The landmarks must be exactly the runtime
    # root's, and nothing in bin may be an earlier match.
    landmark = f"lib/python{major_minor}/os.py"
    if not _SHA256.fullmatch(runtime_tree.get(landmark, "")):
        return stop("runtime_version", FAIL, f"the runtime inventory has no {landmark}")
    shadowing = sorted(k for k in runtime_tree
                       if fold(k).startswith(("bin/lib/", "bin/modules/"))
                       or fold(k) in ("bin/pybuilddir.txt", "bin/os.py", "bin/os.pyc"))
    if shadowing:
        return stop("runtime_version", FAIL, f"stdlib landmarks under bin: {shadowing}")
    spelled = [problem for name in TREES
               for problem in name_problems(name, manifest["trees"][name], major_minor)]
    if spelled:
        return stop("runtime_version", FAIL, "; ".join(spelled[:20]))
    checks.append(Check("runtime_version", PASS, version))

    walked: dict[str, Tree] = {}
    problems: list[str] = []
    for name in TREES:
        try:
            walked[name] = walk_tree(roots[name])
        except Rejected as exc:
            problems.append(f"{name}: {exc}")
            continue
        problems += compare_tree(name, manifest["trees"][name], walked[name])
    if problems:
        return stop("tree_inventory", FAIL, "; ".join(problems))
    checks.append(Check("tree_inventory", PASS, ", ".join(
        f"{name} {len(walked[name].entries)} entries" for name in TREES)))

    # The directories on the planned sys.path: stdlib, lib-dynload, site-packages, and the
    # path lines of every .pth (site.py processes .pth files only in site directories).
    stdlib = runtime / "lib" / f"python{major_minor}"
    zipped = runtime / "lib" / f"python{major_minor.replace('.', '')}.zip"
    if os.path.lexists(zipped):
        return stop("startup_hooks", FAIL, f"a zip on sys.path is not inspected: {zipped}")
    sites = site_packages(venv) + site_packages(runtime)
    directories = [stdlib, stdlib / "lib-dynload", *sites]
    pth_count = 0
    for site in sites:
        for entry in sorted(os.listdir(site)):
            if not fold(entry).endswith(".pth"):
                continue
            pth_count += 1
            try:
                data = read_pinned(site / entry, CONFIG_LIMIT, keep=True).data
            except Rejected as exc:
                problems.append(str(exc))
                continue
            if executable_pth_lines(data):
                # An import line runs before sitecustomize and can install import hooks that
                # load code from anywhere: none is accepted (the 3.14 venv has none).
                problems.append(f"{entry}: executable (import) lines are not accepted")
            for path in pth_paths(site, data):
                if not any(_inside(path, root) for root in roots.values()):
                    problems.append(f"{entry}: path outside the verified trees: {path}")
                elif not path.is_dir():
                    problems.append(f"{entry}: path is not an existing directory: {path}")
                else:
                    directories.append(path)
    if problems:
        return stop("pth_files", FAIL, "; ".join(problems))
    checks.append(Check("pth_files", PASS,
                        f"{pth_count} .pth files (pinned by the inventory), no executable line"))
    tree_roots = list(roots.values())
    # The stdlib and lib-dynload must exist (getpath would otherwise search above the
    # runtime root); every directory must be real and inside a tree.
    linked = [str(d) for d in directories if not real_directory_in(d, tree_roots)]
    if linked:
        return stop("startup_hooks", FAIL, f"sys.path directories missing, reached through a "
                                           f"symlink or outside the trees: {linked}")
    hooks = startup_hooks(directories)
    if hooks:
        return stop("startup_hooks", FAIL, f"unexpected startup hook: {hooks}")
    checks.append(Check("startup_hooks", PASS, f"none in {len(directories)} sys.path directories"))

    injected = injected_variables(environ)
    if injected:
        return stop("environment", FAIL, f"unexpected environment variables: {injected}")
    checks.append(Check("environment", PASS, "no PYTHON*/DYLD_*/LD_*/NOTION_*/PIP_* variable"))

    changed = []
    for name in TREES:
        try:
            again = walk_tree(roots[name])
        except Rejected as exc:
            changed.append(f"{name}: {exc}")
            continue
        if again.identities != walked[name].identities or again.entries != walked[name].entries:
            differ = sorted(k for k in set(again.identities) | set(walked[name].identities)
                            if again.identities.get(k) != walked[name].identities.get(k))
            changed.append(f"{name}: {differ[:20]}")
    if not _same_file(manifest_path, manifest_file):
        changed.append("the manifest")
    provenance = manifest["provenance"]
    if [c.status for c in checks if c.name == "provenance"] == [PASS]:
        try:
            again_evidence = read_pinned(evidence_root / provenance["evidence"], CONFIG_LIMIT * 16)
            if again_evidence.sha256 != provenance["evidence_sha256"]:
                changed.append("the attestation evidence")
        except Rejected:
            changed.append("the attestation evidence")
    if changed:
        return stop("toctou", FAIL, f"changed after it was verified: {changed}")
    checks.append(Check("toctou", PASS, "all trees and the manifest unchanged at the end"))
    return BootstrapResult(overall(checks), checks, manifest)


def _same_file(path: Path, pinned: Pinned) -> bool:
    try:
        return _identity(os.lstat(path)) == pinned.identity
    except OSError:
        return False


# --- C-2 / C-3: production launcher (dry-run plan only) ----------------------------------


class Secret:
    """A configuration value that never prints."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def load_config(path: Path) -> dict[str, Secret]:
    """The only source of credentials: an explicit KEY=VALUE file owned by the current user
    and not readable by anyone else. Messages name keys and line numbers, never values."""
    pinned = read_pinned(path, CONFIG_LIMIT, keep=True)
    mode = pinned.identity[4]
    if mode & 0o077:
        raise Rejected(f"config must not be accessible by group or others (mode {mode & 0o777:o})")
    if os.lstat(path).st_uid != os.getuid():
        raise Rejected("config is not owned by the current user")
    try:
        text = pinned.data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected("config is not UTF-8") from None
    values: dict[str, Secret] = {}
    allowed = CONFIG_REQUIRED + CONFIG_OPTIONAL
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _CONFIG_LINE.match(line)
        if not match:
            raise Rejected(f"config line {number}: expected KEY=VALUE")
        key, value = match.groups()
        if key not in allowed:
            raise Rejected(f"config line {number}: unknown key {key}")
        if key in values:
            raise Rejected(f"config line {number}: duplicate key {key}")
        if not value or not _TOKEN.match(value):
            raise Rejected(f"config line {number}: invalid value for {key}")
        if key.endswith("_DATA_SOURCE_ID") and not _DATA_SOURCE_ID.match(value):
            raise Rejected(f"config line {number}: {key} is not a data source id")
        values[key] = Secret(value)
    missing = [key for key in CONFIG_REQUIRED if key not in values]
    if missing:
        raise Rejected(f"config is missing {missing}")
    if pinned.identity != _identity(os.lstat(path)):
        raise Rejected("config changed while it was checked")
    return values


def _reads_disable_variable(source: bytes) -> bool:
    """dotenv/main.py defines _load_dotenv_disabled() reading PYTHON_DOTENV_DISABLED, and
    load_dotenv() calls it (parsed, not imported; comments do not count)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    check, load = functions.get("_load_dotenv_disabled"), functions.get("load_dotenv")
    if check is None or load is None:
        return False
    reads = any(isinstance(n, ast.Constant) and n.value == "PYTHON_DOTENV_DISABLED"
                for n in ast.walk(check))
    called = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_load_dotenv_disabled" for n in ast.walk(load))
    return reads and called


def dotenv_disable_supported(venv: Path, venv_tree: dict[str, str]) -> Check:
    """Static check (nothing is imported or run) that the venv's python-dotenv - a file the
    bootstrap inventory pinned - honours PYTHON_DOTENV_DISABLED."""
    problems = []
    for packages in site_packages(venv):
        versions = sorted(p.name[len("python_dotenv-"):-len(".dist-info")]
                          for p in packages.glob("python_dotenv-*.dist-info"))
        if not versions:
            continue
        if versions[0] not in DOTENV_DISABLE_VERSIONS or len(versions) != 1:
            problems.append(f"python-dotenv {versions} not in {list(DOTENV_DISABLE_VERSIONS)}")
            continue
        main = packages / "dotenv" / "main.py"
        relative = str(main.relative_to(venv))
        try:
            pinned = read_pinned(main, keep=True)
        except Rejected as exc:
            problems.append(str(exc))
            continue
        if venv_tree.get(relative) != pinned.sha256:
            problems.append("dotenv/main.py is not the file pinned by the manifest")
            continue
        if not _reads_disable_variable(pinned.data):
            problems.append("dotenv/main.py does not honour PYTHON_DOTENV_DISABLED")
            continue
        return Check("dotenv_disabled", PASS, f"python-dotenv {versions[0]}")
    return Check("dotenv_disabled", FAIL, "; ".join(problems) or "python-dotenv not found")


def launch_environment(environ: dict[str, str], config: dict[str, Secret]) -> dict[str, Any]:
    if not environ.get("HOME") or not os.path.isabs(environ["HOME"]):
        raise Rejected("HOME is missing or not an absolute path")
    kept: dict[str, Any] = {k: v for k, v in environ.items() if k in ENVIRONMENT_KEEP}
    return {**kept, "PATH": os.pathsep.join(LAUNCH_PATH), "PYTHON_DOTENV_DISABLED": "1",
            **config}


def parse_invocation(argv: list[str]) -> set[str] | None:
    """The flags of a CLI invocation, parsed like Click does for the known options (an option
    that takes a value consumes the next token, even one starting with ``--``; ``--`` ends the
    options). None if the command or an option is unknown or a value is missing."""
    if not argv or argv[0] not in VALUE_OPTIONS:
        return None
    values, flags = VALUE_OPTIONS[argv[0]], FLAG_OPTIONS[argv[0]]
    found: set[str] = set()
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token.startswith("--"):
            name, equals, _ = token.partition("=")
            if name in values:
                if not equals:
                    index += 1
                    if index >= len(argv):
                        return None
            elif name in flags and not equals:
                found.add(name)
            else:
                return None
        elif token.startswith("-") and token != "-":
            return None
        index += 1
    return found


def command_may_mutate(argv: list[str]) -> bool:
    """True unless the invocation cannot write to Notion (unparseable ones may)."""
    flags = parse_invocation(argv)
    if flags is None:
        return True
    if argv[0] in READ_ONLY_COMMANDS:
        return False
    if argv[0] == "import":
        return "--dry-run" not in flags
    return any(flag.startswith("--apply") for flag in flags)


REQUIRED_PREFLIGHT = ("bootstrap", "interpreter_explicit", "inherited_environment", "config",
                      "dotenv_disabled", "command")


@dataclass
class LaunchPlan:
    status: str
    checks: list[Check]
    argv: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict, repr=False)
    may_mutate: bool = True
    executable: bool = EXECUTION_ENABLED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "execution": EXECUTION_NOTICE,
            "executable": self.executable,
            "checks": [c.as_dict() for c in self.checks],
            "argv": self.argv,
            "environment_names": sorted(self.environment),
            "credentials": "<redacted>",
            "may_mutate_notion": self.may_mutate,
        }


def complete(checks: list[Check]) -> bool:
    by_name = {c.name: c.status for c in checks}
    return all(by_name.get(name) == PASS for name in REQUIRED_PREFLIGHT)


def plan_launch(bootstrap: BootstrapResult, python: str, config_path: Path | None,
                environ: dict[str, str], cli_args: list[str]) -> LaunchPlan:
    checks = [Check("bootstrap", bootstrap.status, "the bootstrap result")]

    def finish(argv: list[str] | None = None, env: dict[str, Any] | None = None) -> LaunchPlan:
        status = overall(checks)
        if status == PASS and not complete(checks):
            checks.append(Check("preflight_complete", FAIL, "incomplete preflight"))
            status = FAIL
        if status != PASS:
            argv, env = None, None
        return LaunchPlan(status, checks, argv or [], env or {}, command_may_mutate(cli_args),
                          executable=EXECUTION_ENABLED)

    if bootstrap.status != PASS:
        return finish()
    venv = Path(bootstrap.manifest["venv"])
    expected = venv / bootstrap.manifest["interpreter"]["path"]
    if not os.path.isabs(python) or Path(python) != expected:
        checks.append(Check("interpreter_explicit", FAIL, f"wrong executable path: {python}"))
        return finish()
    checks.append(Check("interpreter_explicit", PASS, python))
    injected = injected_variables(environ)
    if injected:
        checks.append(Check("inherited_environment", FAIL,
                            f"refused inherited variables: {injected}"))
        return finish()
    checks.append(Check("inherited_environment", PASS, "none of " + "/".join(FORBIDDEN_PREFIXES)))
    if config_path is None:
        checks.append(Check("config", FAIL, "missing config: an explicit --config is required"))
        return finish()
    try:
        config = load_config(config_path)
        env = launch_environment(environ, config)
    except Rejected as exc:
        checks.append(Check("config", FAIL, str(exc)))
        return finish()
    checks.append(Check("config", PASS, f"keys {sorted(config)}"))
    checks.append(dotenv_disable_supported(venv, bootstrap.manifest["trees"]["venv"]))
    if checks[-1].status != PASS:
        return finish()
    if parse_invocation(cli_args) is None:
        checks.append(Check("command", FAIL, f"unknown command or option: {cli_args[:1]}"))
        return finish()
    checks.append(Check("command", PASS, cli_args[0]))
    return finish([python, *LAUNCH_ARGS, *cli_args], env)


# --- rollback model ----------------------------------------------------------------------


@dataclass
class RollbackInputs:
    """What the operator recorded before the cutover, and what is observed now."""

    legacy_baseline: dict[str, str]
    legacy_observed: dict[str, str] | None
    config_fingerprint_at_cutover: str
    config_fingerprint_now: str | None
    journal: list[list[str]]


@dataclass
class RollbackAssessment:
    local: str
    external: str
    unconditional: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"local": self.local, "external": self.external,
                "unconditional": self.unconditional, "reasons": self.reasons}


def assess_rollback(inputs: RollbackInputs) -> RollbackAssessment:
    reasons = []
    local = PASS
    if inputs.legacy_observed is None:
        local = FAIL
        reasons.append("missing artifact: the legacy runtime is not present")
    elif inputs.legacy_observed != inputs.legacy_baseline:
        local = FAIL
        changed = sorted(k for k in set(inputs.legacy_baseline) | set(inputs.legacy_observed)
                         if inputs.legacy_baseline.get(k) != inputs.legacy_observed.get(k))
        reasons.append(f"the legacy runtime differs from its baseline: {changed}")
    if inputs.config_fingerprint_now != inputs.config_fingerprint_at_cutover:
        local = FAIL
        reasons.append("configuration mismatch since the cutover")
    mutations = [argv[:1] for argv in inputs.journal if command_may_mutate(argv)]
    if mutations:
        external = "MUTATIONS_REQUIRE_REVIEW"
        reasons.append(f"{len(mutations)} invocation(s) after the cutover may have written to "
                       "Notion; rolling back the runtime does not restore them")
    else:
        external = "NO_MUTATION_RECORDED"
    return RollbackAssessment(local, external, local == PASS and not mutations, reasons)


SELECTOR = "active-runtime"
RUNTIMES = ("legacy", "new")


def read_selector(root: Path) -> str:
    value = read_pinned(root / SELECTOR, 64, keep=True).data.decode("ascii", "replace").strip()
    if value not in RUNTIMES:
        raise Rejected(f"selector holds an unknown runtime: {value!r}")
    return value


def write_selector(root: Path, value: str, fail_before_replace: bool = False) -> None:
    """Atomic: a crash before os.replace leaves the previous selector intact."""
    temporary = root / f".{SELECTOR}.tmp"
    with open(temporary, "x", encoding="ascii") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if fail_before_replace:
        raise Rejected("simulated failure during the rollback switch")
    os.replace(temporary, root / SELECTOR)


HealthCheck = Callable[[Path, str], bool]


@dataclass
class SimulationResult:
    status: str
    active: str
    steps: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "active": self.active, "steps": self.steps}


def simulate_rollback(root: Path, inputs: RollbackInputs, health: HealthCheck,
                      fail_at: str | None = None) -> SimulationResult:
    steps = []
    assessment = assess_rollback(inputs)
    steps.append(f"assess: local={assessment.local} external={assessment.external}")
    if assessment.local != PASS:
        return SimulationResult(FAIL, read_selector(root), steps + ["stop: preconditions"])
    if not assessment.unconditional:
        steps.append("note: external state needs a separate human review")
    running = root / "new.running"
    if running.exists():
        running.unlink()
    steps.append("stopped the new runtime")
    try:
        write_selector(root, "legacy", fail_before_replace=fail_at == "switch")
    except Rejected as exc:
        (root / f".{SELECTOR}.tmp").unlink()
        return SimulationResult(FAIL, read_selector(root),
                                steps + [f"stop: {exc}; no automatic repair"])
    steps.append("switched to legacy")
    if read_selector(root) != "legacy" or not health(root, "legacy"):
        return SimulationResult(FAIL, read_selector(root),
                                steps + ["stop: post-rollback health check failed; no "
                                         "automatic repair"])
    steps.append("legacy health check passed")
    return SimulationResult(PASS, "legacy", steps)


def simulate_cutover(root: Path, inputs: RollbackInputs, health: HealthCheck) -> SimulationResult:
    """Switch to the new runtime; if it does not come up, abort and roll back."""
    if read_selector(root) != "legacy":
        return SimulationResult(FAIL, read_selector(root), ["stop: not starting from legacy"])
    write_selector(root, "new")
    (root / "new.running").write_text("1\n", encoding="ascii")
    steps = ["switched to new"]
    if health(root, "new"):
        return SimulationResult(PASS, "new", steps + ["new health check passed"])
    steps.append("abort: the new runtime failed its health check")
    result = simulate_rollback(root, inputs, health)
    status = FAIL if result.status != PASS else "ROLLED_BACK"
    return SimulationResult(status, result.active, steps + result.steps)


# --- command line -------------------------------------------------------------------------


def main(argv: list[str], environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    parser = argparse.ArgumentParser(description="Phase 3H-R7 cutover readiness (dry-run only)")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("bootstrap", "launch-plan"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--manifest-sha256")
        p.add_argument("--python", required=True)
        if name == "launch-plan":
            p.add_argument("--config", type=Path)
            p.add_argument("cli_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    result = verify_bootstrap(args.manifest, args.manifest_sha256, args.python, environ)
    if args.action == "bootstrap":
        report: dict[str, Any] = result.as_dict()
    else:
        cli_args = args.cli_args[1:] if args.cli_args[:1] == ["--"] else args.cli_args
        report = plan_launch(result, args.python, args.config, environ, cli_args).as_dict()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == PASS else (1 if report["status"] == FAIL else 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
