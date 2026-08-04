"""Production Mutation Guard.

Every mutating entry point (bootstrap_generation_workspace,
begin_generation_transaction, commit_generation_transaction) must go
through this module before touching the filesystem. Read-only APIs
(resolve_active_generation, verify_active_generation,
recover_generation_transaction, compute_generation_digest) never call this
module and never require an authorization.

No environment-variable bypass exists anywhere in this module.
"""

from __future__ import annotations

import errno
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .model import is_valid_generation_id, is_valid_sha256, is_valid_uuid

AUTHORIZATION_SCHEMA_VERSION = "WP-CLAIM-EXIT-MUTATION-AUTHORIZATION-v1"
LOCK_SCHEMA_VERSION = "WP-CLAIM-EXIT-LOCK-v1"

OPERATION_SCOPE_BOOTSTRAP = "BOOTSTRAP_GENERATION_WORKSPACE"
OPERATION_SCOPE_TRANSACTION = "GENERATION_TRANSACTION"
VALID_OPERATION_SCOPES = {OPERATION_SCOPE_BOOTSTRAP, OPERATION_SCOPE_TRANSACTION}

_CANONICAL_SEPARATORS = (",", ":")

_REQUIRED_AUTH_FIELDS = [
    "authorization_schema_version",
    "authorization_id",
    "operation_scope",
    "transaction_id",
    "workspace_root",
    "apply",
    "source_generation_id",
    "source_generation_digest",
    "target_generation_id",
    "expected_stable_file_count",
    "expected_files",
]

# --- Error codes -----------------------------------------------------------

MUTATION_AUTHORIZATION_REQUIRED = "MUTATION_AUTHORIZATION_REQUIRED"
AUTHORIZATION_SCHEMA_UNSUPPORTED = "AUTHORIZATION_SCHEMA_UNSUPPORTED"
AUTHORIZATION_SCHEMA_INVALID = "AUTHORIZATION_SCHEMA_INVALID"
AUTHORIZATION_FIELD_MISSING = "AUTHORIZATION_FIELD_MISSING"
AUTHORIZATION_UNKNOWN_FIELD = "AUTHORIZATION_UNKNOWN_FIELD"
AUTHORIZATION_APPLY_REQUIRED = "AUTHORIZATION_APPLY_REQUIRED"
AUTHORIZATION_OPERATION_MISMATCH = "AUTHORIZATION_OPERATION_MISMATCH"
AUTHORIZATION_TRANSACTION_MISMATCH = "AUTHORIZATION_TRANSACTION_MISMATCH"
AUTHORIZATION_WORKSPACE_MISMATCH = "AUTHORIZATION_WORKSPACE_MISMATCH"
AUTHORIZATION_DIGEST_MISMATCH = "AUTHORIZATION_DIGEST_MISMATCH"
AUTHORIZATION_BASELINE_COUNT_MISMATCH = "AUTHORIZATION_BASELINE_COUNT_MISMATCH"
AUTHORIZATION_BASELINE_INVENTORY_MISMATCH = "AUTHORIZATION_BASELINE_INVENTORY_MISMATCH"
AUTHORIZATION_BASELINE_SHA_MISMATCH = "AUTHORIZATION_BASELINE_SHA_MISMATCH"
AUTHORIZATION_UNSAFE_PATH = "AUTHORIZATION_UNSAFE_PATH"
AUTHORIZATION_DUPLICATE_PATH = "AUTHORIZATION_DUPLICATE_PATH"
ROOT_POLICY_REJECTED = "ROOT_POLICY_REJECTED"
ROOT_SYMLINK_REJECTED = "ROOT_SYMLINK_REJECTED"
AUTHORIZATION_FILE_REJECTED = "AUTHORIZATION_FILE_REJECTED"
LOCK_ALREADY_EXISTS = "LOCK_ALREADY_EXISTS"
LOCK_OWNERSHIP_MISMATCH = "LOCK_OWNERSHIP_MISMATCH"
TARGET_GENERATION_ALREADY_EXISTS = "TARGET_GENERATION_ALREADY_EXISTS"
SOURCE_GENERATION_MISMATCH = "SOURCE_GENERATION_MISMATCH"
FAILED_REQUIRES_RECOVERY = "FAILED_REQUIRES_RECOVERY"


@dataclass(frozen=True)
class GuardResult:
    success: bool
    error_code: str | None = None
    message: str = ""
    filesystem_writes: int = 0
    authorization_id: str | None = None
    transaction_id: str | None = None


class GuardRejection(Exception):
    """Internal control-flow exception carrying a GuardResult failure."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# --- Authorization model ----------------------------------------------------


@dataclass(frozen=True)
class ExpectedFile:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class MutationAuthorization:
    authorization_schema_version: str
    authorization_id: str
    operation_scope: str
    transaction_id: str
    workspace_root: str
    apply: bool
    source_generation_id: str | None
    source_generation_digest: str | None
    target_generation_id: str
    expected_stable_file_count: int
    expected_files: list = field(default_factory=list)  # list[ExpectedFile]

    def canonical_bytes(self) -> bytes:
        files_sorted = sorted(self.expected_files, key=lambda f: f.relative_path)
        obj = {
            "authorization_schema_version": self.authorization_schema_version,
            "authorization_id": self.authorization_id,
            "operation_scope": self.operation_scope,
            "transaction_id": self.transaction_id,
            "workspace_root": self.workspace_root,
            "apply": self.apply,
            "source_generation_id": self.source_generation_id,
            "source_generation_digest": self.source_generation_digest,
            "target_generation_id": self.target_generation_id,
            "expected_stable_file_count": self.expected_stable_file_count,
            "expected_files": [
                {"relative_path": f.relative_path, "sha256": f.sha256} for f in files_sorted
            ],
        }
        return json.dumps(
            obj, ensure_ascii=False, sort_keys=False, separators=_CANONICAL_SEPARATORS
        ).encode("utf-8")

    def digest(self) -> str:
        import hashlib

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def parse_authorization_obj(obj: dict) -> MutationAuthorization:
    if not isinstance(obj, dict):
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "authorization must be a JSON object")

    unknown = set(obj.keys()) - set(_REQUIRED_AUTH_FIELDS)
    if unknown:
        raise GuardRejection(
            AUTHORIZATION_UNKNOWN_FIELD, f"unknown authorization field(s): {sorted(unknown)}"
        )
    missing = [k for k in _REQUIRED_AUTH_FIELDS if k not in obj]
    if missing:
        raise GuardRejection(
            AUTHORIZATION_FIELD_MISSING, f"missing authorization field(s): {missing}"
        )

    if obj["authorization_schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
        raise GuardRejection(
            AUTHORIZATION_SCHEMA_UNSUPPORTED,
            f"unsupported authorization_schema_version: {obj['authorization_schema_version']!r}",
        )
    if not is_valid_uuid(str(obj.get("authorization_id", ""))):
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "invalid authorization_id")
    if obj["operation_scope"] not in VALID_OPERATION_SCOPES:
        raise GuardRejection(
            AUTHORIZATION_SCHEMA_INVALID, f"invalid operation_scope: {obj['operation_scope']!r}"
        )
    if not is_valid_uuid(str(obj.get("transaction_id", ""))):
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "invalid transaction_id")
    if not isinstance(obj["workspace_root"], str) or not obj["workspace_root"]:
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "invalid workspace_root")
    if not isinstance(obj["apply"], bool):
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "apply must be a boolean")
    if not isinstance(obj["expected_stable_file_count"], int):
        raise GuardRejection(
            AUTHORIZATION_SCHEMA_INVALID, "expected_stable_file_count must be an integer"
        )
    if not isinstance(obj["expected_files"], list):
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "expected_files must be a list")

    expected_files = []
    seen_paths = set()
    for entry in obj["expected_files"]:
        if not isinstance(entry, dict) or set(entry.keys()) != {"relative_path", "sha256"}:
            raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "malformed expected_files entry")
        rel = entry["relative_path"]
        sha = entry["sha256"]
        if not isinstance(rel, str) or not isinstance(sha, str) or not is_valid_sha256(sha):
            raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "malformed expected_files entry")
        if rel.startswith("/") or rel.startswith("..") or "/../" in rel or "\\" in rel:
            raise GuardRejection(AUTHORIZATION_UNSAFE_PATH, f"unsafe relative_path: {rel!r}")
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise GuardRejection(AUTHORIZATION_UNSAFE_PATH, f"unsafe relative_path: {rel!r}")
        if rel in seen_paths:
            raise GuardRejection(AUTHORIZATION_DUPLICATE_PATH, f"duplicate relative_path: {rel!r}")
        seen_paths.add(rel)
        expected_files.append(ExpectedFile(relative_path=rel, sha256=sha))

    return MutationAuthorization(
        authorization_schema_version=obj["authorization_schema_version"],
        authorization_id=obj["authorization_id"],
        operation_scope=obj["operation_scope"],
        transaction_id=obj["transaction_id"],
        workspace_root=obj["workspace_root"],
        apply=obj["apply"],
        source_generation_id=obj.get("source_generation_id"),
        source_generation_digest=obj.get("source_generation_digest"),
        target_generation_id=obj["target_generation_id"],
        expected_stable_file_count=obj["expected_stable_file_count"],
        expected_files=expected_files,
    )


# --- Authorization Artifact Policy (external file) --------------------------


def load_authorization_file(
    path: Path, repo_root: Path, workspace_root: Path
) -> MutationAuthorization:
    if not path.exists():
        raise GuardRejection(AUTHORIZATION_FILE_REJECTED, "authorization file does not exist")
    if path.is_symlink():
        raise GuardRejection(
            AUTHORIZATION_FILE_REJECTED, "authorization file must not be a symlink"
        )
    if not path.is_file():
        raise GuardRejection(
            AUTHORIZATION_FILE_REJECTED, "authorization path is not a regular file"
        )

    resolved = path.resolve(strict=True)
    for ancestor in (repo_root, workspace_root):
        try:
            resolved.relative_to(ancestor.resolve(strict=True))
        except (ValueError, FileNotFoundError):
            continue
        else:
            raise GuardRejection(
                AUTHORIZATION_FILE_REJECTED,
                "authorization file must not live under the repository or the workspace",
            )

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardRejection(
            AUTHORIZATION_FILE_REJECTED, f"authorization file unreadable: {exc}"
        ) from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuardRejection(
            AUTHORIZATION_SCHEMA_INVALID, "authorization file is not valid JSON"
        ) from exc

    return parse_authorization_obj(obj)


# --- Root Policy -------------------------------------------------------------


def find_repository_root(start: Path) -> Path:
    current = start.resolve()
    candidates = [current, *current.parents]
    found = [p for p in candidates if (p / ".git").exists()]
    if len(found) != 1:
        raise GuardRejection(
            ROOT_POLICY_REJECTED, "repository root could not be uniquely determined"
        )
    return found[0]


def enforce_root_policy(workspace_root_arg: str, authorization: MutationAuthorization) -> Path:
    raw = Path(workspace_root_arg)
    if raw.is_symlink():
        raise GuardRejection(ROOT_SYMLINK_REJECTED, "workspace_root must not be a symlink")
    if not raw.is_absolute():
        raise GuardRejection(ROOT_POLICY_REJECTED, "workspace_root must be an absolute path")

    try:
        real_root = raw.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise GuardRejection(ROOT_POLICY_REJECTED, f"workspace_root does not exist: {exc}") from exc
    if not real_root.is_dir():
        raise GuardRejection(ROOT_POLICY_REJECTED, "workspace_root is not a directory")

    if real_root == Path(real_root.anchor):
        raise GuardRejection(ROOT_POLICY_REJECTED, "workspace_root must not be the filesystem root")
    if real_root == Path.home().resolve():
        raise GuardRejection(
            ROOT_POLICY_REJECTED, "workspace_root must not be the user home directory"
        )

    repo_root = find_repository_root(Path(__file__))
    if real_root == repo_root or real_root == repo_root.parent:
        raise GuardRejection(
            ROOT_POLICY_REJECTED, "workspace_root must not be the repository root or its parent"
        )
    try:
        real_root.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise GuardRejection(
            ROOT_POLICY_REJECTED, "workspace_root must not be under the repository"
        )

    try:
        auth_root = Path(authorization.workspace_root).resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise GuardRejection(
            AUTHORIZATION_WORKSPACE_MISMATCH, f"authorization workspace_root invalid: {exc}"
        ) from exc
    if auth_root != real_root:
        raise GuardRejection(
            AUTHORIZATION_WORKSPACE_MISMATCH,
            "authorization workspace_root does not match the resolved workspace_root",
        )

    return real_root


# --- Phase A: no-write validation --------------------------------------------


def validate_phase_a(
    authorization: MutationAuthorization | None,
    *,
    expected_operation_scope: str,
    workspace_root_arg: str,
    transaction_id_arg: str,
) -> Path:
    """Raises GuardRejection on any failure. Never touches the filesystem
    except for read-only stat/resolve calls. Returns the resolved,
    policy-approved workspace root on success.
    """
    if authorization is None:
        raise GuardRejection(MUTATION_AUTHORIZATION_REQUIRED, "mutation_authorization is required")

    if authorization.operation_scope != expected_operation_scope:
        raise GuardRejection(
            AUTHORIZATION_OPERATION_MISMATCH,
            f"authorization operation_scope {authorization.operation_scope!r} != "
            f"{expected_operation_scope!r}",
        )
    if not authorization.apply:
        raise GuardRejection(AUTHORIZATION_APPLY_REQUIRED, "authorization.apply must be true")
    if authorization.transaction_id != transaction_id_arg:
        raise GuardRejection(
            AUTHORIZATION_TRANSACTION_MISMATCH, "authorization transaction_id does not match"
        )
    if not is_valid_generation_id(authorization.target_generation_id):
        raise GuardRejection(AUTHORIZATION_SCHEMA_INVALID, "invalid target_generation_id")

    real_root = enforce_root_policy(workspace_root_arg, authorization)

    if authorization.expected_stable_file_count != len(authorization.expected_files):
        raise GuardRejection(
            AUTHORIZATION_BASELINE_COUNT_MISMATCH,
            "expected_stable_file_count does not match the number of expected_files entries",
        )

    return real_root


# --- Lock acquisition (exclusive create) -------------------------------------


def acquire_lock(
    workspace_root: Path,
    *,
    lock_filename: str,
    transaction_id: str,
    authorization_id: str,
    authorization_digest: str,
) -> dict:
    lock_path = workspace_root / lock_filename
    lock_id = authorization_id  # one authorization -> one lock instance
    payload = {
        "lock_schema_version": LOCK_SCHEMA_VERSION,
        "lock_id": lock_id,
        "transaction_id": transaction_id,
        "authorization_id": authorization_id,
        "authorization_digest": authorization_digest,
        "created_at_utc": _now_iso(),
    }
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise GuardRejection(LOCK_ALREADY_EXISTS, "execution lock already held") from exc
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise GuardRejection(LOCK_ALREADY_EXISTS, "execution lock already held") from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


@dataclass(frozen=True)
class LockReleaseResult:
    released: bool
    # error_code is None when there was simply nothing to release (lock
    # already absent) — that is not itself an ownership problem. It is
    # LOCK_OWNERSHIP_MISMATCH when a lock file exists but does not belong
    # to the caller (malformed, or owned by a different transaction).
    error_code: str | None = None


def release_lock_if_owned(
    workspace_root: Path,
    *,
    lock_filename: str,
    lock_id: str,
    transaction_id: str,
    authorization_id: str,
    authorization_digest: str,
) -> LockReleaseResult:
    lock_path = workspace_root / lock_filename
    if not lock_path.is_file():
        return LockReleaseResult(released=False, error_code=None)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return LockReleaseResult(released=False, error_code=LOCK_OWNERSHIP_MISMATCH)
    owned = (
        payload.get("lock_id") == lock_id
        and payload.get("transaction_id") == transaction_id
        and payload.get("authorization_id") == authorization_id
        and payload.get("authorization_digest") == authorization_digest
    )
    if not owned:
        return LockReleaseResult(released=False, error_code=LOCK_OWNERSHIP_MISMATCH)
    lock_path.unlink()
    return LockReleaseResult(released=True, error_code=None)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Phase B: validation under lock -------------------------------------------


def validate_phase_b_bootstrap(workspace_root: Path, authorization: MutationAuthorization) -> None:
    from .inventory import derive_expected_inventory

    if (workspace_root / "active_generation").exists():
        raise GuardRejection(
            AUTHORIZATION_BASELINE_INVENTORY_MISMATCH, "active_generation already exists"
        )

    expected_names = derive_expected_inventory(workspace_root)
    physical = sorted(p.name for p in workspace_root.iterdir() if p.is_file())
    physical_relevant = [n for n in physical if n in expected_names]
    if sorted(expected_names) != sorted(physical_relevant):
        raise GuardRejection(
            AUTHORIZATION_BASELINE_INVENTORY_MISMATCH,
            "flat baseline inventory changed since authorization was issued",
        )

    _validate_expected_files(workspace_root, authorization)


def validate_phase_b_transaction(
    workspace_root: Path, authorization: MutationAuthorization, resolved_generation_id: str
) -> None:
    if authorization.source_generation_id != resolved_generation_id:
        raise GuardRejection(
            SOURCE_GENERATION_MISMATCH,
            "authorization source_generation_id does not match the current active generation",
        )

    from .resolver import resolve_active_generation

    resolved = resolve_active_generation(workspace_root)
    if not resolved.ok or resolved.pointer is None:
        raise GuardRejection(SOURCE_GENERATION_MISMATCH, "active generation could not be resolved")
    if authorization.source_generation_digest != resolved.pointer.generation_digest:
        raise GuardRejection(
            SOURCE_GENERATION_MISMATCH,
            "authorization source_generation_digest does not match the current active generation",
        )

    from .model import generation_directory_name

    target_path = (
        workspace_root
        / "generations"
        / generation_directory_name(authorization.target_generation_id)
    )
    if target_path.exists():
        raise GuardRejection(
            TARGET_GENERATION_ALREADY_EXISTS, f"target generation already exists: {target_path}"
        )

    assert resolved.generation_path is not None
    _validate_expected_files(resolved.generation_path, authorization)


def validate_authorization_binding(
    authorization: MutationAuthorization | None,
    *,
    expected_operation_scope: str,
    transaction_id_arg: str,
    bound_authorization_digest: str,
    bound_source_generation_id: str | None,
    bound_target_generation_id: str,
) -> None:
    """Commit-time re-check: the Runtime Authorization supplied to commit
    must canonicalize to exactly the same digest recorded at begin time.
    No re-scan of the original baseline is performed here (that already
    happened, under Lock, in Phase B at begin time).
    """
    if authorization is None:
        raise GuardRejection(MUTATION_AUTHORIZATION_REQUIRED, "mutation_authorization is required")
    if authorization.operation_scope != expected_operation_scope:
        raise GuardRejection(
            AUTHORIZATION_OPERATION_MISMATCH, "authorization operation_scope mismatch at commit"
        )
    if not authorization.apply:
        raise GuardRejection(AUTHORIZATION_APPLY_REQUIRED, "authorization.apply must be true")
    if authorization.transaction_id != transaction_id_arg:
        raise GuardRejection(
            AUTHORIZATION_TRANSACTION_MISMATCH, "authorization transaction_id mismatch at commit"
        )
    if authorization.source_generation_id != bound_source_generation_id:
        raise GuardRejection(
            SOURCE_GENERATION_MISMATCH, "authorization source_generation_id mismatch at commit"
        )
    if authorization.target_generation_id != bound_target_generation_id:
        raise GuardRejection(
            AUTHORIZATION_OPERATION_MISMATCH,
            "authorization target_generation_id mismatch at commit",
        )
    if authorization.digest() != bound_authorization_digest:
        raise GuardRejection(
            AUTHORIZATION_DIGEST_MISMATCH,
            "runtime authorization digest does not match the digest recorded at begin",
        )


def _validate_expected_files(base_dir: Path, authorization: MutationAuthorization) -> None:
    import hashlib

    for entry in authorization.expected_files:
        file_path = base_dir / entry.relative_path
        if not file_path.is_file():
            raise GuardRejection(
                AUTHORIZATION_BASELINE_INVENTORY_MISMATCH,
                f"expected file missing: {entry.relative_path}",
            )
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != entry.sha256:
            raise GuardRejection(
                AUTHORIZATION_BASELINE_SHA_MISMATCH, f"SHA-256 mismatch: {entry.relative_path}"
            )
