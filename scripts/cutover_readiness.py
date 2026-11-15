"""Phase 3H-R7: production cutover authorization readiness (a DISABLED, dry-run-only component).

Three read-only checks that a human needs before deciding on the Python 3.14 cutover. None of
them launches anything:

* ``verify_bootstrap`` (D-1, local runtime scope): before any Python of the runtime under test
  is started, check the venv interpreter, ``pyvenv.cfg``, libraries, dependency files and
  ``.pth`` files against a runtime manifest whose own sha256 must come from an independent,
  human-approved record. Startup hooks (``sitecustomize`` / ``usercustomize``), unknown
  ``.pth`` files, an unknown runtime version, an unexpected interpreter path, injected
  environment variables and files that change while they are checked (TOCTOU) fail. A
  missing provenance record or a missing expected manifest hash is UNVERIFIED, never PASS:
  a matching sha256 alone does not make a third-party build trustworthy.
* ``plan_launch`` (C-2 / C-3): the launch the production launcher *would* use - an explicit
  interpreter, ``-I -B -m mtg_notion_manager.cli``, an allowlisted environment, python-dotenv's
  automatic ``.env`` discovery disabled (``PYTHON_DOTENV_DISABLED=1``) and the credentials
  taken only from one explicit, owner-only config file. Inherited ``NOTION_*`` / ``PYTHON*`` /
  ``DYLD_*`` / ``LD_*`` / ``PIP_*`` variables are refused, not silently dropped. Secret values
  are held in ``Secret`` and never appear in a message, a report or a log. The plan is never
  executed: ``EXECUTION_ENABLED`` is False and this module has no exec, subprocess or socket
  code at all (tested).
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
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PASS, FAIL, BLOCKED, UNVERIFIED = "PASS", "FAIL", "BLOCKED", "UNVERIFIED"

#: The launcher never executes its plan in Phase 3H-R7 (no code path does; tested).
EXECUTION_ENABLED = False
EXECUTION_NOTICE = ("DISABLED: Phase 3H-R7 builds and checks the plan only. "
                    "PRODUCTION CUTOVER NOT AUTHORIZED.")

MANIFEST_SCHEMA = "mtg-notion-manager/runtime-manifest/1"
#: Runtime versions this repository has verified (the marshal reader of
#: scripts/verify_cutover_gate.py is tied to 3.14.10, too).
KNOWN_VERSIONS = ("3.14.10",)
#: The archive verified by the Cutover Safety Gate (docs/local-python-runtime.md; tested to
#: equal scripts/verify_cutover_gate.py ARCHIVE_SHA256).
VERIFIED_ARCHIVE_SHA256 = "30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4"
FILE_LIMIT = 256 << 20
CONFIG_LIMIT = 64 << 10

#: Inherited variables that must not be present when the launcher plans a launch, and that
#: the bootstrap check reports as injected. Refused, not dropped: a contaminated shell is a
#: reason to stop.
FORBIDDEN_PREFIXES = ("NOTION_", "PYTHON", "DYLD_", "LD_", "PIP_")
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

#: The commands of src/mtg_notion_manager/cli.py. ``import`` writes unless --dry-run; the
#: others write only with an --apply* option; the read-only ones never write.
CLI_COMMANDS = ("doctor", "import", "import-cards", "import-article", "verify-import",
                "dedupe-cards", "audit-duplicates", "review-duplicate-conflicts",
                "apply-dedupe-plan", "apply-price-link-dedupe", "plan-title-updates",
                "apply-single-title-update")
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


# --- reading files without following links ---------------------------------------------


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


def unchanged(pinned: Pinned) -> bool:
    try:
        info = os.lstat(pinned.path)
    except OSError:
        return False
    return _identity(info) == pinned.identity


def _relative(base: Path, relative: str) -> Path:
    """base / relative, refusing absolute paths and '..' (manifest paths stay inside base)."""
    parts = Path(relative).parts
    if not relative or Path(relative).is_absolute() or ".." in parts:
        raise Rejected(f"manifest path must be relative and inside its root: {relative!r}")
    return base / relative


# --- D-1: bootstrap trust boundary -------------------------------------------------------


@dataclass
class BootstrapResult:
    status: str
    checks: list[Check]
    manifest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": [c.as_dict() for c in self.checks]}


_MANIFEST_KEYS = {"schema", "python_version", "runtime_root", "venv", "interpreter",
                  "pyvenv_cfg", "libraries", "files", "pth", "provenance"}


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise Rejected(f"manifest keys must be exactly {sorted(_MANIFEST_KEYS)}")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise Rejected(f"manifest schema is not {MANIFEST_SCHEMA}")
    for key in ("runtime_root", "venv"):
        if not isinstance(manifest[key], str) or not os.path.isabs(manifest[key]):
            raise Rejected(f"manifest {key} must be an absolute path")
    interpreter = manifest["interpreter"]
    if not (isinstance(interpreter, dict) and set(interpreter) == {"path", "sha256"}):
        raise Rejected("manifest interpreter must have exactly path and sha256")
    if not (isinstance(manifest["pyvenv_cfg"], dict) and set(manifest["pyvenv_cfg"]) == {"sha256"}):
        raise Rejected("manifest pyvenv_cfg must have exactly sha256")
    for key in ("libraries", "files", "pth"):
        table = manifest[key]
        if not isinstance(table, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
            raise Rejected(f"manifest {key} must map paths to sha256 strings")
    hashes = [interpreter["sha256"], manifest["pyvenv_cfg"]["sha256"],
              *manifest["libraries"].values(), *manifest["files"].values(),
              *manifest["pth"].values()]
    if not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
        raise Rejected("manifest sha256 values must be 64 lowercase hex digits")
    if not all(re.fullmatch(r"[^/\\]+\.pth", name) for name in manifest["pth"]):
        raise Rejected("manifest pth entries must be bare *.pth file names")
    if not isinstance(manifest["provenance"], dict):
        raise Rejected("manifest provenance must be an object")
    return manifest


def check_provenance(provenance: dict[str, Any]) -> Check:
    """Integrity is not authenticity: PASS needs the attested archive of the Cutover Safety
    Gate and a recorded attestation; anything less is UNVERIFIED (or FAIL if it contradicts)."""
    archive = provenance.get("archive_sha256")
    if archive is None:
        return Check("provenance", UNVERIFIED, "no archive sha256 recorded (untrusted provenance)")
    if archive != VERIFIED_ARCHIVE_SHA256:
        return Check("provenance", FAIL, "archive sha256 is not the archive verified by the "
                                         "Cutover Safety Gate")
    if provenance.get("attestation_verified") is not True or not provenance.get("evidence"):
        return Check("provenance", UNVERIFIED, "no recorded attestation verification")
    return Check("provenance", PASS, f"attested archive; evidence {provenance['evidence']}")


def _parse_pyvenv_cfg(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def site_packages(root: Path) -> list[Path]:
    lib = root / "lib"
    if not lib.is_dir():
        return []
    return sorted(p / "site-packages" for p in lib.iterdir()
                  if p.name.startswith("python") and (p / "site-packages").is_dir())


STARTUP_HOOKS = ("sitecustomize", "usercustomize")


def startup_hooks(directories: list[Path]) -> list[str]:
    """sitecustomize / usercustomize modules, packages or caches in the given directories."""
    found = []
    for directory in directories:
        for entry in sorted(os.listdir(directory)):
            stem = entry.split(".", 1)[0]
            if stem in STARTUP_HOOKS:
                found.append(str(directory / entry))
        cache = directory / "__pycache__"
        if cache.is_dir():
            found += [str(cache / e) for e in sorted(os.listdir(cache))
                      if e.split(".", 1)[0] in STARTUP_HOOKS]
    return found


def executable_pth_lines(data: bytes) -> int:
    """Lines that site.py executes (``import`` followed by a space or a tab)."""
    return sum(1 for line in data.decode("utf-8", "replace").splitlines()
               if line.startswith(("import ", "import\t")))


def injected_variables(environ: dict[str, str]) -> list[str]:
    return sorted(name for name in environ if name.startswith(FORBIDDEN_PREFIXES))


def verify_bootstrap(manifest_path: Path, expected_manifest_sha256: str | None,
                     requested_python: str, environ: dict[str, str]) -> BootstrapResult:
    """All checks are read-only and run before the runtime under test is ever started."""
    checks: list[Check] = []
    pinned: list[Pinned] = []

    def stop(name: str, status: str, detail: str) -> BootstrapResult:
        checks.append(Check(name, status, detail))
        return BootstrapResult(overall(checks), checks)

    try:
        manifest_file = read_pinned(manifest_path, CONFIG_LIMIT * 16, keep=True)
    except Rejected as exc:
        return stop("manifest_present", FAIL, f"missing expected manifest: {exc}")
    pinned.append(manifest_file)
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
    checks.append(check_provenance(manifest["provenance"]))

    venv, runtime = Path(manifest["venv"]), Path(manifest["runtime_root"])
    try:
        expected_python = _relative(venv, manifest["interpreter"]["path"])
    except Rejected as exc:
        return stop("interpreter_path", FAIL, str(exc))
    if not os.path.isabs(requested_python) or Path(requested_python) != expected_python:
        return stop("interpreter_path", FAIL, f"unexpected Python path: {requested_python}")
    checks.append(Check("interpreter_path", PASS, str(expected_python)))

    try:
        interpreter = read_pinned(expected_python)
    except Rejected as exc:
        return stop("interpreter_integrity", FAIL, str(exc))
    pinned.append(interpreter)
    if interpreter.sha256 != manifest["interpreter"]["sha256"]:
        return stop("interpreter_integrity", FAIL, "executable hash mismatch")
    checks.append(Check("interpreter_integrity", PASS, interpreter.sha256))

    try:
        cfg = read_pinned(venv / "pyvenv.cfg", CONFIG_LIMIT, keep=True)
    except Rejected as exc:
        return stop("runtime_version", FAIL, str(exc))
    pinned.append(cfg)
    if cfg.sha256 != manifest["pyvenv_cfg"]["sha256"]:
        return stop("runtime_version", FAIL, "pyvenv.cfg hash mismatch")
    values = _parse_pyvenv_cfg(cfg.data.decode("utf-8", "replace"))
    version = values.get("version_info") or values.get("version")
    if version not in KNOWN_VERSIONS or version != manifest["python_version"]:
        return stop("runtime_version", FAIL, f"unknown runtime version: {version}")
    if values.get("include-system-site-packages", "false").lower() != "false":
        return stop("runtime_version", FAIL, "the venv includes the system site-packages")
    checks.append(Check("runtime_version", PASS, version))

    for name, base, table in (("libraries", runtime, manifest["libraries"]),
                              ("dependency_files", venv, manifest["files"])):
        mismatched = []
        for relative, expected in sorted(table.items()):
            try:
                item = read_pinned(_relative(base, relative))
            except Rejected as exc:
                mismatched.append(f"{relative}: {exc}")
                continue
            pinned.append(item)
            if item.sha256 != expected:
                mismatched.append(f"{relative}: hash mismatch")
        if mismatched:
            return stop(name, FAIL, "; ".join(mismatched))
        checks.append(Check(name, PASS, f"{len(table)} files"))

    directories = site_packages(venv) + site_packages(runtime)
    hooks = startup_hooks(directories)
    if hooks:
        return stop("startup_hooks", FAIL, f"unexpected startup hook: {hooks}")
    checks.append(Check("startup_hooks", PASS, f"none in {len(directories)} site directories"))

    found: dict[str, Pinned] = {}
    problems = []
    for directory in directories:
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".pth"):
                continue
            try:
                item = read_pinned(directory / entry, CONFIG_LIMIT, keep=True)
            except Rejected as exc:
                problems.append(str(exc))
                continue
            pinned.append(item)
            if entry in found:
                problems.append(f"{entry}: present in more than one site directory")
            found[entry] = item
    unknown = sorted(set(found) - set(manifest["pth"]))
    if unknown:
        problems.append(f"unknown .pth files: {unknown}")
    problems += [f"{name}: hash mismatch" for name in sorted(set(found) & set(manifest["pth"]))
                 if found[name].sha256 != manifest["pth"][name]]
    problems += [f"{name}: missing" for name in sorted(set(manifest["pth"]) - set(found))]
    if problems:
        return stop("pth_files", FAIL, "; ".join(problems))
    executable = sum(executable_pth_lines(item.data) for item in found.values())
    checks.append(Check("pth_files", PASS,
                        f"{len(found)} pinned .pth files, {executable} executable lines (they "
                        "still run at interpreter start-up; accepted limitation)"))

    injected = injected_variables(environ)
    if injected:
        return stop("environment", FAIL, f"unexpected environment variables: {injected}")
    checks.append(Check("environment", PASS, "no PYTHON*/DYLD_*/LD_*/NOTION_*/PIP_* variable"))

    changed = [item.path for item in pinned if not unchanged(item)]
    if changed:
        return stop("toctou", FAIL, f"changed after it was verified: {changed}")
    checks.append(Check("toctou", PASS, f"{len(pinned)} files unchanged at the end"))
    return BootstrapResult(overall(checks), checks, manifest)


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


def dotenv_disable_supported(venv: Path) -> Check:
    """Static check (nothing is imported or run) that the venv's python-dotenv honours
    PYTHON_DOTENV_DISABLED."""
    problems = []
    for packages in site_packages(venv):
        versions = sorted(p.name[len("python_dotenv-"):-len(".dist-info")]
                          for p in packages.glob("python_dotenv-*.dist-info"))
        if not versions:
            continue
        if versions[0] not in DOTENV_DISABLE_VERSIONS or len(versions) != 1:
            problems.append(f"python-dotenv {versions} not in {list(DOTENV_DISABLE_VERSIONS)}")
            continue
        try:
            main = read_pinned(packages / "dotenv" / "main.py", keep=True)
        except Rejected as exc:
            problems.append(str(exc))
            continue
        if b'"PYTHON_DOTENV_DISABLED"' not in main.data:
            problems.append("dotenv/main.py does not read PYTHON_DOTENV_DISABLED")
            continue
        return Check("dotenv_disabled", PASS, f"python-dotenv {versions[0]}")
    return Check("dotenv_disabled", FAIL, "; ".join(problems) or "python-dotenv not found")


def launch_environment(environ: dict[str, str], config: dict[str, Secret]) -> dict[str, Any]:
    if not environ.get("HOME") or not os.path.isabs(environ["HOME"]):
        raise Rejected("HOME is missing or not an absolute path")
    kept: dict[str, Any] = {k: v for k, v in environ.items() if k in ENVIRONMENT_KEEP}
    return {**kept, "PATH": os.pathsep.join(LAUNCH_PATH), "PYTHON_DOTENV_DISABLED": "1",
            **config}


def command_may_mutate(argv: list[str]) -> bool:
    """True unless the invocation cannot write to Notion (unknown commands may)."""
    if not argv or argv[0] not in CLI_COMMANDS:
        return True
    if argv[0] in READ_ONLY_COMMANDS:
        return False
    if argv[0] == "import":
        return "--dry-run" not in argv[1:]
    return any(arg.startswith("--apply") for arg in argv[1:])


REQUIRED_PREFLIGHT = ("bootstrap", "interpreter_explicit", "inherited_environment", "config",
                      "dotenv_disabled", "command")


@dataclass
class LaunchPlan:
    status: str
    checks: list[Check]
    argv: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict, repr=False)
    may_mutate: bool = True
    executable: bool = False

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
        return LaunchPlan(status, checks, argv or [], env or {}, command_may_mutate(cli_args),
                          executable=False)

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
    checks.append(dotenv_disable_supported(venv))
    if checks[-1].status != PASS:
        return finish()
    if not cli_args or cli_args[0] not in CLI_COMMANDS:
        checks.append(Check("command", FAIL, f"unknown command: {cli_args[:1]}"))
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
        (root / f".{SELECTOR}.tmp").unlink(missing_ok=True)
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
