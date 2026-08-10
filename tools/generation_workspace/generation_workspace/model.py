"""Domain model and canonical serializers for the Generation Workspace.

Canonical serialization contract (fixed by design, do not change key order
or separators without also changing the schema version):

  Pointer:  pointer_schema_version, generation_id,
            generation_digest_schema_version, generation_digest, transaction_id
  Digest Manifest: digest_schema_version, generation_id, components
  Component: component_id, relative_path, byte_count, sha256

Both are serialized as minified JSON: UTF-8, ensure_ascii=False,
sort_keys=False, separators=(",", ":"), no trailing newline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

POINTER_SCHEMA_VERSION = "WP-CLAIM-EXIT-ACTIVE-GENERATION-v1"
GENERATION_ID_RE = re.compile(r"^[0-9]{10}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_CANONICAL_SEPARATORS = (",", ":")

# Canonical Operational Reader (WP-OGR-01) failure classification. These are
# shared, read-only classification constants — not mutation authorization
# error codes — so they live here rather than in mutation_guard.py, keeping
# the existing "read-only APIs never depend on mutation_guard" boundary
# intact (see mutation_guard.py's module docstring).
ACTIVE_GENERATION_ABSENT = "ACTIVE_GENERATION_ABSENT"
ACTIVE_GENERATION_MALFORMED = "ACTIVE_GENERATION_MALFORMED"
ACTIVE_GENERATION_SYMLINK = "ACTIVE_GENERATION_SYMLINK"
GENERATION_MISSING = "GENERATION_MISSING"
GENERATION_SYMLINK = "GENERATION_SYMLINK"
GENERATION_DIGEST_MISMATCH = "GENERATION_DIGEST_MISMATCH"
GENERATION_INVENTORY_MISMATCH = "GENERATION_INVENTORY_MISMATCH"


class MalformedPointerError(ValueError):
    """Raised when a pointer file cannot be parsed or fails validation."""


class MalformedDigestManifestError(ValueError):
    """Raised when a digest manifest cannot be parsed or fails validation."""


def is_valid_generation_id(value: str) -> bool:
    return bool(GENERATION_ID_RE.match(value))


def is_valid_sha256(value: str) -> bool:
    return bool(SHA256_RE.match(value))


def is_valid_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value))


def generation_directory_name(generation_id: str) -> str:
    """Canonical Generation Path resolution: generations/gen-<generation_id>.

    Never hardcode "generations/<generation_id>" (missing gen- prefix).
    """
    if not is_valid_generation_id(generation_id):
        raise MalformedPointerError(f"invalid generation_id: {generation_id!r}")
    return f"gen-{generation_id}"


@dataclass(frozen=True)
class Pointer:
    pointer_schema_version: str
    generation_id: str
    generation_digest_schema_version: str
    generation_digest: str
    transaction_id: str

    def validate(self) -> None:
        if self.pointer_schema_version != POINTER_SCHEMA_VERSION:
            raise MalformedPointerError(
                f"unsupported pointer_schema_version: {self.pointer_schema_version!r}"
            )
        if not is_valid_generation_id(self.generation_id):
            raise MalformedPointerError(f"invalid generation_id: {self.generation_id!r}")
        if not self.generation_digest_schema_version:
            raise MalformedPointerError("generation_digest_schema_version is missing")
        if not is_valid_sha256(self.generation_digest):
            raise MalformedPointerError(f"invalid generation_digest: {self.generation_digest!r}")
        if not is_valid_uuid(self.transaction_id):
            raise MalformedPointerError(f"invalid transaction_id: {self.transaction_id!r}")

    def to_canonical_bytes(self) -> bytes:
        obj = {
            "pointer_schema_version": self.pointer_schema_version,
            "generation_id": self.generation_id,
            "generation_digest_schema_version": self.generation_digest_schema_version,
            "generation_digest": self.generation_digest,
            "transaction_id": self.transaction_id,
        }
        return json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=False,
            separators=_CANONICAL_SEPARATORS,
        ).encode("utf-8")


def parse_pointer(raw: bytes) -> Pointer:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedPointerError("pointer is not valid UTF-8") from exc
    if not text.strip():
        raise MalformedPointerError("pointer file is empty")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedPointerError("pointer is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise MalformedPointerError("pointer JSON must be an object")

    required = [
        "pointer_schema_version",
        "generation_id",
        "generation_digest_schema_version",
        "generation_digest",
        "transaction_id",
    ]
    for key in required:
        if key not in obj:
            raise MalformedPointerError(f"pointer is missing required field: {key}")
        if not isinstance(obj[key], str):
            raise MalformedPointerError(f"pointer field {key} must be a string")

    pointer = Pointer(
        pointer_schema_version=obj["pointer_schema_version"],
        generation_id=obj["generation_id"],
        generation_digest_schema_version=obj["generation_digest_schema_version"],
        generation_digest=obj["generation_digest"],
        transaction_id=obj["transaction_id"],
    )
    pointer.validate()
    return pointer


@dataclass(frozen=True)
class Component:
    component_id: str
    relative_path: str
    byte_count: int
    sha256: str

    def to_obj(self) -> dict:
        return {
            "component_id": self.component_id,
            "relative_path": self.relative_path,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class GenerationDigestManifest:
    digest_schema_version: str
    generation_id: str
    components: list[Component] = field(default_factory=list)

    def to_canonical_bytes(self) -> bytes:
        obj = {
            "digest_schema_version": self.digest_schema_version,
            "generation_id": self.generation_id,
            "components": [c.to_obj() for c in self.components],
        }
        return json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=False,
            separators=_CANONICAL_SEPARATORS,
        ).encode("utf-8")
