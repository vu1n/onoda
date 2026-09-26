"""Local content-addressed store: immutable objects, immutable records, per-run journals.

Layout (all paths relative to the store root; nothing refers to a host path):

    objects/sha256/<2 hex>/<62 hex>   raw bytes, named by their SHA-256
    records/<kind>/<id>.json          canonical JSON of one immutable record
    events/<run_id>.jsonl             append-only, hash-chained journal
    tmp/  quarantine/                 in-flight writes / recovered partial writes
    index.sqlite                      disposable; rebuilt from the above

Commit order (PORTABLE_RUN_SPEC §3): bytes are written to tmp, fsynced, hashed
and atomically renamed into place before any record may reference them, and a
record is committed before any event references it. `append_event` refuses
payload refs that are not yet committed, so a journal can never point at an
unpublished output. Existing objects and records are never overwritten.

Refs used in event payloads and checkpoints are either an object digest
("sha256:...") or a record ref "<kind>/<id>" (see `record_ref`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from labrun.canonical import canonical_bytes, is_digest, sha256_digest
from labrun.clock import Clock, SystemClock
from labrun.errors import IntegrityError, LabrunError, MissingObjectError, VisibilityError
from labrun.records import (
    RECORD_TYPES,
    ArtifactDescriptor,
    Event,
    Record,
    Visibility,
    load_record,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

CrashHook = Callable[[str], None]
"""Called at named commit boundaries; tests raise from it to simulate a crash.

Points: object.tmp-written, object.committed, record.tmp-written,
record.committed, event.before-append, event.appended.
"""


def check_safe_id(value: str, what: str = "id") -> str:
    if not _SAFE_ID.match(value):
        raise LabrunError(f"unsafe {what} {value!r}: use [A-Za-z0-9._-], max 128 chars")
    return value


def record_ref(kind: str, record_id: str) -> str:
    return f"{kind}/{record_id}"


def _id_to_filename(record_id: str) -> str:
    if is_digest(record_id):
        return record_id.split(":", 1)[1] + ".json"
    return check_safe_id(record_id, "record id") + ".json"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        clock: Clock | None = None,
        allowed_visibility: Iterable[Visibility] = ("agent",),
        crash_hook: CrashHook | None = None,
    ):
        self.root = Path(root)
        self.clock: Clock = clock or SystemClock()
        self.allowed_visibility = frozenset(allowed_visibility)
        self.crash_hook = crash_hook
        for sub in ("objects/sha256", "records", "events", "tmp", "quarantine"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def _object_path(self, digest: str) -> Path:
        if not is_digest(digest):
            raise IntegrityError(f"malformed digest {digest!r}")
        hexpart = digest.split(":", 1)[1]
        return self.root / "objects" / "sha256" / hexpart[:2] / hexpart[2:]

    def _record_path(self, kind: str, record_id: str) -> Path:
        if kind not in RECORD_TYPES:
            raise LabrunError(f"unknown record kind {kind!r}")
        return self.root / "records" / kind / _id_to_filename(record_id)

    def _journal_path(self, run_id: str) -> Path:
        return self.root / "events" / f"{check_safe_id(run_id, 'run id')}.jsonl"

    def _hook(self, point: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point)

    def _atomic_write(self, dest: Path, data: bytes, point: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "tmp" / f"{dest.name}.{os.getpid()}.{id(data):x}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        self._hook(f"{point}.tmp-written")
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
        self._hook(f"{point}.committed")

    # ---------------------------------------------------------------- objects

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        visibility: Visibility = "agent",
        encoding: str | None = None,
        units: str | None = None,
        source: dict[str, Any] | None = None,
        license: str | None = None,
        labels: Iterable[str] = (),
    ) -> ArtifactDescriptor:
        """Commit bytes and their descriptor. Idempotent for identical bytes."""
        if visibility not in self.allowed_visibility:
            raise VisibilityError(f"this store does not accept {visibility!r} artifacts")
        digest = sha256_digest(data)
        path = self._object_path(digest)
        if not path.exists():
            self._atomic_write(path, data, "object")
        if self.has_record("artifact", digest):
            return self.descriptor(digest)
        desc = ArtifactDescriptor.new(
            digest=digest,
            size=len(data),
            media_type=media_type,
            encoding=encoding,
            units=units,
            visibility=visibility,
            source=source or {},
            license=license,
            labels=sorted(set(labels)),
        )
        self.put_record(desc)
        return desc

    def put_json(self, value: Any, **kwargs: Any) -> ArtifactDescriptor:
        kwargs.setdefault("media_type", "application/json")
        return self.put_bytes(canonical_bytes(value), **kwargs)

    def has_object(self, digest: str) -> bool:
        return is_digest(digest) and self._object_path(digest).exists()

    def get_bytes(self, digest: str) -> bytes:
        path = self._object_path(digest)
        if not path.exists():
            raise MissingObjectError(digest)
        data = path.read_bytes()
        if sha256_digest(data) != digest:
            raise IntegrityError(f"object {digest} failed checksum verification")
        return data

    def get_json(self, digest: str) -> Any:
        return json.loads(self.get_bytes(digest))

    def descriptor(self, digest: str) -> ArtifactDescriptor:
        rec = self.get_record("artifact", digest)
        assert isinstance(rec, ArtifactDescriptor)
        return rec

    def iter_object_digests(self) -> Iterator[str]:
        base = self.root / "objects" / "sha256"
        for sub in sorted(base.iterdir()):
            for f in sorted(sub.iterdir()):
                yield f"sha256:{sub.name}{f.name}"

    # ---------------------------------------------------------------- records

    def put_record(self, record: Record) -> str:
        """Commit an immutable record; returns its id. Never overwrites."""
        record_id = record.record_id()
        path = self._record_path(record.kind, record_id)
        data = canonical_bytes(record.to_json())
        if path.exists():
            if path.read_bytes() != data:
                raise IntegrityError(
                    f"{record.kind} {record_id} already exists with different content"
                )
            return record_id
        self._atomic_write(path, data, "record")
        return record_id

    def has_record(self, kind: str, record_id: str) -> bool:
        return self._record_path(kind, record_id).exists()

    def get_record(self, kind: str, record_id: str) -> Record:
        path = self._record_path(kind, record_id)
        if not path.exists():
            raise MissingObjectError(record_ref(kind, record_id))
        data = json.loads(path.read_bytes())
        record = load_record(kind, data)
        if record.content_addressed and record.record_digest() != record_id:
            raise IntegrityError(f"{kind} {record_id} failed digest verification")
        return record

    def list_record_ids(self, kind: str) -> list[str]:
        base = self.root / "records" / kind
        if not base.exists():
            return []
        ids = []
        for f in sorted(base.glob("*.json")):
            stem = f.name[: -len(".json")]
            digest_named = RECORD_TYPES[kind].content_addressed or kind == "artifact"
            ids.append(f"sha256:{stem}" if digest_named else stem)
        return ids

    def ref_exists(self, ref: str) -> bool:
        if is_digest(ref):
            return self.has_object(ref)
        kind, _, record_id = ref.partition("/")
        return kind in RECORD_TYPES and bool(record_id) and self.has_record(kind, record_id)

    # ---------------------------------------------------------------- journal

    def append_event(
        self,
        run_id: str,
        *,
        actor: str,
        type: str,
        payload: dict[str, Any] | None = None,
        payload_refs: dict[str, str] | None = None,
        causes: Iterable[str] = (),
    ) -> Event:
        """Append one event. Refs must already be committed (commit-before-visible)."""
        refs = dict(payload_refs or {})
        for name, ref in refs.items():
            if not self.ref_exists(ref):
                raise MissingObjectError(ref, f"event ref {name!r} -> {ref} is not committed")
        existing = self.events(run_id)
        prev = existing[-1] if existing else None
        seq = prev.seq + 1 if prev else 1
        event = Event.new(
            event_id=f"{run_id}.e{seq:06d}",
            run_id=run_id,
            seq=seq,
            actor=actor,
            type=type,
            causes=list(causes),
            payload_refs=refs,
            payload=payload or {},
            prev_digest=prev.record_digest() if prev else None,
            ts=self.clock.now(),
        )
        line = canonical_bytes(event.to_json()) + b"\n"
        self._hook("event.before-append")
        path = self._journal_path(run_id)
        with open(path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._hook("event.appended")
        return event

    def events(self, run_id: str) -> list[Event]:
        """Read and verify a run's journal (seq continuity + hash chain)."""
        path = self._journal_path(run_id)
        if not path.exists():
            return []
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise IntegrityError(f"journal {run_id} has a torn final line; run recover()")
        out: list[Event] = []
        prev: Event | None = None
        for n, line in enumerate(raw.splitlines(), start=1):
            try:
                event = load_record("event", json.loads(line))
            except (ValueError, LabrunError) as exc:
                raise IntegrityError(f"journal {run_id} line {n} is invalid: {exc}") from exc
            assert isinstance(event, Event)
            expected_prev = prev.record_digest() if prev else None
            if event.run_id != run_id or event.seq != n or event.prev_digest != expected_prev:
                raise IntegrityError(f"journal {run_id} chain broken at line {n}")
            out.append(event)
            prev = event
        return out

    def run_ids_with_journals(self) -> list[str]:
        return sorted(p.name[: -len(".jsonl")] for p in (self.root / "events").glob("*.jsonl"))

    # --------------------------------------------------------------- recovery

    def recover(self) -> dict[str, list[str]]:
        """Quarantine partial writes left by a crash. Committed data is untouched."""
        report: dict[str, list[str]] = {"tmp": [], "torn_journals": []}
        qdir = self.root / "quarantine"
        for tmp in sorted((self.root / "tmp").iterdir()):
            shutil.move(str(tmp), qdir / tmp.name)
            report["tmp"].append(tmp.name)
        for path in sorted((self.root / "events").glob("*.jsonl")):
            raw = path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                keep, _, torn = raw.rpartition(b"\n")
                keep = keep + b"\n" if keep else b""
                (qdir / f"{path.name}.torn").write_bytes(torn)
                self._atomic_write(path, keep, "journal-repair")
                report["torn_journals"].append(path.name)
        return report

    # ------------------------------------------------------------------ index

    @property
    def index_path(self) -> Path:
        return self.root / "index.sqlite"

    def rebuild_index(self) -> Path:
        """Rebuild the disposable SQLite index from canonical files."""
        tmp = self.root / "tmp" / "index.sqlite.tmp"
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        try:
            con.execute("CREATE TABLE records (kind TEXT, id TEXT, run_id TEXT, digest TEXT)")
            con.execute("CREATE TABLE events (run_id TEXT, seq INTEGER, type TEXT, event_id TEXT)")
            for kind in RECORD_TYPES:
                for record_id in self.list_record_ids(kind):
                    rec = self.get_record(kind, record_id)
                    con.execute(
                        "INSERT INTO records VALUES (?,?,?,?)",
                        (kind, record_id, getattr(rec, "run_id", None), rec.record_digest()),
                    )
            for run_id in self.run_ids_with_journals():
                for ev in self.events(run_id):
                    con.execute(
                        "INSERT INTO events VALUES (?,?,?,?)",
                        (run_id, ev.seq, ev.type, ev.event_id),
                    )
            con.commit()
        finally:
            con.close()
        os.replace(tmp, self.index_path)
        return self.index_path

    def drop_index(self) -> None:
        self.index_path.unlink(missing_ok=True)
