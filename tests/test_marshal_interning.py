"""Phase 3H-R6 (Decision 04): MARSHAL_EQUIVALENT_APPROVED_INTERNING_DIFF.

Streams are hand-built with the independent encoder of test_marshal_structure. A
class body, as codegen_class_body (Python/codegen.c, v3.14.10) compiles it, loads
its qualified name as constant 0 and stores it in __qualname__; the same object is
the code's co_qualname. The installed file has it interned ('Z', shared through
TYPE_REF), the recompiled one plain ('z') with an interned co_qualname written
separately - the shape of the four files of Decision 04.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

from tests.test_marshal_structure import (
    FILE,
    HEADER,
    NONE,
    OTHER,
    _gate_context,
    code,
    gate,
    installed,  # noqa: F401 - pytest fixture
    r,
    t,
    z,
)

Q = "Outer._Inner"
PATH = "<module>.co_consts[0].co_consts[0]"
NAME = "python/lib/python3.14/__pycache__/argparse.cpython-314.pyc"
CLASS_NAMES = t(z("__name__", interned=True), z("__module__", interned=True),
                z("__qualname__", interned=True))


def class_body(const: bytes, qualname: bytes, *, consts: bytes | None = None,
               **fields: Any) -> bytes:
    options = {"names": CLASS_NAMES, "name": z("_Inner", interned=True),
               "counts": (0, 0, 0, 1, 0)}
    options.update(fields)
    return code(consts=t(const, NONE) if consts is None else consts, qualname=qualname,
                filename=z(FILE), **options)


def pyc(body: bytes, module_filename: bytes = z(FILE), tail: bytes = NONE) -> bytes:
    return HEADER + code(consts=t(body, tail), names=t(z("_Inner", interned=True)),
                         filename=module_filename)


INSTALLED = pyc(class_body(z(Q, ref=True, interned=True), r(0)))
RECOMPILED = pyc(class_body(z(Q), z(Q, ref=True, interned=True)))


def approval(data: bytes = INSTALLED, path: str = PATH, value: str = Q,
             name: str = NAME) -> dict[str, dict]:
    return {name: {"sha256": hashlib.sha256(data).hexdigest(), "strings": [(path, value)]}}


def classify(installed_data: bytes, recompiled: bytes, approved: dict[str, dict] | None = None,
             verified: bool | None = True, name: str = NAME) -> dict:
    return gate.classify_pyc(name, installed_data, recompiled, {}, verified,
                             approval(installed_data) if approved is None else approved)


def test_fixture_differs_only_in_the_interned_flag() -> None:
    structure = gate.compare_structure(INSTALLED, RECOMPILED)
    assert structure["parsed"] and not structure["equivalent"]
    assert structure["equal_without_interned_flag"] is True
    assert [d["kind"] for d in structure["differences"]] == ["tag"]
    assert structure["differences"][0]["path"] == PATH


def test_approved_interning_difference_is_accepted() -> None:
    result = classify(INSTALLED, RECOMPILED)
    assert result["classification"] == gate.INTERNING_EQUIVALENT, result
    (row,) = result["structure"]["approved_interning"]["targets"]
    assert (row["installed_tag"], row["recompiled_tag"]) == ("Z", "z")


def _mismatch(result: dict) -> None:
    assert result["classification"] == gate.STRUCTURAL_MISMATCH, result


def test_same_string_at_another_code_position_is_rejected() -> None:
    moved = HEADER + code(consts=t(z("doc string, not a name"),
                                   class_body(z(Q, ref=True, interned=True), r(0)), NONE),
                          filename=z(FILE))
    recompiled = HEADER + code(consts=t(z("doc string, not a name"),
                                        class_body(z(Q), z(Q, ref=True, interned=True)), NONE),
                               filename=z(FILE))
    _mismatch(classify(moved, recompiled))
    assert "path not present" in classify(moved, recompiled)["structure"][
        "approved_interning"]["targets"][0]["problem"]


def test_string_that_is_not_constant_0_is_rejected() -> None:
    inst = pyc(class_body(NONE, r(0), consts=t(NONE, z(Q, ref=True, interned=True))))
    rec = pyc(class_body(NONE, z(Q, ref=True, interned=True), consts=t(NONE, z(Q))))
    result = classify(inst, rec, approval(inst, "<module>.co_consts[0].co_consts[1]"))
    _mismatch(result)
    assert result["structure"]["approved_interning"]["targets"][0]["problem"] == \
        "not constant 0 of a code object"


def test_difference_in_an_unapproved_file_is_rejected() -> None:
    _mismatch(classify(INSTALLED, RECOMPILED, approval(name="python/lib/other.pyc")))
    _mismatch(classify(INSTALLED, RECOMPILED, {}))


def test_changed_installed_content_is_not_approved() -> None:
    _mismatch(classify(INSTALLED, RECOMPILED, approval(b"other content")))


def test_changed_value_is_rejected() -> None:
    rec = pyc(class_body(z("Outer._Other"), z(Q, ref=True, interned=True)))
    _mismatch(classify(INSTALLED, rec))


def test_approval_for_another_value_is_rejected() -> None:
    _mismatch(classify(INSTALLED, RECOMPILED, approval(value="Outer._Other")))


@pytest.mark.parametrize("fields", [
    {"counts": (0, 0, 0, 2, 0)},  # nested code attribute (stack size)
    {"firstlineno": 7},
    {"bytecode": b"\x95\x00\x52\x01\x23\x00"},
    {"linetable": b"\xf0\x03\x01\x01\x02"},
    {"exceptiontable": b"\x82\x05\x0a\x03"},
    {"name": z("_Other", interned=True)},
    {"names": t(z("__name__", interned=True), z("__qualname__", interned=True))},
])
def test_any_other_difference_in_the_class_body_is_rejected(fields: dict[str, Any]) -> None:
    rec = pyc(class_body(z(Q), z(Q, ref=True, interned=True), **fields))
    _mismatch(classify(INSTALLED, rec))


def test_other_difference_elsewhere_is_rejected() -> None:
    rec = pyc(class_body(z(Q), z(Q, ref=True, interned=True)), tail=z("x"))
    _mismatch(classify(INSTALLED, rec))


def test_unknown_interned_difference_is_rejected() -> None:
    # The approved difference plus an unapproved Z / z difference (the module file name).
    rec = pyc(class_body(z(Q), z(Q, ref=True, interned=True)),
              module_filename=z(FILE, interned=True))
    result = classify(INSTALLED, rec)
    _mismatch(result)
    assert result["structure"]["approved_interning"]["rest_identical"] is False


def test_flip_at_co_qualname_instead_of_the_constant_is_rejected() -> None:
    inst = pyc(class_body(z(Q), z(Q, ref=True, interned=True)))
    rec = pyc(class_body(z(Q), z(Q)))
    _mismatch(classify(inst, rec))


@pytest.mark.parametrize("installed_tag, recompiled_tag", [("A", "a"), ("t", "u"), ("Z", "Z")])
def test_other_tag_pairs_at_the_target_are_rejected(installed_tag: str,
                                                    recompiled_tag: str) -> None:
    def string(tag: str) -> bytes:
        data = Q.encode()
        size = bytes([len(data)]) if tag in "zZ" else len(data).to_bytes(4, "little")
        return tag.encode() + size + data

    inst = pyc(class_body(string(installed_tag), z(Q, ref=True, interned=True)))
    rec = pyc(class_body(string(recompiled_tag), z(Q, ref=True, interned=True)),
              module_filename=z(FILE, interned=installed_tag == recompiled_tag))
    _mismatch(classify(inst, rec))


def test_malformed_approval_is_recorded_not_raised() -> None:
    for path, value in (("co_consts[0]", Q), (PATH, "é")):
        result = classify(INSTALLED, RECOMPILED, approval(path=path, value=value))
        _mismatch(result)
        assert "Error" in result["structure"]["approved_interning"]["problem"]


def test_reversed_direction_is_rejected() -> None:
    _mismatch(classify(RECOMPILED, INSTALLED, approval(RECOMPILED)))


def test_owner_without_qualname_store_is_rejected() -> None:
    names = t(z("__name__", interned=True))
    inst = pyc(class_body(z(Q, ref=True, interned=True), r(0), names=names))
    rec = pyc(class_body(z(Q), z(Q, ref=True, interned=True), names=names))
    result = classify(inst, rec)
    _mismatch(result)
    assert "__qualname__" in result["structure"]["approved_interning"]["targets"][0]["problem"]


def test_tampered_reference_index_is_rejected() -> None:
    # Out of range: not parsable.
    bad = pyc(class_body(z(Q, ref=True, interned=True), r(5)))
    assert classify(bad, RECOMPILED, approval(bad))["classification"] == gate.REJECTED
    # In range but to another object (reference 1 is "__name__": co_consts precede
    # co_names in a code record): co_qualname no longer matches.
    names = t(z("__name__", interned=True, ref=True), z("__module__", interned=True),
              z("__qualname__", interned=True))
    inst = pyc(class_body(z(Q, ref=True, interned=True), r(1), names=names))
    rec = pyc(class_body(z(Q), z(Q, ref=True, interned=True), names=names))
    _mismatch(classify(inst, rec))


def test_header_difference_is_rejected() -> None:
    rec = RECOMPILED[:8] + bytes([RECOMPILED[8] ^ 1]) + RECOMPILED[9:]
    _mismatch(classify(INSTALLED, rec))


def test_oracle_failure_is_rejected() -> None:
    for verified in (False, None):
        result = classify(INSTALLED, RECOMPILED, verified=verified)
        assert result["classification"] == gate.REJECTED
        assert result["reason"] == "reader conformance not established"


def test_interning_rule_does_not_relax_reference_layout_rule() -> None:
    # A pure reference-layout difference in a file with only an interning approval is
    # still governed by Decision 03 (not approved here: REJECTED).
    shared = pyc(class_body(z(Q, ref=True, interned=True), r(0)))
    inlined = pyc(class_body(z(Q, interned=True), z(Q, interned=True)))
    result = classify(shared, inlined)
    assert result["classification"] == gate.REJECTED


def test_paths_are_parsed_strictly() -> None:
    assert gate.parse_path("<module>.co_consts[3].co_consts[0]") == [
        ("field", "co_consts"), ("index", 3), ("field", "co_consts"), ("index", 0)]
    for bad in ("co_consts[0]", "<module>.co_consts[x]", "<module>co_consts", "<module>[0]x"):
        with pytest.raises(ValueError):
            gate.parse_path(bad)


def test_decision_04_approval_list() -> None:
    approved = gate.INTERNING_APPROVED
    assert sorted(Path(name).name for name in approved) == [
        "argparse.cpython-314.pyc", "inspect.cpython-314.pyc", "typing.cpython-314.pyc",
        "weakref.cpython-314.pyc"]
    per_file = {Path(name).name.split(".")[0]: sorted(value for _, value in entry["strings"])
                for name, entry in approved.items()}
    assert per_file == {
        "argparse": ["HelpFormatter._Section", "_SubParsersAction._ChoicesPseudoAction"],
        "inspect": ["_signature_fromstr.<locals>.RewriteSymbolics"],
        "typing": ["NewType.__mro_entries__.<locals>.Dummy"],
        "weakref": ["finalize._Info"]}
    for name, entry in approved.items():
        assert entry["sha256"] == gate.REF_LAYOUT_APPROVED[name]
        for path, _ in entry["strings"]:
            assert path.endswith(".co_consts[0]")
            gate.parse_path(path)


def test_oracle_expects_both_long_ascii_forms() -> None:
    assert {"A", "a"} <= gate.ORACLE_TAGS
    values = {}
    for line in gate.ORACLE_SOURCE.splitlines():
        key, _, literal = line.partition(" = ")
        if key in ("LONG", "LONG_PLAIN"):
            values[key] = ast.literal_eval(literal)
    assert len(values["LONG"]) == len(values["LONG_PLAIN"]) == 300
    assert values["LONG"].isidentifier() and not values["LONG_PLAIN"].isidentifier()


# --- gate 4 -----------------------------------------------------------------------------


@pytest.mark.parametrize("oracle_status, expected, status", [
    (gate.PASS, gate.INTERNING_EQUIVALENT, gate.PASS),
    (gate.FAIL, gate.REJECTED, gate.FAIL),
])
def test_gate4_with_an_approved_interning_difference(
        tmp_path: Path, installed: tuple[Path, Path],  # noqa: F811
        oracle_status: str, expected: str, status: str) -> None:
    archive, runtime_dir = installed
    ctx = _gate_context(tmp_path, archive, runtime_dir, b"", {}, oracle_status)
    # _gate_context installs SHARED_AT_0 as other.pyc; use the class-body pair instead.
    (runtime_dir / "python" / "lib" / "pkg" / "__pycache__" / Path(OTHER).name).write_bytes(
        INSTALLED)
    factory = ctx.isolation

    def isolation(work: Path) -> Any:
        sandbox = factory(work)
        sandbox.replacement = {**sandbox.replacement, OTHER: RECOMPILED}
        return sandbox

    ctx.isolation = isolation
    ctx.settings.interning_approved = approval(INSTALLED, name=OTHER)
    result = gate.gate4_runtime(ctx)
    assert result.status == status, result.evidence
    counts = result.evidence["recompilation"]["classification"]
    assert counts[expected] == 1 and counts[gate.BYTE_IDENTICAL] == 1
    assert sum(value for key, value in counts.items() if key != "total") == counts["total"] == 2
