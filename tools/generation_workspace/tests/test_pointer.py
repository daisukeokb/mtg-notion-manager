from generation_workspace.model import MalformedPointerError, Pointer, parse_pointer
from generation_workspace.resolver import resolve_active_generation

from .conftest import new_uuid


def _valid_pointer_obj():
    return {
        "pointer_schema_version": "WP-CLAIM-EXIT-ACTIVE-GENERATION-v1",
        "generation_id": "0000000001",
        "generation_digest_schema_version": "WP-CLAIM-EXIT-GENERATION-DIGEST-v1",
        "generation_digest": "a" * 64,
        "transaction_id": new_uuid(),
    }


def test_pointer_roundtrip_canonical_bytes():
    obj = _valid_pointer_obj()
    pointer = Pointer(**obj)
    raw = pointer.to_canonical_bytes()
    assert raw == (
        b'{"pointer_schema_version":"WP-CLAIM-EXIT-ACTIVE-GENERATION-v1",'
        b'"generation_id":"0000000001",'
        b'"generation_digest_schema_version":"WP-CLAIM-EXIT-GENERATION-DIGEST-v1",'
        b'"generation_digest":"' + b"a" * 64 + b'",'
        b'"transaction_id":"' + obj["transaction_id"].encode() + b'"}'
    )
    assert parse_pointer(raw) == pointer


def test_T_025_digest_schema_version_field_missing_rejected():
    """T-025: generation_digest_schema_version field absence is rejected,
    distinctly from an unsupported (but present) schema version value."""
    obj = _valid_pointer_obj()
    del obj["generation_digest_schema_version"]
    import json

    raw = json.dumps(obj).encode("utf-8")
    try:
        parse_pointer(raw)
        raise AssertionError("expected MalformedPointerError")
    except MalformedPointerError as exc:
        assert "generation_digest_schema_version" in str(exc)


def test_active_generation_absent_pointer_resolution_fails(tmp_path):
    result = resolve_active_generation(tmp_path)
    assert not result.ok
    assert "absent" in result.reason
