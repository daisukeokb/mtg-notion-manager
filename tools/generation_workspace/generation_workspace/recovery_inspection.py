"""Recovery Inspection: read-only observation and classification of
Generation Workspace recovery-relevant state (WP-OGR-03).

INSPECTION_ONLY. inspect_generation_recovery() never mutates the filesystem
and never calls a mutation entry point. It layers a Direct Observation
(Layer 2) and a Derived Interpretation (Layer 3) on top of the existing
Underlying Recovery Result (Layer 1, from
transaction.recover_generation_transaction — itself read-only).

Historical Evidence Limits (do not weaken these when extending this module):

- Protocol Completion Status is never treated as Inspection-derived
  Transaction Commit Status. A Stable State (no Lock, no Control
  Transaction, valid Pointer, Generation verifies) never reconstructs a
  historical commit: phase_origin and transaction_commit_status stay
  UNCLASSIFIED.
- Transaction ID equality (Lock vs Pointer) is correlation evidence only.
  It is never treated as proof of historical transaction identity, and it
  never by itself promotes phase_origin or transaction_commit_status.
- transaction_commit_status is derived only from the explicit combination
  of Control Transaction State + Pointer State + Generation Verification.
  Control Transaction absence, or any combination outside the accepted
  matrix, always yields UNCLASSIFIED.
- Lock-without-Control-Transaction is reported as an Observation
  (LOCK_WITHOUT_CONTROL_TRANSACTION); it never implies a specific historical
  cause, phase, or commit status.
- Target Generation is never inferred (no "current + 1", no lock-derived
  guess). Only the Control Transaction's own recorded target (when a
  Control Transaction is present) is authoritative; Direct Observation
  fields are reported as-is otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .model import LOCK_WITHOUT_CONTROL_TRANSACTION, RECOVERY_CONTEXT_UNCLASSIFIED
from .resolver import (
    ACTIVE_GENERATION_FILENAME,
    GENERATIONS_DIRNAME,
    read_verified_active_generation,
)
from .transaction import (
    CONTROL_TRANSACTION_FILENAME,
    LOCK_FILENAME,
    STATE_COMMITTED,
    STATE_COMMITTING_POINTER,
    STATE_GENERATION_PUBLISHED,
    STATE_PREPARING,
    STATE_VERIFIED,
    recover_generation_transaction,
)

RECOVERY_INSPECTION_SCHEMA_VERSION = "WP-CLAIM-EXIT-RECOVERY-INSPECTION-v1"

RECOVERY_STATUS_FAILED_REQUIRES_RECOVERY = "FAILED_REQUIRES_RECOVERY"

LOCK_METADATA_STATUS_VALID = "VALID"
LOCK_METADATA_STATUS_MALFORMED = "MALFORMED"

INSPECTION_CLASSIFICATION_NONE = "NONE"

TRANSACTION_BINDING_MATCH = "MATCH"
TRANSACTION_BINDING_MISMATCH = "MISMATCH"
TRANSACTION_BINDING_LOCK_ID_UNAVAILABLE = "LOCK_ID_UNAVAILABLE"
TRANSACTION_BINDING_POINTER_ID_UNAVAILABLE = "POINTER_ID_UNAVAILABLE"
TRANSACTION_BINDING_BOTH_IDS_UNAVAILABLE = "BOTH_IDS_UNAVAILABLE"
TRANSACTION_BINDING_UNCLASSIFIED = "UNCLASSIFIED"

# Only UNCLASSIFIED is ever assigned by this implementation. The POSSIBLE_*
# values are reserved for schema compatibility with a future, stronger
# provenance mechanism (e.g. historical transaction replay detection) that
# does not exist yet; no code path in this module promotes to them.
PHASE_ORIGIN_UNCLASSIFIED = "UNCLASSIFIED"
PHASE_ORIGIN_POSSIBLE_PRE_CONTROL_TRANSACTION_WINDOW = "POSSIBLE_PRE_CONTROL_TRANSACTION_WINDOW"
PHASE_ORIGIN_POSSIBLE_POST_COMMIT_CLEANUP_WINDOW = "POSSIBLE_POST_COMMIT_CLEANUP_WINDOW"

TRANSACTION_COMMIT_STATUS_NOT_COMMITTED = "NOT_COMMITTED"
TRANSACTION_COMMIT_STATUS_COMMITTED_OR_EFFECTIVELY_COMMITTED = (
    "COMMITTED_OR_EFFECTIVELY_COMMITTED"
)
TRANSACTION_COMMIT_STATUS_COMMITTED = "COMMITTED"
TRANSACTION_COMMIT_STATUS_UNCLASSIFIED = "UNCLASSIFIED"

SAFE_ACTION_MANUAL_LOCK_STATE_REVIEW_REQUIRED = "MANUAL_LOCK_STATE_REVIEW_REQUIRED"

AUTOMATIC_MUTATION_PROHIBITED = "PROHIBITED"

_LOCK_REQUIRED_KEYS = (
    "lock_schema_version",
    "lock_id",
    "transaction_id",
    "authorization_id",
    "authorization_digest",
    "created_at_utc",
)

_NOT_COMMITTED_STATES = frozenset(
    {STATE_PREPARING, STATE_VERIFIED, STATE_GENERATION_PUBLISHED, STATE_COMMITTING_POINTER}
)

_STAGING_PREFIX = ".staging-gen-"


@dataclass(frozen=True)
class RecoveryInspectionResult:
    # Layer 1 — Underlying Recovery Result (unmodified meaning).
    underlying_recovery_case: str
    underlying_recovery_status: str
    underlying_safe_action: str
    # Layer 2 — Direct Observation.
    lock_present: bool
    lock_metadata_status: str | None
    lock_transaction_id: str | None
    control_transaction_present: bool
    control_transaction_tmp_present: bool
    pointer_generation_id: str | None
    pointer_transaction_id: str | None
    current_verified_active_generation_id: str | None
    staging_present: bool
    staging_entry_count: int | None
    generation_directories_present: bool
    # Layer 3 — Derived Interpretation.
    inspection_classification: str
    transaction_binding_status: str
    phase_origin: str
    transaction_commit_status: str
    safe_action: str
    automatic_mutation: str


def _control_transaction_tmp_path(workspace_root: Path) -> Path:
    path = workspace_root / CONTROL_TRANSACTION_FILENAME
    return path.with_suffix(path.suffix + ".tmp")


def _read_lock(workspace_root: Path) -> tuple[bool, str | None, str | None]:
    lock_path = workspace_root / LOCK_FILENAME
    if not lock_path.is_file() or lock_path.is_symlink():
        return False, None, None
    try:
        obj = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, LOCK_METADATA_STATUS_MALFORMED, None
    if not isinstance(obj, dict) or any(
        not isinstance(obj.get(key), str) for key in _LOCK_REQUIRED_KEYS
    ):
        return True, LOCK_METADATA_STATUS_MALFORMED, None
    return True, LOCK_METADATA_STATUS_VALID, obj["transaction_id"]


def _read_control_transaction_fields(workspace_root: Path) -> dict[str, str | None] | None:
    path = workspace_root / CONTROL_TRANSACTION_FILENAME
    if not path.is_file() or path.is_symlink():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    state = obj.get("state")
    source_generation = obj.get("source_generation")
    target_generation = obj.get("target_generation")
    return {
        "state": state if isinstance(state, str) else None,
        "source_generation": source_generation if isinstance(source_generation, str) else None,
        "target_generation": target_generation if isinstance(target_generation, str) else None,
    }


def _read_pointer_raw(workspace_root: Path) -> tuple[str | None, str | None]:
    pointer_path = workspace_root / ACTIVE_GENERATION_FILENAME
    if not pointer_path.is_file() or pointer_path.is_symlink():
        return None, None
    try:
        obj = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    generation_id = obj.get("generation_id")
    transaction_id = obj.get("transaction_id")
    return (
        generation_id if isinstance(generation_id, str) else None,
        transaction_id if isinstance(transaction_id, str) else None,
    )


def _staging_observation(workspace_root: Path) -> tuple[bool, int | None]:
    matches = [
        p for p in workspace_root.iterdir() if p.is_dir() and p.name.startswith(_STAGING_PREFIX)
    ]
    if not matches:
        return False, None
    entry_count = sum(1 for staging_dir in matches for _ in staging_dir.iterdir())
    return True, entry_count


def _generation_directories_present(workspace_root: Path) -> bool:
    generations_dir = workspace_root / GENERATIONS_DIRNAME
    if not generations_dir.is_dir():
        return False
    return any(p.is_dir() and p.name.startswith("gen-") for p in generations_dir.iterdir())


def _classify_transaction_binding_status(
    lock_transaction_id: str | None, pointer_transaction_id: str | None
) -> str:
    if lock_transaction_id is not None and pointer_transaction_id is not None:
        if lock_transaction_id == pointer_transaction_id:
            return TRANSACTION_BINDING_MATCH
        return TRANSACTION_BINDING_MISMATCH
    if lock_transaction_id is None and pointer_transaction_id is not None:
        return TRANSACTION_BINDING_LOCK_ID_UNAVAILABLE
    if lock_transaction_id is not None and pointer_transaction_id is None:
        return TRANSACTION_BINDING_POINTER_ID_UNAVAILABLE
    if lock_transaction_id is None and pointer_transaction_id is None:
        return TRANSACTION_BINDING_BOTH_IDS_UNAVAILABLE
    return TRANSACTION_BINDING_UNCLASSIFIED  # defensive; structurally unreachable


def _classify_transaction_commit_status(
    control_fields: dict[str, str | None] | None,
    pointer_generation_id: str | None,
    generation_verification_pass: bool,
) -> str:
    if control_fields is None or not generation_verification_pass:
        return TRANSACTION_COMMIT_STATUS_UNCLASSIFIED

    state = control_fields["state"]
    source = control_fields["source_generation"]
    target = control_fields["target_generation"]

    if (
        state in _NOT_COMMITTED_STATES
        and pointer_generation_id is not None
        and pointer_generation_id == source
    ):
        return TRANSACTION_COMMIT_STATUS_NOT_COMMITTED
    if (
        state == STATE_COMMITTING_POINTER
        and pointer_generation_id is not None
        and pointer_generation_id == target
    ):
        return TRANSACTION_COMMIT_STATUS_COMMITTED_OR_EFFECTIVELY_COMMITTED
    if (
        state == STATE_COMMITTED
        and pointer_generation_id is not None
        and pointer_generation_id == target
    ):
        return TRANSACTION_COMMIT_STATUS_COMMITTED
    return TRANSACTION_COMMIT_STATUS_UNCLASSIFIED


def inspect_generation_recovery(workspace_root: Path) -> RecoveryInspectionResult:
    """Read-only Recovery Inspection entry point. Never mutates the
    filesystem; never calls a mutation entry point (only the existing
    read-only recover_generation_transaction() and
    read_verified_active_generation()).
    """
    underlying = recover_generation_transaction(workspace_root)

    lock_present, lock_metadata_status, lock_transaction_id = _read_lock(workspace_root)
    control_transaction_present = (workspace_root / CONTROL_TRANSACTION_FILENAME).is_file()
    control_transaction_tmp_present = _control_transaction_tmp_path(workspace_root).is_file()
    control_fields = (
        _read_control_transaction_fields(workspace_root) if control_transaction_present else None
    )
    pointer_generation_id, pointer_transaction_id = _read_pointer_raw(workspace_root)

    verified = read_verified_active_generation(workspace_root)
    current_verified_active_generation_id = (
        verified.verified.generation_id if verified.ok and verified.verified else None
    )

    staging_present, staging_entry_count = _staging_observation(workspace_root)
    generation_directories_present = _generation_directories_present(workspace_root)

    if lock_present and not control_transaction_present:
        inspection_classification = LOCK_WITHOUT_CONTROL_TRANSACTION
    elif underlying.status == RECOVERY_STATUS_FAILED_REQUIRES_RECOVERY:
        inspection_classification = RECOVERY_CONTEXT_UNCLASSIFIED
    else:
        inspection_classification = INSPECTION_CLASSIFICATION_NONE

    transaction_binding_status = _classify_transaction_binding_status(
        lock_transaction_id, pointer_transaction_id
    )
    transaction_commit_status = _classify_transaction_commit_status(
        control_fields, pointer_generation_id, verified.ok
    )
    phase_origin = PHASE_ORIGIN_UNCLASSIFIED

    if inspection_classification == LOCK_WITHOUT_CONTROL_TRANSACTION:
        safe_action = SAFE_ACTION_MANUAL_LOCK_STATE_REVIEW_REQUIRED
    else:
        safe_action = underlying.safe_action

    return RecoveryInspectionResult(
        underlying_recovery_case=underlying.case,
        underlying_recovery_status=underlying.status,
        underlying_safe_action=underlying.safe_action,
        lock_present=lock_present,
        lock_metadata_status=lock_metadata_status,
        lock_transaction_id=lock_transaction_id,
        control_transaction_present=control_transaction_present,
        control_transaction_tmp_present=control_transaction_tmp_present,
        pointer_generation_id=pointer_generation_id,
        pointer_transaction_id=pointer_transaction_id,
        current_verified_active_generation_id=current_verified_active_generation_id,
        staging_present=staging_present,
        staging_entry_count=staging_entry_count,
        generation_directories_present=generation_directories_present,
        inspection_classification=inspection_classification,
        transaction_binding_status=transaction_binding_status,
        phase_origin=phase_origin,
        transaction_commit_status=transaction_commit_status,
        safe_action=safe_action,
        automatic_mutation=AUTOMATIC_MUTATION_PROHIBITED,
    )
