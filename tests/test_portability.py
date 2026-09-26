"""Export / import / capability tests (T08, T09, T12, T13 export side)."""

from __future__ import annotations

import gzip
import io
import json
import os
import socket
import tarfile
from pathlib import Path

import pytest

from labrun.canonical import sha256_digest
from labrun.clock import LogicalClock
from labrun.errors import ImportRejected, IntegrityError, SchemaVersionError, VisibilityError
from labrun.portability import export_bundle, import_bundle, inspect_capabilities
from labrun.records import (
    Checkpoint,
    Evaluation,
    ForkDeclaration,
    InvocationReceipt,
    ParentRef,
    Run,
    StepSpec,
    TaskManifest,
)
from labrun.store import Store, record_ref

T0 = "2026-01-01T00:00:00Z"
SECRET = "sk-test-DO-NOT-EXPORT-0123456789"
REFERENCE = b"PROTECTED-REFERENCE-ANSWER energy_hartree=-76.02676"
FUTURE = b"PARENT-FUTURE-DECISION: raise basis to cc-pVQZ"


def _invoke(store, run_id, n, spec, outputs, kind="execution", labels=("fixture",), **extra):
    sid = store.put_record(spec)
    inv = InvocationReceipt.new(
        invocation_id=f"{run_id}.i{n}",
        run_id=run_id,
        spec_digest=sid,
        invocation_kind=kind,
        inputs=dict(spec.inputs),
        outputs=outputs,
        status="succeeded",
        started_at=T0,
        labels=list(labels),
        **extra,
    )
    store.put_record(inv)
    store.append_event(
        run_id,
        actor="controller",
        type="invocation.completed",
        payload_refs={"invocation": record_ref("invocation", inv.invocation_id), **outputs},
    )
    return inv


def build_run(root: Path, run_id: str = "r1") -> dict:
    """Synthetic fixture run: task -> calc -> parse -> checkpoint -> decision (5 events)."""
    store = Store(root, clock=LogicalClock())
    geom = store.put_bytes(
        b"fixture geometry: O 0 0 0; H 0 0 1; H 0 1 0\n",
        media_type="text/plain",
        source={"kind": "synthetic-fixture"},
        license="CC0-1.0",
    )
    store.put_record(
        TaskManifest.new(
            task_id="t1", inputs={"geometry": geom.digest}, split="dev", synthetic=True
        )
    )
    store.put_record(
        Run.new(run_id=run_id, experiment_id="exp1", task="t1", quota_id="q1", created_at=T0)
    )
    store.append_event(
        run_id, actor="controller", type="run.started", payload_refs={"task": "task/t1"}
    )
    log = store.put_bytes(b"FIXTURE LOG energy_hartree=-76.0267\n", media_type="text/plain")
    calc = StepSpec.new(
        operation="fixture.calc",
        inputs={"geometry": geom.digest},
        code_ref="fixture:calc@1",
        determinism="pure",
    )
    i1 = _invoke(store, run_id, 1, calc, {"log": log.digest})
    parsed = store.put_json({"energy_hartree": "-76.0267"})
    parse = StepSpec.new(
        operation="fixture.parse",
        inputs={"log": log.digest},
        code_ref="fixture:parse@1",
        determinism="pure",
    )
    i2 = _invoke(
        store,
        run_id,
        2,
        parse,
        {"parsed": parsed.digest},
        kind="derivation",
        parents=[i1.invocation_id],
    )
    cp = Checkpoint.new(
        run_id=run_id,
        boundary="diagnostics-committed",
        event_seq=3,
        frontier=[i1.invocation_id, i2.invocation_id],
        visible={"parsed": parsed.digest},
        phase="decide",
        quiescent=True,
        capabilities={"semantic_restart": "supported", "native_resume": "unsupported"},
    )
    cp_id = store.put_record(cp)
    store.append_event(
        run_id,
        actor="controller",
        type="checkpoint.committed",
        payload_refs={"checkpoint": record_ref("checkpoint", cp_id)},
    )
    future = store.put_bytes(FUTURE, media_type="text/plain")
    decide = StepSpec.new(
        operation="fixture.decide",
        inputs={"parsed": parsed.digest},
        code_ref="fixture:policy@1",
        determinism="nondeterministic",
    )
    _invoke(
        store,
        run_id,
        3,
        decide,
        {"decision": future.digest},
        kind="decision",
        parents=[i2.invocation_id],
    )
    return {
        "store": store,
        "geom": geom.digest,
        "log": log.digest,
        "parsed": parsed.digest,
        "checkpoint": cp_id,
        "future": future.digest,
    }


def add_fork(run: dict, child: str = "child") -> dict:
    store = run["store"]
    parent = ParentRef(run_id="r1", checkpoint=run["checkpoint"])
    fork = ForkDeclaration.new(
        new_run_id=child,
        parent=parent,
        intervention={"policy": "alt@1"},
        visibility_cut={"r1": 4},
        reason="policy comparison",
    )
    fork_id = store.put_record(fork)
    store.put_record(
        Run.new(
            run_id=child,
            experiment_id="exp1",
            task="t1",
            parent=parent,
            fork=fork_id,
            quota_id="q1",
            created_at=T0,
        )
    )
    store.append_event(
        child,
        actor="controller",
        type="run.forked",
        payload_refs={"fork": record_ref("fork", fork_id)},
    )
    alt = store.put_bytes(b"CHILD-DECISION: keep basis\n", media_type="text/plain")
    decide = StepSpec.new(
        operation="fixture.decide",
        inputs={"parsed": run["parsed"]},
        code_ref="fixture:alt@1",
        determinism="nondeterministic",
    )
    _invoke(store, child, 1, decide, {"decision": alt.digest}, kind="decision")
    return {**run, "fork": fork_id, "alt": alt.digest}


def bundle_bytes(path: Path) -> bytes:
    if path.is_file():
        return path.read_bytes() + gzip.decompress(path.read_bytes())
    return b"".join(p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file())


def rewrite_manifest(bundle: Path, fn) -> None:
    m = json.loads((bundle / "manifest.json").read_bytes())
    fn(m)
    (bundle / "manifest.json").write_text(json.dumps(m))


def make_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    path.write_bytes(gzip.compress(buf.getvalue()))
    return path


def tar_of_dir(bundle: Path, out: Path, extra=()) -> Path:
    members = []
    for p in sorted(bundle.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            info = tarfile.TarInfo(p.relative_to(bundle).as_posix())
            info.size = len(data)
            members.append((info, data))
    return make_tar(out, members + list(extra))


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ T09


@pytest.mark.parametrize("suffix", ["", ".tar.gz"])
def test_round_trip_into_other_absolute_path(tmp_path, suffix, no_network):
    src = build_run(tmp_path / "origin" / "store-a")
    a = src["store"]
    out = tmp_path / "xfer" / f"bundle{suffix}"
    manifest = export_bundle(a, ["r1"], out)
    assert manifest["format"] == "labrun.bundle/1" and manifest["exact_replay"] is True
    assert str(tmp_path).encode() not in bundle_bytes(out)

    b = Store(tmp_path / "elsewhere" / "deep" / "store-b")
    report = import_bundle(out, b)
    assert report.roots == ["r1"] and report.omissions == []
    b.drop_index()
    assert [e.record_digest() for e in b.events("r1")] == [
        e.record_digest() for e in a.events("r1")
    ]
    assert sorted(b.iter_object_digests()) == sorted(a.iter_object_digests())
    for d in b.iter_object_digests():
        assert b.get_bytes(d) == a.get_bytes(d)
    b.rebuild_index()
    caps = inspect_capabilities(b, "r1")
    assert {k: v["status"] for k, v in caps.items()} == {
        "inspect": "supported",
        "reanalyze": "supported",
        "reexecute": "supported",
        "resume": "supported",
        "fork": "supported",
    }
    assert "native session resume unsupported" in caps["resume"]["reason"]
    # Re-importing the same bundle is idempotent.
    import_bundle(out, b)


def test_bundle_layout(tmp_path):
    src = build_run(tmp_path / "s")
    out = tmp_path / "b"
    manifest = export_bundle(src["store"], ["r1"], out)
    for rel in (
        "manifest.json",
        "README.md",
        "schemas/run.schema.json",
        "events/r1.jsonl",
        "environment/environment.json",
        "provenance/sources.json",
        "provenance/licenses.json",
        "provenance/omissions.json",
    ):
        assert (out / rel).is_file(), rel
    paths = {e["path"] for e in manifest["inventory"]}
    assert "manifest.json" not in paths
    assert paths == {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} - {
        "manifest.json"
    }
    for d in (src["geom"], src["log"], src["parsed"], src["future"]):
        hexpart = d.split(":")[1]
        assert (out / "objects" / "sha256" / hexpart[:2] / hexpart[2:]).is_file()
    assert json.loads((out / "provenance/licenses.json").read_text())[src["geom"]] == "CC0-1.0"


def test_archive_bytes_are_deterministic(tmp_path):
    a = build_run(tmp_path / "one" / "store")["store"]
    b = build_run(tmp_path / "two" / "nested" / "store")["store"]
    export_bundle(a, ["r1"], tmp_path / "x1.tar.gz")
    export_bundle(a, ["r1"], tmp_path / "x2.tar.gz")
    export_bundle(b, ["r1"], tmp_path / "x3.tar.gz")
    first = (tmp_path / "x1.tar.gz").read_bytes()
    assert first == (tmp_path / "x2.tar.gz").read_bytes() == (tmp_path / "x3.tar.gz").read_bytes()
    with tarfile.open(tmp_path / "x1.tar.gz") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == sorted(m.name for m in members)
    assert {(m.mtime, m.uid, m.gid, m.mode) for m in members} == {(0, 0, 0, 0o644)}


# ------------------------------------------------------------------ T08


def test_one_byte_corruption_names_digest(tmp_path):
    src = build_run(tmp_path / "s")
    out = tmp_path / "b"
    export_bundle(src["store"], ["r1"], out)
    hexpart = src["log"].split(":")[1]
    obj = out / "objects" / "sha256" / hexpart[:2] / hexpart[2:]
    data = bytearray(obj.read_bytes())
    data[0] ^= 1
    obj.write_bytes(bytes(data))
    with pytest.raises(IntegrityError, match=src["log"]):
        import_bundle(out, Store(tmp_path / "dst"))
    assert list((tmp_path / "dst" / "events").iterdir()) == []


def test_removed_object_reports_digest_and_capabilities(tmp_path):
    src = build_run(tmp_path / "s")
    out = tmp_path / "b"
    export_bundle(src["store"], ["r1"], out)
    hexpart = src["geom"].split(":")[1]
    rel = f"objects/sha256/{hexpart[:2]}/{hexpart[2:]}"
    (out / rel).unlink()
    rewrite_manifest(
        out, lambda m: m.update(inventory=[e for e in m["inventory"] if e["path"] != rel])
    )
    with pytest.raises(ImportRejected) as exc:
        import_bundle(out, Store(tmp_path / "dst"))
    assert src["geom"] in str(exc.value)
    assert "reanalyze" in str(exc.value) and "reexecute" in str(exc.value)


def test_thin_bundle_reports_blocked_and_never_fetches(tmp_path, no_network):
    src = build_run(tmp_path / "s")
    out = tmp_path / "thin.tar.gz"
    manifest = export_bundle(
        src["store"], ["r1"], out, omit={src["geom"]: "restricted geometry; operator-supplied"}
    )
    assert "thin" in manifest["labels"]
    assert manifest["capabilities"]["r1"]["reanalyze"]["status"] == "blocked-by-missing-input"
    dst = Store(tmp_path / "dst")
    report = import_bundle(out, dst)
    (omission,) = report.omissions
    assert omission["digest"] == src["geom"]
    assert omission["affected_capabilities"] == ["reanalyze", "reexecute"]
    assert not dst.has_object(src["geom"])
    caps = inspect_capabilities(dst, "r1")
    assert caps["inspect"]["status"] == "supported"
    for cap in ("reanalyze", "reexecute"):
        assert caps[cap]["status"] == "blocked-by-missing-input"
        assert caps[cap]["missing"] == [src["geom"]]
        assert "restricted geometry" in caps[cap]["reason"]
    assert report.capabilities["r1"] == caps
    # Re-export from the thin store carries the declared omission forward.
    again = export_bundle(dst, ["r1"], tmp_path / "rethin")
    assert again["omissions"] == [src["geom"]]


# ------------------------------------------------------------------ T12


def _info(name: str, size: int = 0, type_=tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.type, info.linkname = size, type_, linkname
    return info


@pytest.mark.parametrize(
    "member",
    [
        (_info("../escape.json", 2), b"{}"),
        (_info("/tmp/abs.json", 2), b"{}"),
        (_info("objects/link", type_=tarfile.SYMTYPE, linkname="../../../etc/passwd"), None),
        (_info("objects/hard", type_=tarfile.LNKTYPE, linkname="README.md"), None),
        (_info("hook.pkl", 4), b"\x80\x04N."),
        (_info("environment/hook.py", 9), b"import os"),
        (_info("dev", type_=tarfile.CHRTYPE), None),
    ],
    ids=["traversal", "absolute", "symlink", "hardlink", "pickle", "py-hook", "device"],
)
def test_unsafe_archive_entries_rejected(tmp_path, member):
    src = build_run(tmp_path / "s")
    export_bundle(src["store"], ["r1"], tmp_path / "b")
    bad = tar_of_dir(tmp_path / "b", tmp_path / "bad.tar.gz", [member])
    dst = Store(tmp_path / "dst")
    with pytest.raises(ImportRejected):
        import_bundle(bad, dst)
    assert list(dst.iter_object_digests()) == []


def test_unsafe_directory_entries_rejected(tmp_path):
    src = build_run(tmp_path / "s")
    export_bundle(src["store"], ["r1"], tmp_path / "b")
    b = tmp_path / "b"
    dst = Store(tmp_path / "dst")

    (b / "records" / "evil.py").write_text("import os; os.system('true')")
    with pytest.raises(ImportRejected, match="not in the manifest inventory"):
        import_bundle(b, dst)
    (b / "records" / "evil.py").unlink()

    os.symlink("/etc/passwd", b / "provenance" / "escape")
    with pytest.raises(ImportRejected, match="symlink"):
        import_bundle(b, dst)
    (b / "provenance" / "escape").unlink()

    os.link(b / "README.md", tmp_path / "outside-hardlink")
    with pytest.raises(ImportRejected, match="hardlink"):
        import_bundle(b, dst)
    (tmp_path / "outside-hardlink").unlink()

    import_bundle(b, dst)  # clean again


def test_unknown_manifest_format_rejected(tmp_path):
    src = build_run(tmp_path / "s")
    b = tmp_path / "b"
    export_bundle(src["store"], ["r1"], b)
    rewrite_manifest(b, lambda m: m.update(format="labrun.bundle/99"))
    with pytest.raises(SchemaVersionError):
        import_bundle(b, Store(tmp_path / "dst"))


def test_unknown_record_schema_rejected(tmp_path):
    src = build_run(tmp_path / "s")
    b = tmp_path / "b"
    export_bundle(src["store"], ["r1"], b)
    rec = b / "records" / "run" / "r1.json"
    data = rec.read_bytes().replace(b"labrun.run/1", b"labrun.run/7")
    rec.write_bytes(data)

    def fix(m):
        for e in m["inventory"]:
            if e["path"] == "records/run/r1.json":
                e["digest"], e["size"] = sha256_digest(data), len(data)

    rewrite_manifest(b, fix)
    with pytest.raises(SchemaVersionError):
        import_bundle(b, Store(tmp_path / "dst"))


def test_size_count_and_expansion_limits(tmp_path):
    src = build_run(tmp_path / "s")
    out = tmp_path / "b.tar.gz"
    export_bundle(src["store"], ["r1"], out)
    dst = Store(tmp_path / "dst")
    with pytest.raises(ImportRejected, match="bytes"):
        import_bundle(out, dst, max_bytes=1000)
    with pytest.raises(ImportRejected, match="files"):
        import_bundle(out, dst, max_files=3)
    bomb = make_tar(
        tmp_path / "bomb.tar.gz",
        [(_info("objects/sha256/00/" + "0" * 62, 50_000_000), bytes(50_000_000))],
    )
    assert bomb.stat().st_size < 200_000
    with pytest.raises(ImportRejected, match="expan"):
        import_bundle(bomb, dst)
    assert list(dst.iter_object_digests()) == []


# ------------------------------------------------------------------ T13


def _evaluator(tmp_path: Path, run: dict) -> Store:
    ev = Store(tmp_path / "evaluator", allowed_visibility=("evaluator",), clock=LogicalClock())
    ref = ev.put_bytes(REFERENCE, media_type="text/plain", visibility="evaluator")
    ev.put_record(
        Evaluation.new(
            evaluation_id="ev1",
            run_id="r1",
            evaluator="fixture-grader",
            evaluator_version="1",
            output_refs={"parsed": run["parsed"]},
            reference_version="ref@1",
            assignments={"reference": ref.digest},
            metrics={"abs_err_hartree": "0.0000"},
            created_at=T0,
        )
    )
    run["reference"] = ref.digest
    return ev


def test_agent_visible_excludes_secret_and_evaluator_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LABRUN_TEST_API_KEY", SECRET)
    src = build_run(tmp_path / "s")
    ev = _evaluator(tmp_path, src)
    for out in (tmp_path / "agent", tmp_path / "agent.tar.gz"):
        export_bundle(src["store"], ["r1"], out, evaluator_store=ev, secrets=[SECRET])
        data = bundle_bytes(out)
        assert SECRET.encode() not in data
        assert REFERENCE not in data
        assert src["reference"].split(":")[1].encode() not in data
        assert b"ev1" not in data and b"fixture-grader" not in data


def test_export_refuses_credentials_in_content(tmp_path):
    src = build_run(tmp_path / "s")
    store = src["store"]
    leak = store.put_bytes(f"Authorization: Bearer {SECRET}\n".encode(), media_type="text/plain")
    store.append_event(
        "r1", actor="controller", type="tool.call", payload_refs={"req": leak.digest}
    )
    for profile in ("agent-visible", "redacted"):
        with pytest.raises(VisibilityError) as exc:
            export_bundle(
                store, ["r1"], tmp_path / f"out-{profile}", profile=profile, secrets=[SECRET]
            )
        assert SECRET not in str(exc.value)
        assert not (tmp_path / f"out-{profile}").exists()


def test_evaluator_profile_includes_reference_not_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("LABRUN_TEST_API_KEY", SECRET)
    src = build_run(tmp_path / "s")
    ev = _evaluator(tmp_path, src)
    out = tmp_path / "eval.tar.gz"
    manifest = export_bundle(
        src["store"], ["r1"], out, profile="evaluator", evaluator_store=ev, secrets=[SECRET]
    )
    data = bundle_bytes(out)
    assert REFERENCE in data and b"ev1" in data
    assert SECRET.encode() not in data
    assert "evaluator-authorized" in manifest["labels"]
    with pytest.raises(VisibilityError):
        import_bundle(out, Store(tmp_path / "agent-dst"))
    both = Store(tmp_path / "authorized", allowed_visibility=("agent", "evaluator"))
    import_bundle(out, both)
    assert both.get_bytes(src["reference"]) == REFERENCE
    assert both.has_record("evaluation", "ev1")


def test_redacted_export_is_derivative(tmp_path):
    src = build_run(tmp_path / "s")
    out = tmp_path / "red"
    replacement = b"[redacted geometry]\n"
    manifest = export_bundle(
        src["store"], ["r1"], out, profile="redacted", redact={src["geom"]: replacement}
    )
    assert manifest["exact_replay"] is False
    assert "redacted-derivative" in manifest["labels"]
    assert "not exact" in manifest["capabilities"]["r1"]["inspect"]["reason"]
    omissions = json.loads((out / "provenance" / "omissions.json").read_text())
    assert [o["digest"] for o in omissions] == [src["geom"]]
    assert omissions[0]["reason"] == "redacted"
    assert omissions[0]["replacement"] == sha256_digest(replacement)
    assert b"fixture geometry" not in bundle_bytes(out)
    with pytest.raises(ValueError):
        export_bundle(src["store"], ["r1"], tmp_path / "x", redact={src["geom"]: b"x"})
    dst = Store(tmp_path / "dst")
    report = import_bundle(out, dst)
    assert report.exact_replay is False
    caps = inspect_capabilities(dst, "r1")
    assert caps["reanalyze"]["status"] == "blocked-by-missing-input"
    assert src["geom"] in caps["reanalyze"]["missing"]


# ------------------------------------------------------------------ forks


def test_fork_export_includes_parent_prefix_only(tmp_path):
    run = add_fork(build_run(tmp_path / "s"))
    store = run["store"]
    out = tmp_path / "child"
    manifest = export_bundle(store, ["child"], out)
    assert manifest["roots"] == ["child"] and manifest["ancestors"] == {"r1": 4}
    assert len((out / "events" / "r1.jsonl").read_bytes().splitlines()) == 4
    data = bundle_bytes(out)
    assert FUTURE not in data and run["future"].split(":")[1].encode() not in data
    assert b"r1.i3" not in data
    dst = Store(tmp_path / "dst")
    import_bundle(out, dst)
    assert len(dst.events("r1")) == 4 and len(dst.events("child")) == 2
    assert inspect_capabilities(dst, "child")["inspect"]["status"] == "supported"

    both = tmp_path / "both"
    manifest = export_bundle(store, ["child", "r1"], both)
    assert manifest["ancestors"] == {}
    assert FUTURE in bundle_bytes(both)
    import_bundle(both, dst)  # extends the prefix journal already imported
    assert len(dst.events("r1")) == 5


# ------------------------------------------------------------ capabilities


def test_capability_statuses_for_live_steps(tmp_path):
    store = Store(tmp_path / "s", clock=LogicalClock())
    store.put_record(TaskManifest.new(task_id="t1", inputs={}, split="dev", synthetic=True))
    store.put_record(
        Run.new(run_id="live", experiment_id="e", task="t1", quota_id="q", created_at=T0)
    )
    inp = store.put_bytes(b"input deck", media_type="text/plain")
    out = store.put_bytes(b"solver output", media_type="text/plain")
    solver = StepSpec.new(
        operation="solver.run",
        inputs={"deck": inp.digest},
        code_ref="orca",
        environment={"solver": "orca@6.0.1"},
        determinism="external",
    )
    _invoke(store, "live", 1, solver, {"out": out.digest}, labels=("live",))
    caps = inspect_capabilities(store, "live")
    assert caps["reexecute"]["status"] == "blocked-by-environment"
    assert "solver=orca@6.0.1" in caps["reexecute"]["reason"]
    assert caps["resume"]["status"] == "blocked-by-missing-input"
    ok = inspect_capabilities(store, "live", available_environment={"solver": "orca@6.0.1"})
    assert ok["reexecute"]["status"] == "supported"

    answer = store.put_bytes(b"model says: next", media_type="text/plain")
    model = StepSpec.new(
        operation="model.decide",
        inputs={"obs": out.digest},
        code_ref="provider-x",
        determinism="nondeterministic",
    )
    _invoke(store, "live", 2, model, {"answer": answer.digest}, kind="decision", labels=())
    caps = inspect_capabilities(store, "live", available_environment={"solver": "orca@6.0.1"})
    assert caps["reexecute"]["status"] == "unavailable-provider-state"
    assert caps["inspect"]["status"] == "supported"
