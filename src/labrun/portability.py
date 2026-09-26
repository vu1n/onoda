"""Portable bundles: export, data-only import, capability inspection (PORTABLE_RUN_SPEC §4, §11).

Bundle layout (every path relative; nothing names a host path):

    manifest.json                  format, profile, roots, ancestor cuts, inventory, capabilities
    schemas/<kind>.schema.json     JSON schema of each record kind
    records/<kind>/<id>.json       canonical record bytes, named as in the store
    events/<run_id>.jsonl          journal; an ancestor only up to its visibility cut
    objects/sha256/<2>/<62>        raw bytes
    environment/environment.json   interpreter, platform, lock digest, code revision
    environment/lockfiles/uv.lock
    provenance/{sources,licenses,omissions}.json
    README.md

The inventory lists every file except manifest.json with size and digest.
Closure is computed by walking references: digests, "<kind>/<id>" record refs,
invocation ids in `frontier`/`parents`/`derived_from`, and `Run.task`. An
ancestor run contributes only its events up to the cut declared by the fork
(ForkDeclaration.visibility_cut, Checkpoint.ancestry_cut, else the fork
checkpoint's event_seq) unless it is itself a requested root.

Import parses JSON and copies bytes. It never unpickles, executes, installs or
fetches; objects a bundle omits stay declared omissions (recorded under
`<store>/imports/<manifest hex>.json`) that `inspect_capabilities` reports.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from labrun.canonical import canonical_bytes, is_digest, sha256_digest
from labrun.errors import (
    ImportRejected,
    IntegrityError,
    LabrunError,
    MissingObjectError,
    SchemaVersionError,
    VisibilityError,
)
from labrun.records import (
    RECORD_TYPES,
    ArtifactDescriptor,
    Checkpoint,
    Event,
    ForkDeclaration,
    InvocationReceipt,
    Record,
    Run,
    StepSpec,
    load_record,
)
from labrun.store import Store, check_safe_id, record_ref

BUNDLE_FORMAT = "labrun.bundle/1"
Profile = Literal["agent-visible", "evaluator", "redacted"]
PROFILES: tuple[str, ...] = ("agent-visible", "evaluator", "redacted")
CAPABILITIES: tuple[str, ...] = ("inspect", "reanalyze", "reexecute", "resume", "fork")
SUPPORTED = "supported"
MISSING_INPUT = "blocked-by-missing-input"
ENVIRONMENT = "blocked-by-environment"
PROVIDER_STATE = "unavailable-provider-state"

# What a reference's role in a record means for each capability if its bytes are absent.
_ROLE_CAPS: dict[str, tuple[str, ...]] = {
    "state": ("inspect", "resume", "fork"),
    "input": ("reanalyze", "reexecute"),
    "output": ("reanalyze",),
    "other": ("inspect",),
}
_REF_KINDS = tuple(k for k in RECORD_TYPES if k != "event")
_RECORD_REF = re.compile(rf"^(?:{'|'.join(_REF_KINDS)})/[A-Za-z0-9:._-]+$")
_SKIP_KEYS = frozenset(
    {
        "schema",
        "event_id",
        "run_id",
        "new_run_id",
        "prev_digest",
        "causes",
        "ancestry_cut",
        "visibility_cut",
        "type",
        "actor",
        "ts",
        "started_at",
        "finished_at",
        "created_at",
    }
)
_INVOCATION_KEYS = frozenset({"frontier", "parents", "derived_from"})
_CONTENT_KINDS = tuple(k for k, c in RECORD_TYPES.items() if c.content_addressed)
_SAFE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_LAYOUT = re.compile(
    r"^(?:README\.md"
    r"|schemas/[a-z]+\.schema\.json"
    rf"|records/(?:{'|'.join(RECORD_TYPES)})/{_SAFE}\.json"
    rf"|events/{_SAFE}\.jsonl"
    r"|objects/sha256/[0-9a-f]{2}/[0-9a-f]{62}"
    r"|environment/environment\.json"
    r"|environment/lockfiles/[A-Za-z0-9._-]+\.lock"
    r"|provenance/(?:sources|licenses|omissions)\.json)$"
)


# ------------------------------------------------------------------ references


def _as_ref(kind: str, key: str, value: str) -> str | None:
    if key in _INVOCATION_KEYS and not is_digest(value) and not _RECORD_REF.match(value):
        return record_ref("invocation", value)
    if kind == "run" and key == "task":
        return record_ref("task", value)
    if is_digest(value) or _RECORD_REF.match(value):
        return value
    return None


def _role(kind: str, key: str) -> str:
    if kind in ("event", "checkpoint"):
        return "state"
    if key == "inputs":
        return "input"
    if key in ("outputs", "output_refs"):
        return "output"
    return "other"


def extract_refs(kind: str, data: dict[str, Any]) -> list[tuple[str, str]]:
    """(ref, role) pairs a record dict points at. Descriptors point at nothing."""
    if kind == "artifact":
        return []
    out: list[tuple[str, str]] = []

    def walk(value: Any, key: str, role: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in _SKIP_KEYS:
                    walk(v, k, role)
        elif isinstance(value, list):
            for v in value:
                walk(v, key, role)
        elif isinstance(value, str):
            ref = _as_ref(kind, key, value)
            if ref is not None:
                out.append((ref, role))

    for key, value in data.items():
        if key not in _SKIP_KEYS:
            walk(value, key, _role(kind, key))
    return out


class _View(Protocol):
    def record(self, kind: str, record_id: str) -> Record | None: ...
    def has_object(self, digest: str) -> bool: ...
    def events(self, run_id: str) -> list[Event]: ...
    def checkpoints(self) -> list[Checkpoint]: ...


class _StoreView:
    def __init__(self, *stores: Store):
        self.stores = stores
        self._checkpoints: list[Checkpoint] | None = None

    def record(self, kind: str, record_id: str) -> Record | None:
        for s in self.stores:
            try:
                present = s.has_record(kind, record_id)
            except LabrunError:
                return None
            if present:
                return s.get_record(kind, record_id)
        return None

    def object_owner(self, digest: str) -> Store | None:
        return next((s for s in self.stores if s.has_object(digest)), None)

    def has_object(self, digest: str) -> bool:
        return self.object_owner(digest) is not None

    def events(self, run_id: str) -> list[Event]:
        return self.stores[0].events(run_id)

    def checkpoints(self) -> list[Checkpoint]:
        if self._checkpoints is None:
            self._checkpoints = [
                s.checkpoint(cid) for s in self.stores for cid in s.list_record_ids("checkpoint")
            ]
        return self._checkpoints


@dataclass
class _BundleView:
    records: dict[tuple[str, str], Record]
    objects: set[str]
    journals: dict[str, list[Event]]

    def record(self, kind: str, record_id: str) -> Record | None:
        return self.records.get((kind, record_id))

    def has_object(self, digest: str) -> bool:
        return digest in self.objects

    def events(self, run_id: str) -> list[Event]:
        return self.journals.get(run_id, [])

    def checkpoints(self) -> list[Checkpoint]:
        return [r for r in self.records.values() if isinstance(r, Checkpoint)]


@dataclass
class _Closure:
    cuts: dict[str, int | None]
    records: dict[str, Record] = field(default_factory=dict)
    objects: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    roles: dict[str, set[str]] = field(default_factory=dict)
    events: dict[str, list[Event]] = field(default_factory=dict)

    def affected(self, ref: str) -> list[str]:
        roles = self.roles.get(ref)
        if not roles:
            return list(CAPABILITIES)
        return [c for c in CAPABILITIES if any(c in _ROLE_CAPS[r] for r in roles)]


def _ancestor_cuts(view: _View, run_ids: Iterable[str]) -> dict[str, int | None]:
    """Requested runs map to None (whole journal); ancestors to their max visible seq."""
    cuts: dict[str, int | None] = {r: None for r in run_ids}
    stack = list(cuts)
    while stack:
        run = view.record("run", stack.pop())
        if not isinstance(run, Run) or run.parent is None:
            continue
        cp = view.record("checkpoint", run.parent.checkpoint)
        fork = view.record("fork", run.fork) if run.fork else None
        cut_map = dict(cp.ancestry_cut) if isinstance(cp, Checkpoint) else {}
        if isinstance(fork, ForkDeclaration):
            cut_map.update(fork.visibility_cut)
        if isinstance(cp, Checkpoint):
            cut_map.setdefault(run.parent.run_id, cp.event_seq)
        for anc, seq in cut_map.items():
            prev = cuts.get(anc, -1)
            if prev is None or prev >= seq:
                continue
            cuts[anc] = seq
            stack.append(anc)
    return cuts


def _closure(view: _View, run_ids: Iterable[str], extra_roots: Iterable[str] = ()) -> _Closure:
    c = _Closure(cuts=_ancestor_cuts(view, run_ids))
    queue: list[str] = []
    seen: set[str] = set()

    def add(ref: str, role: str | None) -> None:
        if role is not None:
            c.roles.setdefault(ref, set()).add(role)
        queue.append(ref)

    for run_id, cut in sorted(c.cuts.items()):
        add(record_ref("run", run_id), "state")
        events = view.events(run_id)
        if cut is not None:
            if len(events) < cut:
                c.missing.add(f"events/{run_id}@{cut}")
            events = [e for e in events if e.seq <= cut]
        c.events[run_id] = events
        for ev in events:
            for ref, role in extract_refs("event", ev.to_json()):
                add(ref, role)
        for cp in view.checkpoints():
            if cp.run_id == run_id and (cut is None or cp.event_seq <= cut):
                add(record_ref("checkpoint", cp.record_digest()), "state")
    for ref in extra_roots:
        add(ref, "other")

    while queue:
        ref = queue.pop()
        if ref in seen:
            continue
        seen.add(ref)
        if is_digest(ref):
            if view.has_object(ref):
                c.objects.add(ref)
                add(record_ref("artifact", ref), None)
                continue
            kind = next((k for k in _CONTENT_KINDS if view.record(k, ref) is not None), None)
            if kind is not None:
                add(record_ref(kind, ref), None)
                continue
            c.missing.add(ref)
            # An omitted object's descriptor still travels, so the bundle says what is absent.
            if view.record("artifact", ref) is not None:
                add(record_ref("artifact", ref), None)
            continue
        kind, _, record_id = ref.partition("/")
        rec = view.record(kind, record_id)
        if rec is None:
            c.missing.add(ref)
            continue
        c.records[ref] = rec
        for sub, role in extract_refs(kind, rec.to_json()):
            add(sub, role)
    return c


# ---------------------------------------------------------------- capabilities


def _entry(status: str, reason: str, missing: list[str] | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "missing": missing or []}


def _capabilities(
    view: _View,
    run_id: str,
    *,
    reasons: dict[str, str],
    env: dict[str, Any],
    absent: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    if view.record("run", run_id) is None and not view.events(run_id):
        raise MissingObjectError(record_ref("run", run_id))
    c = _closure(view, [run_id])
    missing = c.missing | (set(absent) & c.objects)
    blocked: dict[str, list[str]] = {cap: [] for cap in CAPABILITIES}
    for ref in sorted(missing):
        for cap in c.affected(ref):
            blocked[cap].append(ref)

    invocations = [r for r in c.records.values() if isinstance(r, InvocationReceipt)]
    steps = [(inv, c.records.get(record_ref("step", inv.spec_digest))) for inv in invocations]
    checkpoints = sorted(
        (r for r in c.records.values() if isinstance(r, Checkpoint) and r.run_id == run_id),
        key=lambda cp: cp.event_seq,
    )
    out: dict[str, dict[str, Any]] = {}
    out["inspect"] = _entry(
        SUPPORTED,
        f"{sum(len(e) for e in c.events.values())} recorded events and checkpoint state "
        "reconstruct offline; no provider or network access",
    )
    pure = sorted(
        {s.operation for _, s in steps if isinstance(s, StepSpec) and s.determinism == "pure"}
    )
    out["reanalyze"] = (
        _entry(SUPPORTED, "pure transforms rerun on recorded bytes: " + ", ".join(pure))
        if pure
        else _entry(MISSING_INPUT, "no pure derivation step is recorded")
    )

    offline: list[str] = []
    env_blocked: list[str] = []
    provider: list[str] = []
    for inv, step in steps:
        if not isinstance(step, StepSpec):
            continue
        fixture = "fixture" in inv.labels or step.code_ref.startswith(
            ("fixture", "labrun.fixture:")
        )
        live = "live" in inv.labels or (
            not fixture and (step.determinism == "external" or "external" in inv.labels)
        )
        if fixture and not live:
            offline.append(step.operation)
            continue
        if inv.invocation_kind == "decision" and inv.metadata.get("provider_state") != "known":
            provider.append(f"{inv.invocation_id} ({step.operation})")
            continue
        unmet = {k: v for k, v in step.environment.items() if env.get(k) != v}
        if unmet or (live and not step.environment):
            need = ", ".join(f"{k}={v}" for k, v in sorted(unmet.items())) or "undeclared"
            env_blocked.append(f"{step.operation} needs {need}")
            continue
        offline.append(step.operation)
    if provider:
        out["reexecute"] = _entry(
            PROVIDER_STATE,
            "recorded model calls with unknown hidden provider state: " + "; ".join(provider),
        )
    elif env_blocked:
        out["reexecute"] = _entry(ENVIRONMENT, "; ".join(env_blocked))
    elif offline:
        out["reexecute"] = _entry(
            SUPPORTED, "steps reexecute offline: " + ", ".join(sorted(set(offline)))
        )
    else:
        out["reexecute"] = _entry(MISSING_INPUT, "no recorded invocation to reexecute")

    if checkpoints:
        cp = checkpoints[-1]
        out["resume"] = _entry(
            SUPPORTED,
            f"semantic restart from checkpoint {cp.record_digest()} ({cp.boundary}); "
            f"native session resume {cp.capabilities.get('native_resume', 'unsupported')}",
        )
    else:
        out["resume"] = _entry(MISSING_INPUT, "no checkpoint is recorded for this run")
    quiescent = [cp for cp in checkpoints if cp.quiescent]
    out["fork"] = (
        _entry(SUPPORTED, f"{len(quiescent)} quiescent checkpoint(s) available")
        if quiescent
        else _entry(MISSING_INPUT, "no quiescent checkpoint is recorded for this run")
    )

    for cap, refs in blocked.items():
        if refs:
            why = "; ".join(f"{r}: {reasons.get(r, 'absent and undeclared')}" for r in refs)
            out[cap] = _entry(MISSING_INPUT, f"missing inputs ({why})", refs)
    return out


def _stored_omissions(store: Store) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    base = store.root / "imports"
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            for entry in json.loads(path.read_bytes()).get("omissions", []):
                out[entry["digest"]] = entry
    return out


def _imported_prefixes(store: Store) -> dict[str, int]:
    """Runs this store holds only as an ancestor prefix cut by an earlier import."""
    out: dict[str, int] = {}
    base = store.root / "imports"
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            out.update(json.loads(path.read_bytes()).get("ancestors", {}))
    return out


def inspect_capabilities(
    store: Store, run_id: str, *, available_environment: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Per capability: {"status", "reason", "missing"}. Offline; never fetches."""
    reasons = {d: e.get("reason", "") for d, e in _stored_omissions(store).items()}
    return _capabilities(
        _StoreView(store), run_id, reasons=reasons, env=dict(available_environment or {})
    )


# ---------------------------------------------------------------------- export


def _record_file(kind: str, record_id: str) -> str:
    name = record_id.split(":", 1)[1] if is_digest(record_id) else check_safe_id(record_id)
    return f"records/{kind}/{name}.json"


def _object_file(digest: str) -> str:
    hexpart = digest.split(":", 1)[1]
    return f"objects/sha256/{hexpart[:2]}/{hexpart[2:]}"


def _json_file(value: Any) -> bytes:
    # Indented, human-readable metadata; records and journals use canonical_bytes instead.
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _git_revision(root: Path) -> dict[str, Any] | None:
    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        return None
    try:
        run = [git, "-C", str(root)]
        head = subprocess.run(
            [*run, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        status = subprocess.run(
            [*run, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": head, "dirty": bool(status.strip())}


def _environment() -> dict[str, bytes]:
    """Interpreter, platform, lock and revision. Never env vars, hostnames or host paths."""
    files: dict[str, bytes] = {}
    env: dict[str, Any] = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "environment_variables": "excluded: credentials and host identifiers are never exported",
        "lockfile": None,
        "code_revision": None,
    }
    root = Path(__file__).resolve().parents[2]
    lock = root / "uv.lock"
    if lock.is_file() and (root / "pyproject.toml").is_file():
        data = lock.read_bytes()
        files["environment/lockfiles/uv.lock"] = data
        env["lockfile"] = {"path": "environment/lockfiles/uv.lock", "digest": sha256_digest(data)}
        env["code_revision"] = _git_revision(root)
    files["environment/environment.json"] = _json_file(env)
    return files


def _readme(manifest: dict[str, Any], omissions: list[dict[str, Any]]) -> bytes:
    lines = [
        "# labrun bundle",
        "",
        f"- format: {manifest['format']}",
        f"- profile: {manifest['profile']}",
        f"- runs: {', '.join(manifest['roots'])}",
        "- ancestors (visible prefix only): "
        + (", ".join(f"{r} <= seq {s}" for r, s in manifest["ancestors"].items()) or "none"),
        f"- exact replay claimed: {'yes' if manifest['exact_replay'] else 'no'}",
        f"- labels: {', '.join(manifest['labels'])}",
        "",
        "## Capabilities",
        "",
    ]
    for run_id, caps in manifest["capabilities"].items():
        for cap, entry in caps.items():
            lines.append(f"- {run_id} {cap}: {entry['status']} ({entry['reason']})")
    lines += ["", "## Omissions", ""]
    lines += [
        f"- {o['digest']}: {o['reason']} (affects: {', '.join(o['affected_capabilities'])})"
        for o in omissions
    ] or ["none"]
    lines += [
        "",
        "Import is data-only: checksums, schemas, closure and paths are validated; nothing",
        "is executed, unpickled, installed or fetched. Omitted objects are never fetched.",
        "",
    ]
    return "\n".join(lines).encode()


def _write_archive(out: Path, files: dict[str, bytes]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(files):
            data = files[path]
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    with open(out, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz:
            gz.write(buf.getvalue())


def _check_leaks(
    files: dict[str, bytes], secrets: Iterable[str], protected: list[tuple[str, bytes]]
) -> None:
    needles = [s.encode() for s in secrets if s]
    for path, data in sorted(files.items()):
        if any(n in data for n in needles):
            raise VisibilityError(f"credential material found in {path}; export refused")
        for digest, content in protected:
            if digest.split(":", 1)[1].encode() in data or (len(content) >= 8 and content in data):
                raise VisibilityError(f"evaluator-protected object {digest} would leak via {path}")


def export_bundle(
    store: Store,
    run_ids: list[str],
    out: str | os.PathLike[str],
    *,
    profile: Profile = "agent-visible",
    evaluator_store: Store | None = None,
    archive: bool | None = None,
    omit: dict[str, str] | None = None,
    redact: dict[str, bytes] | None = None,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """Write a bundle (directory, or deterministic .tar.gz) and return its manifest.

    `omit` maps object digest -> reason (thin bundle). `redact` maps object
    digest -> replacement bytes and requires the "redacted" profile; the result
    is a derivative that claims no exact replay. Any `secrets` value found in
    exported bytes refuses the export under every profile.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    redact = dict(redact or {})
    if redact and profile != "redacted":
        raise ValueError("redaction requires profile='redacted'")
    if profile == "evaluator" and evaluator_store is None:
        raise ValueError("the evaluator profile needs evaluator_store")
    out = Path(out)
    if out.exists():
        raise LabrunError(f"export target {out.name!r} already exists")
    if archive is None:
        archive = out.name.endswith((".tar.gz", ".tgz"))
    roots = sorted({check_safe_id(r, "run id") for r in run_ids})
    if not roots:
        raise ValueError("no runs to export")

    extra: list[str] = []
    if profile == "evaluator":
        assert evaluator_store is not None
        view = _StoreView(store, evaluator_store)
        for eid in evaluator_store.list_record_ids("evaluation"):
            if getattr(evaluator_store.get_record("evaluation", eid), "run_id", None) in roots:
                extra.append(record_ref("evaluation", eid))
    else:
        view = _StoreView(store)
    closure = _closure(view, roots, extra)

    stored = _stored_omissions(store)
    omissions: dict[str, dict[str, Any]] = {}
    for digest, reason in [*(omit or {}).items(), *((d, "redacted") for d in redact)]:
        if digest not in closure.objects:
            raise ValueError(f"cannot omit {digest}: not an object in the export closure")
        omissions[digest] = {"digest": digest, "reason": reason, "acquisition": "operator-supplied"}
    for ref in sorted(closure.missing):
        if ref not in stored:
            raise MissingObjectError(ref, f"export closure incomplete: {ref} is not in the store")
        omissions[ref] = dict(stored[ref])
    for digest, entry in omissions.items():
        entry["affected_capabilities"] = closure.affected(digest)

    if profile != "evaluator":
        for ref, rec in closure.records.items():
            if ref.startswith("evaluation/") or (
                isinstance(rec, ArtifactDescriptor) and rec.visibility != "agent"
            ):
                raise VisibilityError(f"{ref} is not agent-visible")

    files: dict[str, bytes] = {}
    sources: dict[str, Any] = {}
    licenses: dict[str, str] = {}
    for ref, rec in closure.records.items():
        kind, _, record_id = ref.partition("/")
        if kind == "artifact" and record_id in redact:
            continue
        files[_record_file(kind, record_id)] = canonical_bytes(rec.to_json())
        if isinstance(rec, ArtifactDescriptor):
            sources[rec.digest] = rec.source
            licenses[rec.digest] = rec.license or "unspecified"
    for digest in sorted(closure.objects - omissions.keys()):
        owner = view.object_owner(digest)
        assert owner is not None
        files[_object_file(digest)] = owner.get_bytes(digest)
    for digest, data in sorted(redact.items()):
        original = closure.records.get(record_ref("artifact", digest))
        desc = ArtifactDescriptor.new(
            digest=sha256_digest(data),
            size=len(data),
            media_type=getattr(original, "media_type", "application/octet-stream"),
            source={"redacted_from": digest},
            labels=["redacted"],
        )
        files[_object_file(desc.digest)] = data
        files[_record_file("artifact", desc.digest)] = canonical_bytes(desc.to_json())
        omissions[digest]["replacement"] = desc.digest
        sources[desc.digest] = desc.source
        licenses[desc.digest] = "unspecified"
    for run_id, events in closure.events.items():
        files[f"events/{run_id}.jsonl"] = b"".join(
            canonical_bytes(e.to_json()) + b"\n" for e in events
        )
    for kind, cls in RECORD_TYPES.items():
        files[f"schemas/{kind}.schema.json"] = _json_file(cls.model_json_schema(by_alias=True))
    files.update(_environment())
    omission_list = [omissions[k] for k in sorted(omissions)]
    files["provenance/sources.json"] = _json_file(sources)
    files["provenance/licenses.json"] = _json_file(licenses)
    files["provenance/omissions.json"] = _json_file(omission_list)

    reasons = {d: e["reason"] for d, e in omissions.items()}
    capabilities = {
        r: _capabilities(view, r, reasons=reasons, env={}, absent=omissions) for r in roots
    }
    if redact:
        for caps in capabilities.values():
            if caps["inspect"]["status"] == SUPPORTED:
                caps["inspect"]["reason"] = (
                    "derivative bundle: redacted objects replaced; history is inspectable "
                    "but replay is not exact"
                )
    labels = [
        {"agent-visible": "agent-visible", "evaluator": "evaluator-authorized"}.get(
            profile, "redacted-derivative"
        )
    ]
    if omissions:
        labels.append("thin")
    manifest: dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "profile": profile,
        "roots": roots,
        "ancestors": {r: cut for r, cut in sorted(closure.cuts.items()) if cut is not None},
        "exact_replay": not redact
        and all(caps["inspect"]["status"] == SUPPORTED for caps in capabilities.values()),
        "capabilities": capabilities,
        "omissions": sorted(omissions),
        "labels": sorted(labels),
    }
    files["README.md"] = _readme(manifest, omission_list)

    protected: list[tuple[str, bytes]] = []
    if profile != "evaluator" and evaluator_store is not None:
        protected = [
            (d, evaluator_store.get_bytes(d))
            for d in evaluator_store.iter_object_digests()
            if not store.has_object(d)
        ]
    _check_leaks(files, secrets, protected)

    manifest["inventory"] = [
        {"path": p, "size": len(files[p]), "digest": sha256_digest(files[p])} for p in sorted(files)
    ]
    files["manifest.json"] = _json_file(manifest)
    if archive:
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_archive(out, files)
    else:
        for path, data in sorted(files.items()):
            dest = out / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    return manifest


# ---------------------------------------------------------------------- import


@dataclass(frozen=True)
class ImportReport:
    bundle_digest: str
    profile: str
    roots: list[str]
    ancestors: dict[str, int]
    objects: int
    records: int
    events: dict[str, int]
    omissions: list[dict[str, Any]]
    exact_replay: bool
    capabilities: dict[str, dict[str, dict[str, Any]]]


def _safe_path(name: str) -> str:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or any(p in ("", ".", "..") for p in name.split("/"))
    ):
        raise ImportRejected(f"unsafe path {name!r} in bundle")
    return name


def _read_dir(root: Path, max_files: int, max_bytes: int) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    total = 0
    stack: list[tuple[str, str]] = [(str(root), "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            rel = _safe_path(prefix + entry.name)
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise ImportRejected(f"symlink {rel!r} in bundle")
            if stat.S_ISDIR(st.st_mode):
                stack.append((entry.path, rel + "/"))
                continue
            if not stat.S_ISREG(st.st_mode):
                raise ImportRejected(f"{rel!r} is not a regular file")
            if st.st_nlink > 1:
                raise ImportRejected(f"hardlink {rel!r} in bundle")
            if len(out) >= max_files:
                raise ImportRejected(f"bundle has more than {max_files} files")
            total += st.st_size
            if total > max_bytes:
                raise ImportRejected(f"bundle content exceeds {max_bytes} bytes")
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(st.st_size + 1)
            if len(data) != st.st_size:
                raise ImportRejected(f"{rel!r} changed while being read")
            out[rel] = data
    return out


class _Limited:
    """Counts decompressed bytes; raises once the expansion budget is spent."""

    def __init__(self, fh: gzip.GzipFile, limit: int):
        self.fh = fh
        self.limit = limit
        self.count = 0

    def read(self, n: int = -1) -> bytes:
        data = self.fh.read(n)
        self.count += len(data)
        if self.count > self.limit:
            raise ImportRejected(f"archive expands beyond {self.limit} bytes")
        return data


def _read_archive(
    path: Path, max_files: int, max_bytes: int, max_expansion_ratio: int
) -> dict[str, bytes]:
    budget = path.stat().st_size * max_expansion_ratio
    out: dict[str, bytes] = {}
    total = 0
    with open(path, "rb") as raw:
        if raw.read(2) != b"\x1f\x8b":
            raise ImportRejected("bundle is neither a directory nor a .tar.gz archive")
        raw.seek(0)
        stream = _Limited(gzip.GzipFile(fileobj=raw, mode="rb"), budget)
        try:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for m in tar:
                    if m.isdir():
                        _safe_path(m.name.rstrip("/"))
                        continue
                    name = _safe_path(m.name)
                    if m.issym() or m.islnk():
                        raise ImportRejected(f"link entry {name!r} in bundle")
                    if not m.isreg() or m.issparse():
                        raise ImportRejected(f"{name!r} is not a regular file")
                    if name in out:
                        raise ImportRejected(f"duplicate entry {name!r} in bundle")
                    if len(out) >= max_files:
                        raise ImportRejected(f"bundle has more than {max_files} files")
                    total += m.size
                    if total > max_bytes:
                        raise ImportRejected(f"bundle content exceeds {max_bytes} bytes")
                    if stream.count + m.size > budget:
                        raise ImportRejected(
                            f"archive expansion exceeds ratio {max_expansion_ratio}"
                        )
                    fh = tar.extractfile(m)
                    assert fh is not None
                    out[name] = fh.read()
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            raise ImportRejected(f"unreadable archive: {exc}") from exc
    return out


def _parse_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    raw = files.get("manifest.json")
    if raw is None:
        raise ImportRejected("bundle has no manifest.json")
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise ImportRejected("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ImportRejected("manifest.json is not an object")
    if manifest.get("format") != BUNDLE_FORMAT:
        raise SchemaVersionError(
            f"bundle format {manifest.get('format')!r} is not supported; "
            f"this reader supports {BUNDLE_FORMAT!r}"
        )
    if manifest.get("profile") not in PROFILES:
        raise ImportRejected(f"unknown bundle profile {manifest.get('profile')!r}")
    roots = manifest.get("roots")
    inventory = manifest.get("inventory")
    if not isinstance(roots, list) or not roots or not isinstance(inventory, list):
        raise ImportRejected("manifest needs roots and inventory lists")
    for r in roots:
        if not isinstance(r, str) or not re.fullmatch(_SAFE, r):
            raise ImportRejected(f"unsafe run id {r!r} in manifest")
    for e in inventory:
        if not (
            isinstance(e, dict)
            and isinstance(e.get("path"), str)
            and isinstance(e.get("size"), int)
            and isinstance(e.get("digest"), str)
        ):
            raise ImportRejected("malformed inventory entry")
    return manifest


def _check_inventory(files: dict[str, bytes], manifest: dict[str, Any]) -> None:
    inventory = {e["path"]: e for e in manifest["inventory"]}
    if len(inventory) != len(manifest["inventory"]):
        raise ImportRejected("inventory lists a path twice")
    present = set(files) - {"manifest.json"}
    for path in sorted(present - inventory.keys()):
        raise ImportRejected(f"{path!r} is not in the manifest inventory")
    for path in sorted(inventory.keys() - present):
        raise ImportRejected(f"inventoried file {path!r} is missing from the bundle")
    for path in sorted(present):
        if not _LAYOUT.match(path):
            raise ImportRejected(f"{path!r} is outside the bundle layout")
        data = files[path]
        if path.startswith("objects/"):
            digest = "sha256:" + path.split("/", 2)[2].replace("/", "")
            if sha256_digest(data) != digest:
                raise IntegrityError(f"object {digest} failed checksum verification")
        entry = inventory[path]
        if len(data) != entry["size"] or sha256_digest(data) != entry["digest"]:
            raise IntegrityError(f"{path} does not match its inventory size and digest")


def _ref_key(ref: str) -> tuple[str, str]:
    kind, _, record_id = ref.partition("/")
    return kind, record_id


def _parse_journal(run_id: str, raw: bytes) -> list[Event]:
    if raw and not raw.endswith(b"\n"):
        raise IntegrityError(f"journal {run_id} has a torn final line")
    out: list[Event] = []
    for n, line in enumerate(raw.splitlines(), start=1):
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            event = load_record("event", data)
        except ValueError as exc:
            raise IntegrityError(f"journal {run_id} line {n} is invalid") from exc
        assert isinstance(event, Event)
        expected = out[-1].record_digest() if out else None
        if event.run_id != run_id or event.seq != n or event.prev_digest != expected:
            raise IntegrityError(f"journal {run_id} chain broken at line {n}")
        if canonical_bytes(event.to_json()) != line:
            raise IntegrityError(f"journal {run_id} line {n} is not canonical")
        out.append(event)
    return out


def _parse_records(files: dict[str, bytes]) -> dict[tuple[str, str], Record]:
    records: dict[tuple[str, str], Record] = {}
    for path in sorted(p for p in files if p.startswith("records/")):
        kind = path.split("/")[1]
        try:
            data = json.loads(files[path])
            if not isinstance(data, dict):
                raise ValueError("not an object")
            rec = load_record(kind, data)
        except ValueError as exc:
            raise ImportRejected(f"{path} is not a valid {kind} record") from exc
        record_id = rec.record_id()
        if _record_file(kind, record_id) != path:
            raise IntegrityError(f"{path} does not hold record {record_ref(kind, record_id)}")
        if canonical_bytes(rec.to_json()) != files[path]:
            raise IntegrityError(f"{path} is not canonical")
        records[(kind, record_id)] = rec
    return records


def _parse_omissions(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    raw = files.get("provenance/omissions.json")
    if raw is None:
        return {}
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        raise ImportRejected("provenance/omissions.json is not valid JSON") from exc
    if not isinstance(entries, list):
        raise ImportRejected("provenance/omissions.json is not a list")
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("digest"), str):
            raise ImportRejected("malformed omission entry")
        out[e["digest"]] = e
    return out


def _write_journal(store: Store, run_id: str, raw: bytes) -> None:
    # A bundle may carry only an ancestor's prefix (visibility cut); a longer local
    # journal already contains it. Conflicts were rejected before any write.
    path = store._journal_path(run_id)
    if path.exists() and path.read_bytes().startswith(raw):
        return
    tmp = store.root / "tmp" / f"{run_id}.jsonl.import.tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def import_bundle(
    bundle: str | os.PathLike[str],
    store: Store,
    *,
    max_files: int = 100_000,
    max_bytes: int = 1 << 30,
    max_expansion_ratio: int = 100,
) -> ImportReport:
    """Validate a bundle completely, then copy its data into `store`. Data-only."""
    bundle = Path(bundle)
    if bundle.is_dir():
        files = _read_dir(bundle, max_files, max_bytes)
    else:
        files = _read_archive(bundle, max_files, max_bytes, max_expansion_ratio)
    manifest = _parse_manifest(files)
    _check_inventory(files, manifest)
    records = _parse_records(files)
    objects = {
        "sha256:" + p[len("objects/sha256/") :].replace("/", "")
        for p in files
        if p.startswith("objects/")
    }
    journals: dict[str, list[Event]] = {}
    for p in files:
        if p.startswith("events/"):
            run_id = p[len("events/") : -len(".jsonl")]
            journals[run_id] = _parse_journal(run_id, files[p])
    omissions = _parse_omissions(files)

    for digest in sorted(objects):
        desc = records.get(("artifact", digest))
        if not isinstance(desc, ArtifactDescriptor):
            raise ImportRejected(f"object {digest} has no artifact descriptor")
        if desc.size != len(files[_object_file(digest)]) or desc.labels != sorted(set(desc.labels)):
            raise IntegrityError(f"descriptor for {digest} does not match its bytes")
    view = _BundleView(records, objects, journals)
    evaluations = [record_ref("evaluation", rid) for (k, rid) in records if k == "evaluation"]
    closure = _closure(view, manifest["roots"], evaluations)
    undeclared = sorted(closure.missing - omissions.keys())
    if undeclared:
        detail = "; ".join(f"{r} (affects: {', '.join(closure.affected(r))})" for r in undeclared)
        raise ImportRejected(
            f"bundle closure incomplete, not present and not declared omitted: {detail}"
        )
    # Every journal written must be one the closure vouched for, and every event in
    # it must reference committed data: import cannot bypass commit-before-visible.
    for run_id, events in journals.items():
        if run_id not in manifest["roots"] and run_id not in closure.cuts:
            raise ImportRejected(f"journal {run_id} is outside the bundle's declared closure")
        cut = closure.cuts.get(run_id)
        if run_id not in manifest["roots"] and cut is not None and len(events) > cut:
            raise ImportRejected(f"ancestor journal {run_id} extends past its cut {cut}")
        for e in events:
            for ref in e.payload_refs.values():
                present = ref in objects or _ref_key(ref) in records or store.ref_exists(ref)
                if not present and ref not in omissions:
                    raise ImportRejected(f"journal {run_id} event {e.seq} references missing {ref}")
    for (kind, _), rec in sorted(records.items()):
        if isinstance(rec, ArtifactDescriptor) and rec.visibility not in store.allowed_visibility:
            raise VisibilityError(f"this store does not accept {rec.visibility!r} artifact")
        if kind == "evaluation" and "evaluator" not in store.allowed_visibility:
            raise VisibilityError("evaluation records need an evaluator-authorized store")
    for run_id in journals:
        existing = store._journal_path(run_id)
        if existing.exists():
            have, raw = existing.read_bytes(), files[f"events/{run_id}.jsonl"]
            # A bundle may carry a prefix of local history but never extend it:
            # appending to an existing run would let untrusted input write its future.
            prefix_cut = _imported_prefixes(store).get(run_id)
            completes_prefix = (
                run_id in manifest["roots"]
                and prefix_cut == have.count(b"\n")
                and raw.startswith(have)
            )
            if not (have.startswith(raw) or completes_prefix):
                raise ImportRejected(f"journal {run_id} would change or extend the local journal")

    for digest in sorted(objects):
        desc = records[("artifact", digest)]
        assert isinstance(desc, ArtifactDescriptor)
        got = store.put_bytes(
            files[_object_file(digest)],
            media_type=desc.media_type,
            visibility=desc.visibility,
            encoding=desc.encoding,
            units=desc.units,
            source=desc.source,
            license=desc.license,
            labels=desc.labels,
        )
        if got != desc:
            raise IntegrityError(f"descriptor for {digest} conflicts with the store")
    for (kind, record_id), rec in sorted(records.items()):
        if not (kind == "artifact" and record_id in objects):
            store.put_record(rec)
    for run_id in sorted(journals):
        _write_journal(store, run_id, files[f"events/{run_id}.jsonl"])
        store.events(run_id)  # re-verify the chain through the store's own reader

    bundle_digest = sha256_digest(files["manifest.json"])
    omission_list = [omissions[k] for k in sorted(omissions)]
    exact = manifest.get("exact_replay") is True and not any(
        o.get("reason") == "redacted" for o in omission_list
    )
    ancestors = {r: c for r, c in sorted(closure.cuts.items()) if c is not None}
    meta = {
        "bundle": bundle_digest,
        "format": BUNDLE_FORMAT,
        "profile": manifest["profile"],
        "roots": manifest["roots"],
        "ancestors": ancestors,
        "exact_replay": exact,
        "labels": manifest.get("labels", []),
        "omissions": omission_list,
    }
    imports = store.root / "imports"
    imports.mkdir(exist_ok=True)
    tmp = store.root / "tmp" / f"import.{bundle_digest[7:]}.tmp"
    tmp.write_bytes(_json_file(meta))
    os.replace(tmp, imports / f"{bundle_digest[7:]}.json")

    return ImportReport(
        bundle_digest=bundle_digest,
        profile=manifest["profile"],
        roots=list(manifest["roots"]),
        ancestors=ancestors,
        objects=len(objects),
        records=len(records),
        events={r: len(e) for r, e in sorted(journals.items())},
        omissions=omission_list,
        exact_replay=exact,
        capabilities={r: inspect_capabilities(store, r) for r in manifest["roots"]},
    )
