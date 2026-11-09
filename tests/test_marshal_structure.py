"""Phase 3H-R6 (Decision 03): the structural marshal reader of scripts/verify_cutover_gate.py.

The streams are built here by a small encoder written from Python/marshal.c of
CPython v3.14.10, independently of the reader, or produced by the running
interpreter's own marshal writer. Nothing reads the installed runtime.
"""

from __future__ import annotations

import hashlib
import json
import marshal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_verify_cutover_gate import (
    TAG,
    _context,
    _LocalIsolation,
    _pyc,
    gate,
    installed,  # noqa: F401 - pytest fixture
)

MAGIC = bytes.fromhex("2b0e0d0a")
HEADER = MAGIC + bytes(4) + struct.pack("<II", 1704067200, 42)
PY314 = sys.version_info[:2] == (3, 14)


# --- an independent encoder (CPython 3.14 marshal, version 5) -------------------------------


def _i32(value: int) -> bytes:
    return struct.pack("<i", value)


def _type(tag: str, ref: bool) -> bytes:
    return bytes([ord(tag) | (0x80 if ref else 0)])


def z(text: str, ref: bool = False, interned: bool = False) -> bytes:
    data = text.encode("ascii")
    return _type("Z" if interned else "z", ref) + bytes([len(data)]) + data


def u(text: str, ref: bool = False) -> bytes:
    data = text.encode("utf-8")
    return _type("u", ref) + _i32(len(data)) + data


def b(data: bytes, ref: bool = False) -> bytes:
    return _type("s", ref) + _i32(len(data)) + data


def t(*items: bytes, ref: bool = False) -> bytes:
    return _type(")", ref) + bytes([len(items)]) + b"".join(items)


def i(value: int, ref: bool = False) -> bytes:
    return _type("i", ref) + _i32(value)


def r(index: int) -> bytes:
    return b"r" + _i32(index)


NONE = b"N"


def code(*, consts: bytes | None = None, names: bytes | None = None,
         localsplusnames: bytes | None = None, localspluskinds: bytes | None = None,
         filename: bytes | None = None, name: bytes | None = None,
         qualname: bytes | None = None, bytecode: bytes = b"\x95\x00\x52\x00\x23\x00",
         linetable: bytes = b"\xf0\x03\x01\x01\x01", exceptiontable: bytes = b"",
         counts: tuple[int, int, int, int, int] = (0, 0, 0, 1, 0), firstlineno: int = 1,
         ref: bool = False) -> bytes:
    """A code record in the field order of w_complex_object() (marshal.c, v3.14.10)."""
    return (_type("c", ref) + b"".join(_i32(n) for n in counts) + b(bytecode)
            + (t(NONE) if consts is None else consts) + (t() if names is None else names)
            + (t() if localsplusnames is None else localsplusnames)
            + (b(b"") if localspluskinds is None else localspluskinds)
            + (z("/lib/mod.py") if filename is None else filename)
            + (z("<module>", interned=True) if name is None else name)
            + (z("<module>", interned=True) if qualname is None else qualname)
            + _i32(firstlineno) + b(linetable) + b(exceptiontable))


FILE = "/lib/mod.py"


def function(filename: bytes, **fields: Any) -> bytes:
    options = {"localsplusnames": t(z("x", interned=True)), "localspluskinds": b(b"\x26"),
               "name": z("f", interned=True), "qualname": z("f", interned=True),
               "counts": (1, 0, 0, 1, 3), "consts": t(NONE)}
    options.update(fields)
    return code(filename=filename, **options)


def module(inner: bytes, filename: bytes, *, consts_tail: bytes = z("doc string, not a name"),
           ref: bool = False, **fields: Any) -> bytes:
    options = {"names": t(z("f", interned=True)), **fields}
    return HEADER + code(consts=t(inner, consts_tail, NONE), filename=filename, ref=ref,
                         **options)


# The same module three ways: the file name shared through TYPE_REF 0, through
# TYPE_REF 1 (the module record takes index 0), and written twice.
SHARED_AT_0 = module(function(z(FILE, ref=True)), r(0))
SHARED_AT_1 = module(function(z(FILE, ref=True)), r(1), ref=True)
INLINED = module(function(z(FILE)), z(FILE))
FLAGGED_ONLY = module(function(z(FILE)), z(FILE, ref=True))


def _compare(a: bytes, c: bytes) -> dict:
    return gate.compare_structure(a, c)


# --- accepted: only the reference layout differs ---------------------------------------------


@pytest.mark.parametrize("other", [SHARED_AT_1, INLINED, FLAGGED_ONLY])
def test_reference_layout_differences_are_equivalent(other: bytes) -> None:
    assert other != SHARED_AT_0
    result = _compare(SHARED_AT_0, other)
    assert result["parsed"] and result["equivalent"], result
    assert "differences" not in result


def test_flag_ref_only_difference_is_equivalent() -> None:
    result = _compare(INLINED, FLAGGED_ONLY)
    assert result["equivalent"]
    assert result["installed"]["flag_refs"] == 0 and result["recompiled"]["flag_refs"] == 1


def test_type_ref_index_difference_is_equivalent() -> None:
    result = _compare(SHARED_AT_0, SHARED_AT_1)
    assert result["equivalent"]
    assert result["installed"]["type_refs"] == result["recompiled"]["type_refs"] == 1
    assert result["installed"]["flag_refs"] == 1 and result["recompiled"]["flag_refs"] == 2


def test_identical_streams_are_equivalent() -> None:
    assert _compare(SHARED_AT_0, SHARED_AT_0)["equivalent"]


# --- rejected: a real difference ------------------------------------------------------------


@pytest.mark.parametrize("other, path, kind", [
    (module(function(z(FILE), bytecode=b"\x95\x00\x52\x01\x23\x00"), z(FILE)),
     "<module>.co_consts[0].co_code", "value"),
    (module(function(z(FILE)), z(FILE), consts_tail=z("another doc string")),
     "<module>.co_consts[1]", "value"),
    (module(function(z(FILE)), z(FILE), consts_tail=i(7)), "<module>.co_consts[1]", "structure"),
    (module(function(z(FILE)), z(FILE), names=t(z("g", interned=True))),
     "<module>.co_names[0]", "value"),
    (module(function(z(FILE), qualname=z("g", interned=True)), z(FILE)),
     "<module>.co_consts[0].co_qualname", "value"),
    (module(function(z(FILE), localsplusnames=t(z("y", interned=True))), z(FILE)),
     "<module>.co_consts[0].co_localsplusnames[0]", "value"),
    (module(function(z(FILE), localspluskinds=b(b"\x66")), z(FILE)),
     "<module>.co_consts[0].co_localspluskinds", "value"),
    (module(function(z(FILE)), z("/lib/other.py")), "<module>.co_filename", "value"),
    (module(function(z(FILE)), z(FILE), linetable=b"\xf0\x03\x01\x01\x02"),
     "<module>.co_linetable", "value"),
    (module(function(z(FILE)), z(FILE), exceptiontable=b"\x82\x05\x0a\x03"),
     "<module>.co_exceptiontable", "value"),
    (module(function(z(FILE), counts=(1, 0, 0, 2, 3)), z(FILE)), "<module>.co_consts[0]",
     "value"),
    (module(function(z(FILE)), z(FILE), firstlineno=2), "<module>", "value"),
])
def test_real_differences_are_mismatches(other: bytes, path: str, kind: str) -> None:
    result = _compare(INLINED, other)
    assert result["parsed"] and not result["equivalent"]
    first = result["differences"][0]
    assert (first["path"], first["kind"]) == (path, kind), result["differences"]


def test_code_scalar_difference_names_the_field() -> None:
    other = module(function(z(FILE), counts=(1, 0, 0, 2, 3)), z(FILE))
    assert _compare(INLINED, other)["differences"][0]["fields"] == ["co_stacksize"]


def test_interned_flag_difference_is_a_mismatch() -> None:
    other = module(function(z(FILE)), z(FILE, interned=True))
    result = _compare(INLINED, other)
    assert not result["equivalent"]
    assert result["differences"][0]["kind"] == "tag"
    assert result["equal_without_interned_flag"] is True  # diagnostics only


def test_header_difference_is_a_mismatch() -> None:
    other = HEADER[:8] + struct.pack("<I", 1) + HEADER[12:] + INLINED[16:]
    result = _compare(INLINED, other)
    assert result["parsed"] and not result["header_identical"] and not result["equivalent"]


def test_frozenset_order_is_kept() -> None:
    def fs(*items: bytes) -> bytes:
        return b">" + _i32(len(items)) + b"".join(items)

    first = module(function(z(FILE)), z(FILE), consts_tail=fs(i(1), i(2)))
    second = module(function(z(FILE)), z(FILE), consts_tail=fs(i(2), i(1)))
    assert not _compare(first, second)["equivalent"]


def _fs(*items: bytes, ref: bool = False) -> bytes:
    return _type(">", ref) + _i32(len(items)) + b"".join(items)


def _slice(start: bytes, stop: bytes, step: bytes, ref: bool = False) -> bytes:
    return _type(":", ref) + start + stop + step


@pytest.mark.parametrize("first, second", [
    (i(5), b"l" + _i32(1) + struct.pack("<H", 5)),  # TYPE_INT vs TYPE_LONG
    (t(i(1)), b"(" + _i32(1) + i(1)),  # small vs large tuple
    (z("abc"), b"a" + _i32(3) + b"abc"),  # short vs long ASCII
    (z("abc"), u("abc")),  # ASCII vs unicode
    (u("é"), b"t" + _i32(2) + "é".encode()),  # interned or not
    (z("abc"), b(b"abc")),  # str vs bytes
    (b"g" + struct.pack("<d", 0.0), b"g" + struct.pack("<d", -0.0)),
    (b"g" + bytes.fromhex("000000000000f87f"), b"g" + bytes.fromhex("010000000000f87f")),
    (t(i(1), i(2)), _fs(i(1), i(2))),  # tuple vs frozenset
    (_fs(i(1), i(2), i(3)), _slice(i(1), i(2), i(3))),
    (_slice(i(1), i(2), NONE), _slice(i(1), i(2), i(1))),
    (b"T", i(1)),
    (z(""), u("")),
])
def test_distinct_types_and_values_are_not_equivalent(first: bytes, second: bytes) -> None:
    result = _compare(_with_tail(first), _with_tail(second))
    assert result["parsed"] and not result["equivalent"], result


@pytest.mark.parametrize("flagged, inline", [
    (function(z(FILE), ref=True), function(z(FILE))),
    (_fs(i(1), i(2), ref=True), _fs(i(1), i(2))),
    (_slice(i(1), i(2), NONE, ref=True), _slice(i(1), i(2), NONE)),
])
def test_references_to_code_frozenset_and_slice_are_resolved(flagged: bytes,
                                                              inline: bytes) -> None:
    # The flagged object is reference 0 (nothing before it is flagged); the second
    # occurrence is TYPE_REF 0 in one stream and a copy in the other.
    shared = module(function(z(FILE)), z(FILE), consts_tail=t(flagged, r(0)))
    copied = module(function(z(FILE)), z(FILE), consts_tail=t(inline, inline))
    result = _compare(shared, copied)
    assert result["equivalent"], result
    assert result["installed"]["type_refs"] == 1


def test_shared_names_tuple_is_validated_once() -> None:
    # Many code records sharing one large names tuple through TYPE_REF: linear, not
    # quadratic (independent review finding).
    names = t(*[z(f"n{k}", interned=True) for k in range(250)], ref=True)
    records = [function(z(FILE), names=names)]
    records += [function(z(FILE), names=r(0)) for _ in range(250)]
    started = time.monotonic()
    data = _with_tail(t(*records))
    result = _compare(data, data)
    assert result["parsed"] and result["equivalent"]
    assert time.monotonic() - started < 5


def test_difference_walk_is_bounded() -> None:
    table = gate.NodeTable()
    a = gate.parse_pyc(_with_tail(_dag(i(1))), table).root
    c = gate.parse_pyc(_with_tail(_dag(i(2))), table).root
    found = gate.diff_nodes(table, a, c, max_pairs=10)
    assert found[-1]["kind"] == "truncated"


def test_gate4_counts_every_row(tmp_path: Path, installed: tuple[Path, Path]) -> None:  # noqa: F811
    archive, runtime_dir = installed
    ctx = _gate_context(tmp_path, archive, runtime_dir, INLINED + b"\x00", {}, gate.PASS)
    _pyc(runtime_dir, "mod").write_bytes(module(function(z(FILE)), z("/lib/changed.py")))
    result = gate.gate4_runtime(ctx)
    counts = result.evidence["recompilation"]["classification"]
    assert result.status == gate.FAIL
    assert counts == {"total": 2, gate.BYTE_IDENTICAL: 0, gate.REF_LAYOUT_EQUIVALENT: 0,
                      gate.STRUCTURAL_MISMATCH: 1, gate.REJECTED: 1}


# --- rejected: not parsable -----------------------------------------------------------------


def _with_tail(tail: bytes) -> bytes:
    return module(function(z(FILE)), z(FILE), consts_tail=tail)


def _nested(depth: int, leaf: bytes = NONE) -> bytes:
    """`depth` one-item tuples around `leaf`."""
    return b")\x01" * depth + leaf


@pytest.mark.parametrize("data, reason", [
    (module(function(z(FILE)), r(5)), "invalid reference 5"),
    (module(function(z(FILE)), r(-1)), "invalid reference -1"),
    (module(function(z(FILE)), z(FILE), ref=True, consts_tail=r(0)),
     "reference 0 to an object still being read"),
    (_with_tail(t(z("a"), r(0), ref=True)), "reference 0 to an object still being read"),
    (_with_tail(b"\xf2" + _i32(0)), "FLAG_REF on TYPE_REF"),
    (_with_tail(b"\xce"), "FLAG_REF on 'N'"),
    (INLINED[:-1], "truncated stream"),
    (INLINED[:20], "truncated stream"),
    (INLINED + b"\x00", "1 trailing bytes"),
    (_with_tail(b"[" + _i32(0)), "unsupported marshal type 0x5b"),
    (_with_tail(b"{0"), "unsupported marshal type 0x7b"),
    (_with_tail(b"<" + _i32(0)), "unsupported marshal type 0x3c"),
    (_with_tail(b"0"), "unsupported marshal type 0x30"),
    (_with_tail(b"?"), "unsupported marshal type 0x3f"),
    (_with_tail(b"S"), "unsupported marshal type 0x53"),
    (_with_tail(b"f\x031.5"), "unsupported marshal type 0x66"),
    (bytes.fromhex("610d0d0a") + INLINED[4:], "incorrect magic number 610d0d0a"),
    (INLINED[:10], "truncated header"),
    (HEADER + t(NONE), "the top-level object is not a code record"),
    (_with_tail(_nested(250)), "nested too deeply"),
    (_with_tail(b"(" + _i32(0x7FFFFFF0)), "container size 2147483632 over the limit 1000000"),
    (_with_tail(b"(" + _i32(5000)), "container size 5000 beyond the end of the stream"),
    (_with_tail(b"s" + _i32(0x7FFFFF00)), "bytes size 2147483392 over the limit 8388608"),
    (_with_tail(b"a" + _i32(-1)), "str size out of range"),
    (_with_tail(b"l" + _i32(0x7FFFFFFF)), "long size 2147483647 out of range"),
    (_with_tail(b"l" + _i32(2) + struct.pack("<HH", 1, 0)), "unnormalized long"),
    (_with_tail(b"l" + _i32(1) + struct.pack("<H", 0x8000)), "digit out of range in long"),
    (_with_tail(b"z\x02\xc3\xa9"), "non-ASCII data in an ASCII string"),
    (_with_tail(b"u" + _i32(1) + b"\xff"), "invalid UTF-8 in a string"),
    (module(function(z(FILE), bytecode=b"\x95"), z(FILE)), "co_code has an odd length"),
    (module(function(z(FILE), localspluskinds=b(b"")), z(FILE)),
     "co_localsplusnames and co_localspluskinds differ in length"),
    (module(function(z(FILE), consts=z("not a tuple")), z(FILE)), "co_consts is not a tuple"),
    (module(function(z(FILE), names=t(i(1))), z(FILE)), "co_names holds a non-str"),
    (module(function(b(b"bytes")), z(FILE)), "co_filename is not a str"),
    (module(function(z(FILE), counts=(2, 0, 0, 1, 3)), z(FILE)), "co_varnames is too small"),
    (module(function(z(FILE), counts=(0, 1, 0, 1, 0)), z(FILE)),
     "negative or inconsistent counts"),
])
def test_malformed_streams_are_rejected(data: bytes, reason: str) -> None:
    result = _compare(data, INLINED)
    assert result["parsed"] is False and result["rejected"] == "installed"
    assert reason in result["reason"], result
    flipped = _compare(INLINED, data)
    assert flipped["parsed"] is False and flipped["rejected"] == "recompiled"


def test_nesting_at_the_limit_is_accepted_and_beyond_rejected() -> None:
    # The module record and its constants tuple take two levels: the leaf of
    # _nested(n) is read at depth n + 3.
    limit = gate.MARSHAL_LIMITS.depth
    at_limit = _with_tail(_nested(limit - 3))
    assert _compare(at_limit, at_limit)["parsed"]
    assert _compare(_with_tail(_nested(limit - 2)), INLINED)["reason"] == "nested too deeply"


def test_deep_tree_through_references_is_rejected() -> None:
    # A (reference 0) is 82 levels high; G wraps 80 tuples around a reference to it: each
    # is read within the depth limit, but the resolved tree is 162 levels high.
    first = b"\xa9\x01" + _nested(80)  # ")" | FLAG_REF
    grafted = _nested(80, r(0))
    result = _compare(_with_tail(t(first, grafted)), INLINED)
    assert result["reason"] == "resolved structure nested too deeply"


def test_limits_stay_under_the_python_recursion_limit() -> None:
    limits = gate.MARSHAL_LIMITS
    assert 5 * max(limits.depth, limits.height) < sys.getrecursionlimit() - 200


def _dag(leaf: bytes, levels: int = 60) -> bytes:
    """Level k is (level k-1, level k-1) through TYPE_REF: 2**levels leaves once expanded."""
    parts = [t(leaf, ref=True)] + [t(r(k), r(k), ref=True) for k in range(levels)]
    return t(*parts)


def test_shared_subtrees_are_compared_in_linear_time() -> None:
    started = time.monotonic()
    first, second = _with_tail(_dag(i(1))), _with_tail(_dag(i(2)))
    result = _compare(first, second)
    assert not result["equivalent"] and result["differences"]
    assert time.monotonic() - started < 10


def test_excessive_object_count_is_rejected() -> None:
    limits = gate.MarshalLimits(objects=50)
    data = _with_tail(t(*[i(n) for n in range(60)]))
    result = gate.compare_structure(data, data, limits)
    assert result["reason"] == "too many objects"


def test_input_over_the_limit_is_rejected_before_parsing() -> None:
    limits = gate.MarshalLimits(input_bytes=64)
    result = gate.compare_structure(INLINED, INLINED, limits)
    assert result["parsed"] is False and "over the limit" in result["reason"]


# --- the classification of Decision 03 ------------------------------------------------------

NAME = "python/lib/python3.14/__pycache__/argparse.cpython-314.pyc"


def _approved(data: bytes) -> dict[str, str]:
    return {NAME: hashlib.sha256(data).hexdigest()}


def test_byte_identical_needs_no_parse() -> None:
    assert gate.classify_pyc(NAME, b"x", b"x", {}, None) == {
        "classification": gate.BYTE_IDENTICAL}


def test_equivalent_approved_and_verified_is_accepted() -> None:
    result = gate.classify_pyc(NAME, SHARED_AT_0, SHARED_AT_1, _approved(SHARED_AT_0), True)
    assert result["classification"] == gate.REF_LAYOUT_EQUIVALENT


@pytest.mark.parametrize("name, approved, verified, reason", [
    ("python/lib/python3.14/__pycache__/other.cpython-314.pyc", None, True,
     "equivalent, but not approved by Decision 03"),
    (NAME, {NAME: "0" * 64}, True, "equivalent, but not approved by Decision 03"),
    (NAME, {}, True, "equivalent, but not approved by Decision 03"),
    (NAME, None, None, "reader conformance not established"),
    (NAME, None, False, "reader conformance not established"),
])
def test_equivalent_but_not_approved_or_not_verified_is_rejected(
        name: str, approved: dict[str, str] | None, verified: bool | None, reason: str) -> None:
    approved = _approved(SHARED_AT_0) if approved is None else approved
    result = gate.classify_pyc(name, SHARED_AT_0, SHARED_AT_1, approved, verified)
    assert result["classification"] == gate.REJECTED and result["reason"] == reason


def test_mismatch_in_an_approved_file_is_still_a_mismatch() -> None:
    other = module(function(z(FILE)), z("/lib/other.py"))
    result = gate.classify_pyc(NAME, INLINED, other, _approved(INLINED), True)
    assert result["classification"] == gate.STRUCTURAL_MISMATCH


def test_unparsable_approved_file_is_rejected() -> None:
    result = gate.classify_pyc(NAME, INLINED, INLINED + b"\x00", _approved(INLINED), True)
    assert result["classification"] == gate.REJECTED and result["reason"] == "not parsable"


def test_approval_list_is_the_four_files_of_decision_03() -> None:
    assert sorted(Path(name).name for name in gate.REF_LAYOUT_APPROVED) == [
        "argparse.cpython-314.pyc", "inspect.cpython-314.pyc", "typing.cpython-314.pyc",
        "weakref.cpython-314.pyc"]
    assert all(len(value) == 64 for value in gate.REF_LAYOUT_APPROVED.values())


# --- the reader against an independent writer: the running interpreter's marshal ------------


def _not_interned(text: str) -> str:
    return "".join([text[:1], text[1:]])  # built at run time, so not interned


SHARED = _not_interned("shared, not an identifier!")
VALUES = [
    (1, -1, 2**31 - 1, -2**31, 2**31, -2**31 - 1, 2**100, -(2**70), 0),
    (0.0, -0.0, 1.5, float("inf")), (3j, complex(-1.5, 2.0)),
    (b"", b"\x00\xff", b"x" * 300),
    ("", "abc", "x" * 300, "été", "中" * 100, sys.intern("an_identifier")),
    (SHARED, SHARED, (SHARED, (SHARED,))),
    frozenset({1, 2, 3}), (frozenset(), frozenset({"a", (1, 2)})),
    (None, True, False, ...), tuple(range(300)),
]


@pytest.mark.parametrize("value", VALUES)
def test_reader_matches_the_interpreters_marshal_writer(value: Any) -> None:
    data = marshal.dumps(value, 4)
    table = gate.NodeTable()
    root, _ = gate.parse_marshal(data, table)
    assert gate.describe_node(table, root) == gate.describe_live(value)


def test_reader_resolves_the_writers_references() -> None:
    value = (SHARED, SHARED, (SHARED, SHARED))
    data = marshal.dumps(value, 4)
    table = gate.NodeTable()
    root, reader = gate.parse_marshal(data, table)
    assert reader.type_refs == 3 and len(reader.refs) >= 1
    inlined, plain = gate.parse_marshal(marshal.dumps(value, 2), table)  # version 2: no refs
    assert plain.type_refs == 0
    assert gate.describe_node(table, inlined) == gate.describe_node(table, root)


@pytest.mark.skipif(not PY314, reason="needs the CPython 3.14 marshal writer")
def test_reader_against_the_314_writer_through_the_oracle(tmp_path: Path) -> None:
    subprocess.run([sys.executable, "-I", "-B", "-S", str(gate.__file__), "--oracle-worker",
                    str(tmp_path)], check=True, timeout=120)
    result = gate.verify_reader(tmp_path)
    assert result["status"] == gate.PASS, result


@pytest.mark.skipif(not PY314, reason="needs the CPython 3.14 marshal writer")
def test_oracle_detects_a_reader_that_loses_a_field(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run([sys.executable, "-I", "-B", "-S", str(gate.__file__), "--oracle-worker",
                    str(tmp_path)], check=True, timeout=120)
    original = gate.describe_node

    def lossy(table: Any, node: int) -> Any:
        described = original(table, node)
        if described[0] == "code":
            described[1]["firstlineno"] = 0
        return described

    monkeypatch.setattr(gate, "describe_node", lossy)
    assert gate.verify_reader(tmp_path)["status"] == gate.FAIL


def test_oracle_failure_is_a_fail(tmp_path: Path) -> None:
    (tmp_path / "oracle.json").write_text(json.dumps({}), encoding="utf-8")
    for name in ("base", "base_extra_refs", "renamed", "variant"):
        (tmp_path / f"oracle-{name}.pyc").write_bytes(INLINED[:-1])
    assert gate.verify_reader(tmp_path)["status"] == gate.FAIL


# --- gate 4 with a reference-layout difference ----------------------------------------------


class _SubstitutingIsolation(_LocalIsolation):
    """Runs the real compile worker, then replaces its outputs by hand-built 3.14 streams
    (and the magic it reports: the interpreter running the tests may not be 3.14)."""

    replacement: dict[str, bytes] = {}

    def run(self, run: Any, argv: list[str]) -> subprocess.CompletedProcess:
        done = super().run(run, argv)
        if "--compile-worker" in argv:
            index = argv.index("--compile-worker")
            jobs = json.loads(Path(argv[index + 1]).read_text(encoding="utf-8"))
            for job in jobs:
                Path(job["cfile"]).write_bytes(self.replacement[job["installed"]])
            result = Path(argv[index + 2])
            report = json.loads(result.read_text(encoding="utf-8"))
            result.write_text(json.dumps({**report, "magic": MAGIC.hex()}), encoding="utf-8")
        return done


OTHER = f"python/lib/pkg/__pycache__/other.{TAG}.pyc"
MOD = f"python/lib/pkg/__pycache__/mod.{TAG}.pyc"


def _gate_context(tmp_path: Path, archive: Path, runtime_dir: Path, recompiled: bytes,
                  approved: dict[str, str], oracle_status: str) -> Any:
    """Installed: mod.pyc = INLINED, other.pyc = SHARED_AT_0. Recompiled: INLINED and
    `recompiled`. The oracle is replaced too, for the same reason."""
    _pyc(runtime_dir, "mod").write_bytes(INLINED)
    _pyc(runtime_dir, "other").write_bytes(SHARED_AT_0)

    def isolation(work: Path) -> Any:
        sandbox = _SubstitutingIsolation(work, [], {"PATH": "/usr/bin:/bin", "HOME": str(work)})
        sandbox.replacement = {MOD: INLINED, OTHER: recompiled}
        return sandbox

    ctx = _context(tmp_path, archive, runtime_dir, isolation=isolation,
                   ref_layout_approved=approved)
    ctx.oracle = lambda *args: {"status": oracle_status}
    return ctx


def test_gate4_accepts_an_approved_reference_layout_difference(
        tmp_path: Path, installed: tuple[Path, Path]) -> None:  # noqa: F811
    archive, runtime_dir = installed
    ctx = _gate_context(tmp_path, archive, runtime_dir, SHARED_AT_1,
                        {OTHER: hashlib.sha256(SHARED_AT_0).hexdigest()}, gate.PASS)
    result = gate.gate4_runtime(ctx)
    assert result.status == gate.PASS, result.evidence
    counts = result.evidence["recompilation"]["classification"]
    assert counts == {"total": 2, gate.BYTE_IDENTICAL: 1, gate.REF_LAYOUT_EQUIVALENT: 1,
                      gate.STRUCTURAL_MISMATCH: 0, gate.REJECTED: 0}


@pytest.mark.parametrize("approved_sha, oracle_status, recompiled, expected", [
    (None, gate.PASS, SHARED_AT_1, gate.REJECTED),  # not approved
    ("other", gate.PASS, SHARED_AT_1, gate.REJECTED),  # approved for another content
    ("same", gate.FAIL, SHARED_AT_1, gate.REJECTED),  # reader not verified
    ("same", gate.PASS, module(function(z(FILE)), z("/lib/x.py")), gate.STRUCTURAL_MISMATCH),
])
def test_gate4_rejects_what_decision_03_does_not_cover(
        tmp_path: Path, installed: tuple[Path, Path],  # noqa: F811
        approved_sha: str | None, oracle_status: str, recompiled: bytes, expected: str) -> None:
    archive, runtime_dir = installed
    approved = {} if approved_sha is None else {
        OTHER: hashlib.sha256(SHARED_AT_0 if approved_sha == "same" else b"x").hexdigest()}
    ctx = _gate_context(tmp_path, archive, runtime_dir, recompiled, approved, oracle_status)
    result = gate.gate4_runtime(ctx)
    assert result.status == gate.FAIL
    (row,) = result.evidence["recompilation"]["mismatches"]
    assert row["classification"] == expected
    assert result.evidence["recompilation"]["classification"][expected] == 1
