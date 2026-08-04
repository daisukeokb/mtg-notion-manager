"""Generation Digest: content-only, transaction-independent integrity proof.

compute_generation_digest reads from wherever the files physically live
(Staging path or Published path) but always builds Digest Components using
the Canonical relative_path ("generations/gen-<generation_id>/<filename>"),
so the digest value is identical regardless of physical location.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .inventory import MANIFEST_FILENAME, STATE_FILENAME, verify_physical_inventory
from .model import Component, GenerationDigestManifest

SUPPORTED_DIGEST_SCHEMA_VERSIONS = {"WP-CLAIM-EXIT-GENERATION-DIGEST-v1"}


class UnsupportedDigestSchemaError(ValueError):
    pass


def _component_id_for(filename: str) -> str:
    if filename.startswith("source_"):
        # source_01_layer_1.jsonl -> SRC-01
        layer_num = filename.split("_")[1]
        return f"SRC-{layer_num}"
    if filename == MANIFEST_FILENAME:
        return "MANIFEST"
    if filename == STATE_FILENAME:
        return "STATE"
    return "CANDIDATE"


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_relative_path(generation_id: str, filename: str) -> str:
    from .model import generation_directory_name

    return f"generations/{generation_directory_name(generation_id)}/{filename}"


def build_components(physical_generation_path: Path, generation_id: str) -> list[Component]:
    filenames = verify_physical_inventory(physical_generation_path)
    components = []
    for filename in filenames:
        file_path = physical_generation_path / filename
        components.append(
            Component(
                component_id=_component_id_for(filename),
                relative_path=canonical_relative_path(generation_id, filename),
                byte_count=file_path.stat().st_size,
                sha256=_sha256_of_file(file_path),
            )
        )
    return components


def compute_generation_digest(
    physical_generation_path: Path,
    generation_id: str,
    digest_schema_version: str,
) -> str:
    """Compute the transaction-independent Generation Digest (sha256 hex).

    physical_generation_path may be a Staging directory or a Published
    Generation directory; the resulting digest is identical in either case
    for identical content, because Component relative_path is always the
    Canonical Published path, never the physical Staging directory name.
    """
    if digest_schema_version not in SUPPORTED_DIGEST_SCHEMA_VERSIONS:
        raise UnsupportedDigestSchemaError(
            f"unsupported digest_schema_version: {digest_schema_version!r}"
        )
    components = build_components(physical_generation_path, generation_id)
    manifest = GenerationDigestManifest(
        digest_schema_version=digest_schema_version,
        generation_id=generation_id,
        components=components,
    )
    return hashlib.sha256(manifest.to_canonical_bytes()).hexdigest()
