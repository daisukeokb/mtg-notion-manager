"""WP-OGR-03 Case-A static Runtime fixture: plan / materialize / validate / fingerprint.

Purpose (WP-OGR-03 fixture tooling only -- NOT a production Generation Workspace
feature, NOT wired into cli.py or __main__.py, NOT reachable from the operational
CLI surface): builds a byte-exact, Human-frozen filesystem state representing
Case A -- an immediate crash after a successful begin_generation_transaction, before
any staging file is written. See the accepted WP-OGR-03 F1 "State-Fidelity" decision
package: Claim R (Recovery Inspection reads the state correctly) is proven by the
Runtime exercise this fixture is for; Claim M (that a real begin_generation_transaction
call produced these exact bytes) is deliberately not re-executed here -- this module
never calls bootstrap_generation_workspace, begin_generation_transaction,
commit_generation_transaction, apply_generation_transaction,
recover_generation_transaction, or inspect_generation_recovery. It does call the
read-only, unmodified resolve_active_generation()/verify_active_generation() production
verifiers (generation_workspace.resolver) from validate_case_a_fixture(), to
independently establish the builder-owned V1-V12+V15 pre-Runtime validation contract
against a materialized fixture, rather than trusting plan-byte-equality alone.

Every identity, timestamp, and the materialization root are Human-frozen literals
(no uuid4(), no datetime.now()/_now_iso()) -- see the frozen constants below.

Two materialization entry points, deliberately separated:
  - materialize_case_a_fixture(output_root): the production entry point. Always
    re-validates output_root against FROZEN_FIXTURE_ROOT before doing anything else,
    and rejects unconditionally otherwise. No parameter, flag, or code path exists to
    bypass this check.
  - _materialize_plan_at(output_root, plan): the underlying, root-agnostic write
    primitive. Not exposed as "the" production entry point; used directly by tests
    against pytest-managed ephemeral paths so the safety check above never has to be
    weakened or bypassed to make the writing logic itself testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .digest import SUPPORTED_DIGEST_SCHEMA_VERSIONS, _component_id_for, canonical_relative_path
from .inventory import MANIFEST_FILENAME, STATE_FILENAME
from .model import (
    POINTER_SCHEMA_VERSION,
    Component,
    GenerationDigestManifest,
    MalformedPointerError,
    Pointer,
    generation_directory_name,
    is_valid_uuid,
    parse_pointer,
)
from .mutation_guard import (
    AUTHORIZATION_SCHEMA_VERSION,
    LOCK_SCHEMA_VERSION,
    OPERATION_SCOPE_TRANSACTION,
    ExpectedFile,
    MutationAuthorization,
)
from .resolver import resolve_active_generation, verify_active_generation
from .transaction import CONTROL_TRANSACTION_PROTOCOL_VERSION

# --- Human-frozen contract (see WP-OGR-03 Final Static Fixture Profile & Path
# Freeze decision) -------------------------------------------------------------

FROZEN_FIXTURE_ROOT = Path("/private/tmp/mtg-notion-manager-wp-ogr-03-case-a-runtime-fixture-01")

BOOTSTRAP_GENERATION_ID = "0000000001"
CASE_A_TARGET_GENERATION_ID = "0000000002"

BOOTSTRAP_TRANSACTION_ID = "00000000-0000-0000-0000-000000000001"
CASE_A_TRANSACTION_ID = "00000000-0000-0000-0000-000000000002"
assert BOOTSTRAP_TRANSACTION_ID != CASE_A_TRANSACTION_ID  # required for MISMATCH binding

BOOTSTRAP_AUTHORIZATION_ID = "00000000-0000-0000-0000-0000000000a1"
CASE_A_AUTHORIZATION_ID = "00000000-0000-0000-0000-0000000000a2"

FROZEN_TIMESTAMP = "2026-01-01T00:00:00Z"

DIGEST_SCHEMA_VERSION = "WP-CLAIM-EXIT-GENERATION-DIGEST-v1"
assert DIGEST_SCHEMA_VERSION in SUPPORTED_DIGEST_SCHEMA_VERSIONS

SOURCE_01_NAME = "source_01_layer_1.jsonl"
SOURCE_02_NAME = "source_02_layer_2.jsonl"
CANDIDATE_NAME = "candidate_part_01_deliverables_01_06.md"

_SOURCE_FILE_CONTENT = b'{"record_type": "CHECKPOINT_METADATA"}\n'
_CANDIDATE_STATUS_LINE = "Candidate Part 1 Status: NON-CANONICAL / UNREVIEWED / NOT ACCEPTED"
_CANDIDATE_FILE_CONTENT = (
    "# Test Candidate\n\nSome content.\n\n" + _CANDIDATE_STATUS_LINE + "\n"
).encode("utf-8")

STAGING_DIRNAME = f".staging-gen-{CASE_A_TARGET_GENERATION_ID}"

# --- Typed materialization failure reason codes (WP-OGR-03-BLD-FINDING-01/02 repair) --
REASON_ROOT_IS_SYMLINK = "ROOT_IS_SYMLINK"
REASON_ROOT_ALREADY_EXISTS = "ROOT_ALREADY_EXISTS"
REASON_WRONG_ROOT = "WRONG_ROOT"
REASON_UNSAFE_PLAN_PATH = "UNSAFE_PLAN_PATH"
REASON_MATERIALIZATION_IO_ERROR = "MATERIALIZATION_IO_ERROR"

# Mirrors recovery_inspection._LOCK_REQUIRED_KEYS by value, deliberately not imported --
# validate_case_a_fixture() must not depend on the recovery_inspection module at all
# (see BLD-14's own import-boundary check), even for a shared read-only constant.
_LOCK_REQUIRED_KEYS: tuple[str, ...] = (
    "lock_schema_version",
    "lock_id",
    "transaction_id",
    "authorization_id",
    "authorization_digest",
    "created_at_utc",
)


def _is_safe_relative_path(relative: str) -> bool:
    """Deterministic, filesystem-independent: rejects anything that is empty,
    absolute, ".", "..", or contains a ".." component -- i.e. anything that could
    lexically escape whatever root it is later joined to. Does not touch disk.
    """
    if not relative or relative in (".", ".."):
        return False
    pure = PurePosixPath(relative)
    if pure.is_absolute():
        return False
    return not any(part in ("", ".", "..") for part in pure.parts)


def _is_safe_leaf_filename(name: str) -> bool:
    """Stronger constraint for generation file names specifically: a single path
    segment, no directory separators of any kind, in addition to the general
    relative-path safety rule.
    """
    return _is_safe_relative_path(name) and "/" not in name and "\\" not in name


def _validate_plan_paths(plan: CaseAFixturePlan) -> str | None:
    """Returns None if every plan-supplied relative path is safe to materialize,
    else a precise UNSAFE_PLAN_PATH reason string. Called before any filesystem
    mutation -- unsafe input must never reach mkdir()/open().
    """
    for filename, _content in plan.generation_files:
        if not _is_safe_leaf_filename(filename):
            return (
                f"{REASON_UNSAFE_PLAN_PATH}: generation file name is not a safe "
                f"leaf filename: {filename!r}"
            )
    for reldir in plan.empty_directories:
        if not _is_safe_relative_path(reldir):
            return f"{REASON_UNSAFE_PLAN_PATH}: directory path is not safe: {reldir!r}"
    return None


@dataclass(frozen=True)
class CaseAFixturePlan:
    generation_files: tuple[tuple[str, bytes], ...]  # (filename, content), canonical order
    generation_digest: str
    pointer_bytes: bytes
    control_transaction_bytes: bytes
    lock_bytes: bytes
    authorization_digest: str
    empty_directories: tuple[str, ...]  # relative to fixture root


def _manifest_and_state_bytes() -> tuple[bytes, bytes]:
    file_classification: dict[str, str] = {
        SOURCE_01_NAME: "SOURCE_CHECKPOINT",
        SOURCE_02_NAME: "SOURCE_CHECKPOINT",
        CANDIDATE_NAME: "CANDIDATE_PART",
    }
    source_checkpoint_files: list[str] = [SOURCE_01_NAME, SOURCE_02_NAME]

    manifest_obj: dict[str, object] = {
        "workspace_version": "TEST-v1",
        "file_classification": file_classification,
        "completed_stage_files": [SOURCE_01_NAME, SOURCE_02_NAME, CANDIDATE_NAME],
        "last_control_action": "CTL-INIT-01",
        "last_control_transaction_id": BOOTSTRAP_TRANSACTION_ID,
    }
    state_obj: dict[str, object] = {
        "source_checkpoint_files": source_checkpoint_files,
        "last_control_action": "CTL-INIT-01",
        "last_control_transaction_id": BOOTSTRAP_TRANSACTION_ID,
    }
    # In-memory self-check: manifest/state source declarations must agree, mirroring
    # derive_expected_inventory's own consistency rule (inventory.py), without needing
    # any filesystem read to check it.
    manifest_sources = sorted(
        name for name, kind in file_classification.items() if kind == "SOURCE_CHECKPOINT"
    )
    assert manifest_sources == sorted(source_checkpoint_files)

    manifest_bytes = (json.dumps(manifest_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    state_bytes = (json.dumps(state_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return manifest_bytes, state_bytes


def build_case_a_fixture_plan() -> CaseAFixturePlan:
    """Pure: no filesystem I/O, no clock read, no UUID generation. Deterministic --
    calling this twice yields two equal CaseAFixturePlan values.
    """
    manifest_bytes, state_bytes = _manifest_and_state_bytes()

    # Canonical inventory order per derive_expected_inventory's documented rule
    # (inventory.py): sources ascending, then candidate, then manifest, then state.
    generation_files: tuple[tuple[str, bytes], ...] = (
        (SOURCE_01_NAME, _SOURCE_FILE_CONTENT),
        (SOURCE_02_NAME, _SOURCE_FILE_CONTENT),
        (CANDIDATE_NAME, _CANDIDATE_FILE_CONTENT),
        (MANIFEST_FILENAME, manifest_bytes),
        (STATE_FILENAME, state_bytes),
    )

    components = [
        Component(
            component_id=_component_id_for(filename),
            relative_path=canonical_relative_path(BOOTSTRAP_GENERATION_ID, filename),
            byte_count=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for filename, content in generation_files
    ]
    digest_manifest = GenerationDigestManifest(
        digest_schema_version=DIGEST_SCHEMA_VERSION,
        generation_id=BOOTSTRAP_GENERATION_ID,
        components=components,
    )
    generation_digest = hashlib.sha256(digest_manifest.to_canonical_bytes()).hexdigest()

    pointer = Pointer(
        pointer_schema_version=POINTER_SCHEMA_VERSION,
        generation_id=BOOTSTRAP_GENERATION_ID,
        generation_digest_schema_version=DIGEST_SCHEMA_VERSION,
        generation_digest=generation_digest,
        transaction_id=BOOTSTRAP_TRANSACTION_ID,
    )
    pointer.validate()  # raises MalformedPointerError if any field is invalid
    pointer_bytes = pointer.to_canonical_bytes()

    expected_files = [
        ExpectedFile(relative_path=filename, sha256=hashlib.sha256(content).hexdigest())
        for filename, content in generation_files
    ]
    authorization = MutationAuthorization(
        authorization_schema_version=AUTHORIZATION_SCHEMA_VERSION,
        authorization_id=CASE_A_AUTHORIZATION_ID,
        operation_scope=OPERATION_SCOPE_TRANSACTION,
        transaction_id=CASE_A_TRANSACTION_ID,
        workspace_root=str(FROZEN_FIXTURE_ROOT),
        apply=True,
        source_generation_id=BOOTSTRAP_GENERATION_ID,
        source_generation_digest=generation_digest,
        target_generation_id=CASE_A_TARGET_GENERATION_ID,
        expected_stable_file_count=len(expected_files),
        expected_files=expected_files,
    )
    authorization_digest = authorization.digest()

    control_transaction_obj = {
        "protocol_version": CONTROL_TRANSACTION_PROTOCOL_VERSION,
        "transaction_id": CASE_A_TRANSACTION_ID,
        "state": "PREPARING",
        "source_generation": BOOTSTRAP_GENERATION_ID,
        "target_generation": CASE_A_TARGET_GENERATION_ID,
        "target_generation_digest": None,
        "authorization_id": CASE_A_AUTHORIZATION_ID,
        "authorization_digest": authorization_digest,
        "operation_scope": OPERATION_SCOPE_TRANSACTION,
        "created_at_utc": FROZEN_TIMESTAMP,
        "updated_at_utc": FROZEN_TIMESTAMP,
    }
    control_transaction_bytes = (
        json.dumps(control_transaction_obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")

    lock_obj = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "lock_id": CASE_A_AUTHORIZATION_ID,  # production convention: lock_id == authorization_id
        "transaction_id": CASE_A_TRANSACTION_ID,
        "authorization_id": CASE_A_AUTHORIZATION_ID,
        "authorization_digest": authorization_digest,
        "created_at_utc": FROZEN_TIMESTAMP,
    }
    lock_bytes = (json.dumps(lock_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    return CaseAFixturePlan(
        generation_files=generation_files,
        generation_digest=generation_digest,
        pointer_bytes=pointer_bytes,
        control_transaction_bytes=control_transaction_bytes,
        lock_bytes=lock_bytes,
        authorization_digest=authorization_digest,
        empty_directories=(STAGING_DIRNAME,),
    )


@dataclass(frozen=True)
class MaterializeResult:
    ok: bool
    output_root: Path | None = None
    reason: str = ""
    reason_code: str | None = None


def _materialize_plan_at(output_root: Path, plan: CaseAFixturePlan) -> MaterializeResult:
    """Root-agnostic write primitive. Fail-closed: refuses a symlink or an
    already-existing root, and refuses any unsafe plan-supplied relative path --
    all before any filesystem mutation. Never overwrites (every write uses
    exclusive "xb" creation). Never cleans up on failure: a mid-write OSError is
    caught and reported as a typed MaterializeResult(ok=False, ...) rather than
    raised, but whatever was already written is deliberately left in place for
    Human disposition -- automatic cleanup, rollback, and retry are prohibited by
    design (see WP-OGR-03-BLD-FINDING-02 repair). A retry against the same root
    then naturally fails closed via the "root already exists" check above.
    Not the production entry point -- callers needing the frozen-root guarantee
    must use materialize_case_a_fixture() instead.
    """
    if output_root.is_symlink():
        return MaterializeResult(
            ok=False, reason="output root must not be a symlink", reason_code=REASON_ROOT_IS_SYMLINK
        )
    if output_root.exists():
        return MaterializeResult(
            ok=False, reason="output root already exists", reason_code=REASON_ROOT_ALREADY_EXISTS
        )

    unsafe_reason = _validate_plan_paths(plan)
    if unsafe_reason is not None:
        return MaterializeResult(
            ok=False, reason=unsafe_reason, reason_code=REASON_UNSAFE_PLAN_PATH
        )

    try:
        gen_dir = output_root / "generations" / generation_directory_name(BOOTSTRAP_GENERATION_ID)
        gen_dir.mkdir(parents=True)

        for filename, content in plan.generation_files:
            with open(gen_dir / filename, "xb") as f:
                f.write(content)

        for reldir in plan.empty_directories:
            (output_root / reldir).mkdir(parents=True)

        with open(output_root / "active_generation", "xb") as f:
            f.write(plan.pointer_bytes)
        with open(output_root / ".control_transaction.json", "xb") as f:
            f.write(plan.control_transaction_bytes)
        with open(output_root / ".execution_lock.json", "xb") as f:
            f.write(plan.lock_bytes)
    except OSError as exc:
        # Whatever was already written above remains on disk untouched -- no
        # cleanup, no rollback, no retry here. output_root is reported so the
        # partial state is discoverable for Human disposition.
        return MaterializeResult(
            ok=False,
            output_root=output_root,
            reason=f"materialization failed: {exc}",
            reason_code=REASON_MATERIALIZATION_IO_ERROR,
        )

    return MaterializeResult(ok=True, output_root=output_root)


def materialize_case_a_fixture(output_root: Path) -> MaterializeResult:
    """Production entry point. Unconditionally rejects any output_root other than
    FROZEN_FIXTURE_ROOT -- there is no parameter or flag to bypass this.
    """
    if output_root != FROZEN_FIXTURE_ROOT:
        return MaterializeResult(
            ok=False,
            reason=f"materialization root must be exactly {FROZEN_FIXTURE_ROOT}",
            reason_code=REASON_WRONG_ROOT,
        )
    return _materialize_plan_at(output_root, build_case_a_fixture_plan())


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    problems: tuple[str, ...] = ()


def validate_case_a_fixture(
    output_root: Path, plan: CaseAFixturePlan | None = None
) -> ValidationResult:
    """Read-only. Never mutates output_root.

    Independently establishes the Human-owned builder pre-Runtime validation
    contract (WP-OGR-03 static fixture decision package): V1-V3 (tree shape /
    presence / forbidden-absence, plus exact-tree-identity / no-unexpected-entries)
    are checked structurally below. V4-V7 (JSON parse validity, required
    fields/types, UUID validity, ID relationships) are established by
    independently re-parsing the materialized files from disk -- not inferred
    from plan-byte-equality alone. V8-V10 (source-generation structural
    verification) are established by invoking the real, unmodified
    resolve_active_generation()/verify_active_generation() read-only production
    verifiers (generation_workspace.resolver) against the materialized tree --
    the same functions WP-OGR-01's own canonical read path depends on, not a
    second implementation of their logic. V11-V12 (control-transaction/lock
    semantic consistency) are established by independently re-parsing and
    field-checking the materialized JSON. V13 (recover_generation_transaction)
    and V14 (inspect_generation_recovery) are deliberately never called here --
    those, and V16 (Human fixture acceptance), remain separate, later Human
    Gates. V15 (fingerprint) is intentionally a separate function; see
    fingerprint_case_a_fixture().
    """
    plan = plan if plan is not None else build_case_a_fixture_plan()
    problems: list[str] = []

    gen_dir = output_root / "generations" / generation_directory_name(BOOTSTRAP_GENERATION_ID)

    # --- V2: required generation files/directories present, byte-exact to the plan
    for filename, content in plan.generation_files:
        p = gen_dir / filename
        if p.is_symlink() or not p.is_file():
            problems.append(f"missing or invalid generation file: {filename}")
        elif p.read_bytes() != content:
            problems.append(f"content mismatch: {filename}")

    for reldir in plan.empty_directories:
        d = output_root / reldir
        if not d.is_dir() or d.is_symlink():
            problems.append(f"missing or invalid directory: {reldir}")
        elif any(d.iterdir()):
            problems.append(f"directory not empty: {reldir}")

    root_files = {
        "active_generation": plan.pointer_bytes,
        ".control_transaction.json": plan.control_transaction_bytes,
        ".execution_lock.json": plan.lock_bytes,
    }
    for relname, expected in root_files.items():
        p = output_root / relname
        if p.is_symlink() or not p.is_file():
            problems.append(f"missing or invalid file: {relname}")
        elif p.read_bytes() != expected:
            problems.append(f"content mismatch: {relname}")

    # --- V3: forbidden paths absent
    forbidden = (
        output_root / "generations" / generation_directory_name(CASE_A_TARGET_GENERATION_ID),
        output_root / "active_generation.tmp",
        output_root / ".control_transaction.json.tmp",
    )
    for p in forbidden:
        if p.exists() or p.is_symlink():
            problems.append(f"forbidden path present: {p}")

    # --- V1: exact tree identity -- no unexpected entries beyond the accepted contract
    expected_relative_paths = {
        "generations",
        f"generations/{generation_directory_name(BOOTSTRAP_GENERATION_ID)}",
        *(
            f"generations/{generation_directory_name(BOOTSTRAP_GENERATION_ID)}/{name}"
            for name, _content in plan.generation_files
        ),
        *plan.empty_directories,
        *root_files.keys(),
    }
    if output_root.is_dir():
        actual_relative_paths = {str(p.relative_to(output_root)) for p in output_root.rglob("*")}
        unexpected = actual_relative_paths - expected_relative_paths
        if unexpected:
            problems.append(f"unexpected fixture entries present: {sorted(unexpected)}")

    if problems:
        # Structural problems already found -- the semantic/production-verifier
        # checks below assume an at-least-structurally-intact tree, so stop here
        # rather than raising a confusing secondary error against a broken one.
        return ValidationResult(ok=False, problems=tuple(problems))

    # --- V4/V6/V7: pointer -- independently re-parsed via the real parse_pointer()
    # (which performs its own full field/UUID/format validation), not inferred from
    # byte-equality to the plan.
    try:
        pointer = parse_pointer((output_root / "active_generation").read_bytes())
    except MalformedPointerError as exc:
        problems.append(f"active_generation failed independent parse/validate: {exc}")
        return ValidationResult(ok=False, problems=tuple(problems))
    if pointer.transaction_id != BOOTSTRAP_TRANSACTION_ID:
        problems.append("pointer transaction_id does not match the frozen bootstrap transaction_id")

    # --- V4/V6/V7/V11: control transaction -- independently re-parsed JSON
    try:
        control = json.loads((output_root / ".control_transaction.json").read_bytes())
    except json.JSONDecodeError as exc:
        problems.append(f".control_transaction.json failed independent JSON parse: {exc}")
        return ValidationResult(ok=False, problems=tuple(problems))

    expected_control = {
        "protocol_version": CONTROL_TRANSACTION_PROTOCOL_VERSION,
        "transaction_id": CASE_A_TRANSACTION_ID,
        "state": "PREPARING",
        "source_generation": BOOTSTRAP_GENERATION_ID,
        "target_generation": CASE_A_TARGET_GENERATION_ID,
        "target_generation_digest": None,
        "authorization_id": CASE_A_AUTHORIZATION_ID,
        "operation_scope": OPERATION_SCOPE_TRANSACTION,
        "created_at_utc": FROZEN_TIMESTAMP,
        "updated_at_utc": FROZEN_TIMESTAMP,
    }
    for key, expected_value in expected_control.items():
        if control.get(key) != expected_value:
            problems.append(
                f".control_transaction.json field {key!r} does not match expected value"
            )
    if control.get("authorization_digest") != plan.authorization_digest:
        problems.append(
            ".control_transaction.json authorization_digest does not match "
            "the plan's derived authorization_digest"
        )
    if not is_valid_uuid(str(control.get("transaction_id", ""))):
        problems.append("control transaction_id is not a valid UUID")
    if not is_valid_uuid(str(control.get("authorization_id", ""))):
        problems.append("control authorization_id is not a valid UUID")
    if control.get("transaction_id") == pointer.transaction_id:
        problems.append(
            "control transaction_id unexpectedly equals pointer transaction_id "
            "(MISMATCH binding is required for Case A)"
        )

    # --- V6/V7/V12: execution lock -- independently re-parsed JSON
    try:
        lock = json.loads((output_root / ".execution_lock.json").read_bytes())
    except json.JSONDecodeError as exc:
        problems.append(f".execution_lock.json failed independent JSON parse: {exc}")
        return ValidationResult(ok=False, problems=tuple(problems))

    for key in _LOCK_REQUIRED_KEYS:
        if key not in lock or not isinstance(lock[key], str):
            problems.append(f".execution_lock.json missing or non-string required key: {key}")
    if lock.get("lock_schema_version") != LOCK_SCHEMA_VERSION:
        problems.append(".execution_lock.json lock_schema_version does not match expected value")
    if lock.get("lock_id") != CASE_A_AUTHORIZATION_ID:
        problems.append("lock_id does not match the frozen Case-A authorization_id")
    if lock.get("transaction_id") != CASE_A_TRANSACTION_ID:
        problems.append("lock transaction_id does not match the frozen Case-A transaction_id")
    if lock.get("authorization_digest") != plan.authorization_digest:
        problems.append(
            ".execution_lock.json authorization_digest does not match "
            "the plan's derived authorization_digest"
        )
    if not is_valid_uuid(str(lock.get("transaction_id", ""))):
        problems.append("lock transaction_id is not a valid UUID")
    if not is_valid_uuid(str(lock.get("lock_id", ""))):
        problems.append("lock lock_id is not a valid UUID")

    # --- V5: manifest/state required fields present
    try:
        manifest = json.loads((gen_dir / MANIFEST_FILENAME).read_bytes())
        state = json.loads((gen_dir / STATE_FILENAME).read_bytes())
    except json.JSONDecodeError as exc:
        problems.append(f"manifest/state failed independent JSON parse: {exc}")
        return ValidationResult(ok=False, problems=tuple(problems))
    if not isinstance(manifest.get("file_classification"), dict):
        problems.append("workspace_manifest.json missing or invalid file_classification")
    if not isinstance(state.get("source_checkpoint_files"), list):
        problems.append("workspace_state.json missing or invalid source_checkpoint_files")

    if problems:
        return ValidationResult(ok=False, problems=tuple(problems))

    # --- V8/V9/V10: real, unmodified production read-only verifier path. This is
    # the load-bearing independent check: it recomputes the generation digest and
    # re-derives inventory/candidate-status-line/manifest-state consistency from
    # the materialized bytes on disk, entirely independent of whatever the plan
    # itself claims -- closing WP-OGR-03-BLD-FINDING-03.
    resolved = resolve_active_generation(output_root)
    if not resolved.ok:
        problems.append(f"resolve_active_generation failed: {resolved.reason}")
        return ValidationResult(ok=False, problems=tuple(problems))

    verified = verify_active_generation(output_root)
    if not verified.ok:
        problems.append(f"verify_active_generation failed: {verified.reason}")
        return ValidationResult(ok=False, problems=tuple(problems))
    if verified.actual_digest != plan.generation_digest:
        problems.append(
            "verify_active_generation's recomputed digest does not match "
            "the plan's generation_digest"
        )

    return ValidationResult(ok=not problems, problems=tuple(problems))


FingerprintEntry = tuple[str, str, "int | None", "str | None"]


def fingerprint_case_a_fixture(output_root: Path) -> tuple[FingerprintEntry, ...]:
    """Read-only. (relative_path, object_type, byte_length, sha256) tuples,
    lexicographically path-sorted. Directories carry (path, "directory", None, None).
    """
    entries: list[FingerprintEntry] = []
    for p in sorted(output_root.rglob("*"), key=lambda x: str(x.relative_to(output_root))):
        rel = str(p.relative_to(output_root))
        if p.is_symlink():
            entries.append((rel, "symlink", None, None))
        elif p.is_dir():
            entries.append((rel, "directory", None, None))
        elif p.is_file():
            data = p.read_bytes()
            entries.append((rel, "file", len(data), hashlib.sha256(data).hexdigest()))
    return tuple(entries)
