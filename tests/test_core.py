"""Contract tests for canonical hashing, records, and the local store (T15 core)."""

from __future__ import annotations

import pytest

from labrun.canonical import canonical_bytes, digest_of, sha256_digest
from labrun.errors import (
    CanonicalizationError,
    IntegrityError,
    MissingObjectError,
    SchemaVersionError,
    VisibilityError,
)
from labrun.records import ArtifactDescriptor, StepSpec, load_record
from labrun.store import Store


class Crash(Exception):
    pass


def test_rfc8785_key_order_is_utf16():
    # RFC 8785 §3.2.3 sorting example.
    value = {"\u20ac": 0, "\r": 1, "\ufb33": 2, "1": 3, "\U0001f600": 4, "\u0080": 5, "\u00f6": 6}
    out = canonical_bytes(value).decode("utf-8")
    keys = [k for k in value]
    order = sorted(keys, key=lambda k: out.index('"' + k.replace("\r", "\\r") + '"'))
    assert order == ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001f600", "\ufb33"]


def test_rfc8785_string_escapes():
    assert canonical_bytes('\u000f\u2028"\\é') == b'"\\u000f\xe2\x80\xa8\\"\\\\\xc3\xa9"'


def test_floats_and_unsafe_ints_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"energy": -1.5})
    with pytest.raises(CanonicalizationError):
        canonical_bytes([2**53])
    assert canonical_bytes({"energy_hartree": "-76.0267"}) == b'{"energy_hartree":"-76.0267"}'


def test_object_roundtrip_and_corruption(tmp_path):
    store = Store(tmp_path)
    desc = store.put_bytes(b"hello", media_type="text/plain")
    assert desc.digest == sha256_digest(b"hello")
    assert store.get_bytes(desc.digest) == b"hello"
    path = store._object_path(desc.digest)
    path.write_bytes(b"hellp")
    with pytest.raises(IntegrityError):
        store.get_bytes(desc.digest)
    with pytest.raises(MissingObjectError):
        store.get_bytes(sha256_digest(b"absent"))


def test_agent_store_refuses_evaluator_artifacts(tmp_path):
    with pytest.raises(VisibilityError):
        Store(tmp_path).put_bytes(b"ref", media_type="text/plain", visibility="evaluator")


def test_records_are_immutable(tmp_path):
    store = Store(tmp_path)
    spec = StepSpec.new(operation="parse", inputs={}, code_ref="x@1", determinism="pure")
    sid = store.put_record(spec)
    assert sid == spec.record_digest()
    assert store.put_record(spec) == sid
    assert store.get_record("step", sid) == spec


def test_event_refs_must_be_committed(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(MissingObjectError):
        store.append_event("r1", actor="t", type="x", payload_refs={"o": sha256_digest(b"nope")})
    desc = store.put_bytes(b"ok", media_type="text/plain")
    e1 = store.append_event("r1", actor="t", type="x", payload_refs={"o": desc.digest})
    e2 = store.append_event("r1", actor="t", type="y", causes=[e1.event_id])
    assert [e.seq for e in store.events("r1")] == [1, 2]
    assert e2.prev_digest == e1.record_digest()


def test_journal_tamper_detected(tmp_path):
    store = Store(tmp_path)
    store.append_event("r1", actor="t", type="a")
    store.append_event("r1", actor="t", type="b")
    path = store._journal_path("r1")
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0].replace(b'"a"', b'"z"') + lines[1])
    with pytest.raises(IntegrityError):
        store.events("r1")


def test_torn_journal_recovered(tmp_path):
    store = Store(tmp_path)
    store.append_event("r1", actor="t", type="a")
    with open(store._journal_path("r1"), "ab") as fh:
        fh.write(b'{"partial":')
    with pytest.raises(IntegrityError):
        store.events("r1")
    report = store.recover()
    assert report["torn_journals"] == ["r1.jsonl"]
    assert len(store.events("r1")) == 1


@pytest.mark.parametrize("point", ["object.tmp-written", "record.tmp-written"])
def test_crash_mid_commit_leaves_nothing_visible(tmp_path, point):
    def hook(p):
        if p == point:
            raise Crash(p)

    store = Store(tmp_path, crash_hook=hook)
    with pytest.raises(Crash):
        store.put_bytes(b"data", media_type="text/plain")
    clean = Store(tmp_path)
    clean.recover()
    digest = sha256_digest(b"data")
    # Either the object never landed, or it landed without a descriptor; an
    # event can reference neither a missing object nor treat it as described.
    assert not clean.has_record("artifact", digest)
    assert list((tmp_path / "tmp").iterdir()) == []


def test_unknown_schema_rejected_and_migration_is_new_record(tmp_path):
    with pytest.raises(SchemaVersionError):
        load_record("artifact", {"schema": "labrun.artifact/9", "digest": "x"})
    old = {"schema": "labrun.artifact/0", "sha256": "ab" * 32, "size": 3, "media_type": "t/p"}
    old_digest = digest_of(old)
    with pytest.raises(SchemaVersionError):
        load_record("artifact", old)
    new = load_record("artifact", old, migrate=True)
    assert isinstance(new, ArtifactDescriptor)
    assert new.digest == "sha256:" + "ab" * 32
    assert digest_of(old) == old_digest  # source dict untouched


def test_index_is_disposable(tmp_path):
    store = Store(tmp_path)
    store.put_bytes(b"x", media_type="text/plain")
    store.append_event("r1", actor="t", type="a")
    store.rebuild_index()
    store.drop_index()
    assert not store.index_path.exists()
    assert len(store.events("r1")) == 1
    store.rebuild_index()
    assert store.index_path.exists()
