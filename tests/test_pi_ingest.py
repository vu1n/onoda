"""Pi spool ingest (M3-004): hand-written contract spools, no Pi process."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from pathlib import Path

import pytest

from onoda.adapters.pi.contract import (
    CTX_COMPACTIONS,
    CTX_MECHANISMS,
    CTX_OBSERVATION_PACK,
    PI_CAPABILITIES,
    PI_VERSION,
    SCRIPTED_MODEL,
    SCRIPTED_PROVIDER,
    SCRIPTED_REDUCER_MODEL,
    SOL_PI_COMMIT,
    SPOOL_SCHEMA,
    VARIANTS,
    Claim,
)
from onoda.adapters.pi.ingest import ingest_spool, recorded_script, run_claim
from onoda.canonical import sha256_digest
from onoda.clock import LogicalClock
from onoda.context import activation_counts
from onoda.errors import CapabilityError, IntegrityError, OnodaError
from onoda.records import RECORD_TYPES, TaskManifest
from onoda.replay import replay_run
from onoda.store import Store

CASE_ID = "demo-case"
INPUT = b"! synthetic ORCA input fixture\n* xyz 0 1\nH 0 0 0\n*\n"
OUTPUT = b"".join(b"SYNTHETIC ORCA OUTPUT FIXTURE line %05d\n" % i for i in range(600))
BIG_BASH = "".join(f"synthetic bash output line {i:05d}\n" for i in range(700))
CLAIM = {
    "schema": "onoda.pi-claim/1",
    "case_id": CASE_ID,
    "final_energy": {"text": "-76.0", "unit": "Eh"},
}


def _hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SpoolBuilder:
    """Writes objects/ + events.jsonl following the spool contract."""

    def __init__(self, root: Path, *, variant: str = "S3", session_id: str = "sess-1"):
        self.dir = root / "spool"
        (self.dir / "objects").mkdir(parents=True)
        self.session_dir = root / "sessions"
        self.session_id = session_id
        self.variant = variant
        self.lines: list[dict] = []
        self.request_index = 0
        self.replay_keys: list[str] = []
        self.reducer_keys: list[str] = []

    def put(self, value) -> str:
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        hexdigest = _hex(data)
        (self.dir / "objects" / hexdigest).write_bytes(data)
        return hexdigest

    def line(self, type: str, **fields) -> None:
        self.lines.append({"seq": len(self.lines), "type": type, **fields})

    def start(self) -> SpoolBuilder:
        self.line(
            "spool.start",
            schema=SPOOL_SCHEMA,
            pi_version=PI_VERSION,
            sol_pi_commit=SOL_PI_COMMIT,
            variant=self.variant,
            mechanisms=list(VARIANTS[self.variant].mechanisms),
            provider=SCRIPTED_PROVIDER,
            model=SCRIPTED_MODEL,
            scripted=True,
            session_id=self.session_id,
            session_dir=str(self.session_dir),
        )
        return self

    def turn(self, *blocks: dict, model: str = SCRIPTED_MODEL) -> int:
        """One provider request and its response; returns the request index."""
        index = self.request_index
        self.request_index += 1
        context = {"messages": [{"role": "user", "content": f"request {index}"}], "model": model}
        key = _hex(json.dumps(context, sort_keys=True).encode())
        (self.reducer_keys if model == SCRIPTED_REDUCER_MODEL else self.replay_keys).append(key)
        self.line(
            "provider.request",
            request_index=index,
            model=model,
            context=self.put(context),
            source="scripted-provider",
            replay_key=key,
        )
        message = {"role": "assistant", "content": list(blocks), "stopReason": "toolUse"}
        self.line(
            "provider.response",
            request_index=index,
            message=self.put(message),
            stop_reason="toolUse",
            usage_tokens={"input": 10, "output": 5, "totalTokens": 15},
            cost_usd="0",
        )
        return index

    def call(self, call_id: str, tool: str, args: dict, *, decision="allowed", reason=None):
        index = self.turn({"type": "toolCall", "id": call_id, "name": tool, "arguments": args})
        self.line(
            "tool.call",
            tool_call_id=call_id,
            tool_name=tool,
            input=self.put(args),
            request_index=index,
            decision=decision,
            reason=reason,
        )

    def result(self, call_id: str, tool: str, text: str, *, raw: str | None = None, error=False):
        for stage, body in (("raw", raw if raw is not None else text), ("visible", text)):
            self.line(
                "tool.result",
                tool_call_id=call_id,
                tool_name=tool,
                stage=stage,
                content=self.put([{"type": "text", "text": body}]),
                is_error=error,
            )

    def archive(self, call_id: str, tool: str, text: str) -> str:
        """Archive a result the way SoL-Pi's ObservationPack does; returns the obs id."""
        content_hash = _hex(text.encode())
        obs_id = "obs_" + _hex(f"{tool}\0{call_id}\0{content_hash}".encode())[:24]
        root = self.session_dir / "sol-pi" / self.session_id / "observation-pack"
        (root / "objects").mkdir(parents=True, exist_ok=True)
        (root / "objects" / f"{obs_id}.txt").write_text(text)
        entries = [
            {"event": "full", "id": obs_id, "request": 3, "contentHash": content_hash},
            {"event": "placeholder", "id": obs_id, "request": 5},
        ]
        with open(root / "ledger.jsonl", "a") as fh:
            for entry in entries:
                fh.write(json.dumps({"timestamp": "2026-01-01T00:00:00Z", **entry}) + "\n")
        return obs_id

    def end(self, status: str = "settled") -> None:
        self.line("spool.end", status=status, requests=self.request_index)

    def write(self, key: bytes | None = None) -> Path:
        """Write events.jsonl; with `key`, chain every line's mac as spool.ts does."""
        out, prev = [], ""
        for x in self.lines:
            body = json.dumps(x, separators=(",", ":"))
            if key is not None:
                prev = hmac.new(key, (prev + body).encode(), hashlib.sha256).hexdigest()
                body = body[:-1] + f',"mac":"{prev}"}}'
            out.append(body + "\n")
        (self.dir / "events.jsonl").write_text("".join(out))
        return self.dir


def full_run(root: Path, *, end: bool = True) -> tuple[SpoolBuilder, str]:
    """An S3 run exercising every mechanism; returns the builder and the obs id."""
    sb = SpoolBuilder(root).start()
    sb.call("c1", "case_input", {})
    sb.result("c1", "case_input", INPUT.decode())
    sb.call("c2", "orca_run", {})
    sb.line("task.acquire", tool_call_id="c2", case_id=CASE_ID, output=sb.put(OUTPUT))
    sb.turn({"type": "text", "text": "reducer receipt"}, model=SCRIPTED_REDUCER_MODEL)
    sb.result("c2", "orca_run", "sol_pi_evidence_receipt_v1\nreduced", raw=OUTPUT.decode())
    sb.call("c3", "bash", {"command": "cat big.txt"})
    sb.result("c3", "bash", BIG_BASH)
    obs_id = sb.archive("c3", "bash", BIG_BASH)
    sb.call("c4", "obs_recall", {"id": obs_id, "offset": 0})
    sb.result("c4", "obs_recall", f"[obs_recall id={obs_id} offset=0]\n...")
    then_run = {"command": "python -m py_compile check.py", "timeout": 30}
    sb.call("c5", "write", {"path": "check.py", "content": "pass\n", "then_run": then_run})
    sb.result("c5", "write", "wrote check.py\n[then_run:succeeded]")
    sb.call(
        "c6",
        "bash",
        {"command": "sbatch job.sh"},
        decision="blocked",
        reason="tool not allowed",
    )
    sb.result("c6", "bash", "blocked: tool not allowed", error=True)
    sb.line("compaction", phase="before")
    details = {"summary": "synthetic compaction summary", "tokensBefore": 1234}
    sb.line("compaction", phase="done", details=sb.put(details))
    sb.call("c7", "report_states", CLAIM)
    sb.line("task.claim", tool_call_id="c7", claim=sb.put(CLAIM))
    sb.result("c7", "report_states", "claim recorded")
    sb.turn({"type": "text", "text": "done"})
    if end:
        sb.end()
    sb.write()
    return sb, obs_id


def make_store(root: Path, **kwargs) -> tuple[Store, TaskManifest]:
    store = Store(root, clock=LogicalClock(), **kwargs)
    inp = store.put_bytes(INPUT, media_type="text/plain", labels=["task-input", "synthetic"])
    out = store.put_bytes(OUTPUT, media_type="text/plain", labels=["job-output", "synthetic"])
    task = TaskManifest.new(
        task_id="pi-demo-case",
        inputs={"input": inp.digest, "output": out.digest},
        permitted_context=["task-input"],
        split="dev",
        target={"kind": "pi-case", "case_id": CASE_ID},
        synthetic=True,
    )
    return store, task


CONFIG = {"mode": "pi-scripted", "variant": "S3"}


def ingest(store: Store, task: TaskManifest, spool: Path, run_id="pi-run", **kwargs):
    return ingest_spool(
        store, spool, run_id=run_id, task=task, config=kwargs.pop("config", CONFIG), **kwargs
    )


@pytest.fixture
def ingested(tmp_path):
    sb, obs_id = full_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    return store, sb, obs_id, result


# ---------------------------------------------------------------- full run


def test_records_validate_and_refs_are_committed(ingested):
    store, _, _, result = ingested
    for kind in RECORD_TYPES:
        for record_id in store.list_record_ids(kind):
            store.get_record(kind, record_id)
    events = store.events("pi-run")
    assert events and result.complete and result.status == "settled"
    for event in events:
        for ref in event.payload_refs.values():
            assert store.ref_exists(ref), (event.type, ref)
    for inv_id in result.invocations:
        receipt = store.receipt(inv_id)
        for digest in (*receipt.inputs.values(), *receipt.outputs.values()):
            assert store.has_object(digest) and store.has_record("artifact", digest)


def test_every_ref_is_committed_before_its_event(tmp_path):
    sb, _ = full_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    committed_at_append: list[tuple[str, bool]] = []
    original = store.append_event

    def checked(run_id, **kwargs):
        for ref in (kwargs.get("payload_refs") or {}).values():
            committed_at_append.append((ref, store.ref_exists(ref)))
        return original(run_id, **kwargs)

    store.append_event = checked
    ingest(store, task, sb.dir)
    assert committed_at_append and all(ok for _, ok in committed_at_append)


def test_replay_run_verifies_the_ingested_run(ingested):
    store, _, _, result = ingested
    report = replay_run(store, "pi-run")
    assert report["external_calls"] == 0
    assert [c["boundary"] for c in report["checkpoints"]] == [
        "initialized",
        "job-collected",
        "finalized",
    ]
    assert report["invocations"] == list(result.invocations)
    assert report["final"]["final"] == CLAIM
    finalized = store.checkpoint(result.checkpoints[-1])
    assert finalized.capabilities == PI_CAPABILITIES
    assert finalized.context[CTX_MECHANISMS]["cacheWriteReadRatio"] == "12.5"
    assert [c["phase"] for c in finalized.context[CTX_COMPACTIONS]] == ["before", "done"]


def test_activation_counts_report_each_mechanism(ingested):
    store, *_ = ingested
    counts = activation_counts(store, "pi-run")
    assert counts == {
        "context.observation-archived": 1,
        "context.page": 1,
        "context.reduced": 1,
        "context.fused-step": 2,
        "context.compaction": 1,
    }


def test_fused_call_is_decomposed(ingested):
    store, *_ = ingested
    steps = [e for e in store.events("pi-run") if e.type == "context.fused-step"]
    assert [s.payload["operation"] for s in steps] == ["write_input", "validate_input"]
    assert steps[0].payload["path"] == "check.py"
    assert steps[1].payload["command"] == "python -m py_compile check.py"
    assert steps[1].payload["timeout_s"] == 30
    assert steps[0].payload["fusion_id"] == steps[1].payload["fusion_id"]
    assert all(s.payload["tool_call_id"] == "c5" for s in steps)


def test_observation_pack_handles_map_to_store_digests(ingested):
    store, sb, obs_id, result = ingested
    archived = (
        sb.session_dir / "sol-pi" / sb.session_id / "observation-pack" / "objects"
    ) / f"{obs_id}.txt"
    digest = sha256_digest(archived.read_bytes())
    assert result.observation_pack == {obs_id: digest}
    assert store.checkpoint(result.checkpoints[-1]).context[CTX_OBSERVATION_PACK] == {
        obs_id: digest
    }
    assert store.get_bytes(digest) == BIG_BASH.encode()
    page = next(e for e in store.events("pi-run") if e.type == "context.page")
    assert page.payload["obs_id"] == obs_id and page.payload["handle"] == digest
    archived_event = next(
        e for e in store.events("pi-run") if e.type == "context.observation-archived"
    )
    assert archived_event.payload["tool_call_id"] == "c3"
    assert archived_event.payload["ledger"] == {"full": 1, "placeholder": 1, "recall": 0}


def test_receipts_are_labelled_and_typed(ingested):
    store, _, _, result = ingested
    receipts = [store.receipt(i) for i in result.invocations]
    assert [r.invocation_id for r in receipts] == [
        f"pi-run.i{n:03d}" for n in range(1, len(receipts) + 1)
    ]
    decisions = [r for r in receipts if r.invocation_kind == "decision"]
    assert all("synthetic" in r.labels and r.usage.simulated for r in decisions)
    assert all(set(r.inputs) == {"context"} and set(r.outputs) == {"message"} for r in decisions)
    assert [r.metadata["role"] for r in decisions].count("reducer") == 1
    reducer = next(r for r in decisions if r.metadata["role"] == "reducer")
    assert "reducer" in reducer.labels and reducer.metadata["model"] == SCRIPTED_REDUCER_MODEL

    acquired = next(r for r in receipts if r.metadata.get("executed_here") is False)
    assert acquired.invocation_kind == "read" and "synthetic" not in acquired.labels
    assert acquired.outputs == {"log": sha256_digest(OUTPUT)}

    tools = {r.metadata["tool_call_id"]: r for r in receipts if "tool_name" in r.metadata}
    assert "c6" not in tools  # blocked: an event, never a receipt
    assert tools["c1"].invocation_kind == "read" and tools["c3"].invocation_kind == "execution"
    assert set(tools["c2"].outputs) == {"result", "raw"}
    blocked = [e for e in store.events("pi-run") if e.type == "pi.tool-blocked"]
    assert [(e.payload["tool_call_id"], e.payload["reason"]) for e in blocked] == [
        ("c6", "tool not allowed")
    ]


def test_run_claim_reads_the_claim(ingested):
    store, *_ = ingested
    assert run_claim(store, "pi-run") == Claim.model_validate(CLAIM)


def test_recorded_script_rebuilds_turns_with_replay_keys(ingested):
    store, sb, _, _ = ingested
    script = recorded_script(store, "pi-run")
    assert script.source == "replay:pi-run" and script.synthetic
    assert [t.expect_replay_key for t in script.turns] == sb.replay_keys
    assert [t.expect_replay_key for t in script.reducer_turns] == sb.reducer_keys
    assert script.turns[0].content[0].name == "case_input"
    assert script.turns[-1].content[0].text == "done"
    assert script.reducer_turns[0].content[0].text == "reducer receipt"


# ------------------------------------------------------ incomplete, idempotent


def test_incomplete_spool_has_no_finalized_checkpoint(tmp_path):
    sb, _ = full_run(tmp_path / "w", end=False)
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    assert not result.complete and result.status is None
    boundaries = [cp.boundary for _, _, cp in store.checkpoints("pi-run")]
    assert "finalized" not in boundaries
    replay_run(store, "pi-run")


def test_torn_final_line_is_dropped(tmp_path):
    sb, _ = full_run(tmp_path / "w", end=False)
    with open(sb.dir / "events.jsonl", "a") as fh:
        fh.write('{"seq": 999, "type": "spool.e')
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    assert not result.complete
    assert store.events("pi-run")[-1].payload["torn_tail"] is True


def test_reingest_is_a_noop(ingested):
    store, sb, _, first = ingested
    before = store.events("pi-run")
    task = store.get_record("task", "pi-demo-case")
    again = ingest(store, task, sb.dir)
    assert again.noop and not first.noop
    assert store.events("pi-run") == before
    assert again.invocations == first.invocations and again.checkpoints == first.checkpoints


def test_conflicting_reingest_is_refused(ingested):
    store, sb, _, _ = ingested
    task = store.get_record("task", "pi-demo-case")
    before = store.events("pi-run")
    sb.lines.pop()  # drop spool.end: a different spool
    sb.turn({"type": "text", "text": "something else"})
    sb.end()
    sb.write()
    with pytest.raises(OnodaError, match="different spool"):
        ingest(store, task, sb.dir)
    with pytest.raises(OnodaError, match="different spool"):
        ingest(store, task, sb.dir, config={"mode": "pi-scripted", "variant": "S2a"})
    assert store.events("pi-run") == before


# 9th event is a tool receipt's invocation.committed (orphan receipt); 18th is
# the first half of the fused call; 32nd is the finalized checkpoint.
@pytest.mark.parametrize(
    "point,nth",
    [
        ("event.appended", 12),
        ("event.before-append", 9),
        ("event.appended", 18),
        ("event.before-append", 32),
    ],
)
def test_interrupted_ingest_continues(tmp_path, point, nth):
    sb, _ = full_run(tmp_path / "w")
    clean_store, task = make_store(tmp_path / "clean")
    clean = ingest(clean_store, task, sb.dir)

    seen = 0

    def crash(at: str) -> None:
        nonlocal seen
        if at == point:
            seen += 1
            if seen == nth:
                raise RuntimeError("simulated crash")

    crashing, task = make_store(tmp_path / "crash", crash_hook=crash)
    with pytest.raises(RuntimeError):
        ingest(crashing, task, sb.dir)
    reopened = Store(tmp_path / "crash", clock=LogicalClock("2026-06-01T00:00:00Z"))
    resumed = ingest(reopened, task, sb.dir)
    assert resumed.complete and resumed.invocations == clean.invocations

    def shape(store):
        return [(e.type, e.payload.get("step_key"), e.causes) for e in store.events("pi-run")]

    assert shape(reopened) == shape(clean_store)
    assert resumed.checkpoints == clean.checkpoints
    replay_run(reopened, "pi-run")


# ----------------------------------------------------------- refusals


def _assert_nothing_recorded(store: Store) -> None:
    assert store.run_ids_with_journals() == []
    assert not store.has_record("run", "pi-run")


def test_allowed_fused_submission_is_refused_and_records_nothing(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    then_run = {"command": "orca job.inp > job.out"}
    sb.call("c1", "write", {"path": "job.inp", "content": "! x\n", "then_run": then_run})
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(CapabilityError, match="unfusable"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_blocked_fused_submission_is_a_blocked_event(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    then_run = {"command": "sbatch run.sh"}
    sb.call(
        "c1",
        "edit",
        {"path": "run.sh", "edits": [], "then_run": then_run},
        decision="blocked",
        reason="unfusable then_run",
    )
    sb.result("c1", "edit", "blocked", error=True)
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    ingest(store, task, sb.dir)
    types = [e.type for e in store.events("pi-run")]
    assert "pi.tool-blocked" in types and "context.fused-step" not in types
    assert store.events("pi-run")[types.index("pi.tool-blocked")].payload["fused"] is True


def test_tampered_object_is_refused(tmp_path):
    sb, _ = full_run(tmp_path / "w")
    victim = next((sb.dir / "objects").iterdir())
    victim.write_bytes(victim.read_bytes() + b" ")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_unpinned_acquisition_is_refused(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    sb.call("c1", "orca_run", {})
    sb.line("task.acquire", tool_call_id="c1", case_id=CASE_ID, output=sb.put(b"other bytes"))
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="pinned"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_spool_must_match_run_config(tmp_path):
    sb, _ = full_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(OnodaError, match="variant"):
        ingest(store, task, sb.dir, config={"mode": "pi-scripted", "variant": "S0"})
    with pytest.raises(OnodaError, match="scripted"):
        ingest(store, task, sb.dir, config={"mode": "pi-live", "variant": "S3"})
    with pytest.raises(OnodaError, match="mode"):
        ingest(store, task, sb.dir, config={"mode": "live", "variant": "S3"})
    _assert_nothing_recorded(store)


def test_result_for_unknown_call_is_refused(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    sb.result("ghost", "bash", "hello")
    sb.write()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="unknown tool call"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_absent_session_dir_means_no_observation_events(tmp_path):
    sb, obs_id = full_run(tmp_path / "w")
    shutil.rmtree(sb.session_dir)
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    assert result.observation_pack == {}
    page = next(e for e in store.events("pi-run") if e.type == "context.page")
    assert page.payload["obs_id"] == obs_id and page.payload["handle"] is None
    assert "context.observation-archived" not in activation_counts(store, "pi-run")


def test_unanswered_request_and_missing_result_are_recorded_as_such(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    sb.call("c1", "bash", {"command": "true"})
    sb.line(
        "provider.request",
        request_index=1,
        model=SCRIPTED_MODEL,
        context=sb.put({"messages": []}),
        source="scripted-provider",
        replay_key="0" * 64,
    )
    sb.line("replay.mismatch", request_index=1, expected="f" * 64, actual="0" * 64)
    sb.end("replay-mismatch")
    sb.write()
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    assert result.status == "replay-mismatch"
    receipts = [store.receipt(i) for i in result.invocations]
    unanswered = [r for r in receipts if r.invocation_kind == "decision" and not r.outputs]
    assert [r.status for r in unanswered] == ["failed"]
    tool = next(r for r in receipts if r.metadata.get("tool_call_id") == "c1")
    assert tool.status == "submission_uncertain" and tool.metadata["result"] == "missing"
    assert "pi.replay-mismatch" in [e.type for e in store.events("pi-run")]
    assert len(recorded_script(store, "pi-run").turns) == 1
    replay_run(store, "pi-run")


# ------------------------------------------------------------ review fixes (M3)

KEY = bytes(range(32))


def test_keyed_spool_ingests_when_every_mac_verifies(tmp_path):
    sb, _ = full_run(tmp_path / "w")
    sb.write(key=KEY)
    store, task = make_store(tmp_path / "store")
    assert ingest(store, task, sb.dir, key=KEY).complete


def _rewrite(spool: Path, edit) -> None:
    events = spool / "events.jsonl"
    lines = events.read_bytes().splitlines(keepends=True)
    events.write_bytes(b"".join(edit(lines)))


@pytest.mark.parametrize(
    "tamper",
    [
        # a tool call's input swapped for another object (what an agent's bash could do)
        lambda lines: [
            ln.replace(b'"input":"', b'"input":"' + b"0" * 64 + b'","x":"', 1)
            if b'"tool.call"' in ln
            else ln
            for ln in lines
        ],
        lambda lines: lines[:3] + lines[4:],  # a line dropped from the middle
        lambda lines: [lines[0], lines[2], lines[1], *lines[3:]],  # reordered
        lambda lines: [ln.replace(b',"mac":"', b',"mbc":"') for ln in lines],  # macs stripped
    ],
)
def test_tampered_keyed_spool_is_refused(tmp_path, tamper):
    sb, _ = full_run(tmp_path / "w")
    sb.write(key=KEY)
    _rewrite(sb.dir, tamper)
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="mac"):
        ingest(store, task, sb.dir, key=KEY)
    _assert_nothing_recorded(store)


def test_keyed_ingest_refuses_a_spool_without_macs_or_under_another_key(tmp_path):
    sb, _ = full_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="no mac"):
        ingest(store, task, sb.dir, key=KEY)
    sb.write(key=bytes(32))
    with pytest.raises(IntegrityError, match="mac"):
        ingest(store, task, sb.dir, key=KEY)
    _assert_nothing_recorded(store)


def test_session_dir_outside_the_launch_is_refused(tmp_path):
    sb, _ = full_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="outside"):
        ingest(store, task, sb.dir, expected_session_dir=tmp_path / "elsewhere")
    _assert_nothing_recorded(store)
    assert ingest(store, task, sb.dir, expected_session_dir=sb.session_dir).complete


def test_only_archives_a_tool_result_vouches_for_are_imported(tmp_path):
    sb, obs_id = full_run(tmp_path / "w")
    planted = "obs_" + "f" * 24
    objects = sb.session_dir / "sol-pi" / sb.session_id / "observation-pack" / "objects"
    (objects / f"{planted}.txt").write_text("planted by a tool, cited by no result")
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir)
    assert set(result.observation_pack) == {obs_id}


def test_then_run_outside_action_fusion_is_plain_even_if_malformed(tmp_path):
    sb = SpoolBuilder(tmp_path / "w", variant="S0").start()
    sb.call("c1", "write", {"path": "a.txt", "content": "x\n", "then_run": "orca x"})
    sb.result("c1", "write", "wrote a.txt")
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    ingest(store, task, sb.dir, config={"mode": "pi-scripted", "variant": "S0"})
    events = store.events("pi-run")
    assert "context.fused-step" not in [e.type for e in events]
    (receipt,) = [
        store.receipt(e.payload["invocation_id"])
        for e in events
        if e.type == "invocation.committed"
        and store.receipt(e.payload["invocation_id"]).metadata.get("tool_call_id") == "c1"
    ]
    assert receipt.metadata["fused"] is False and receipt.status == "succeeded"


def test_summary_requests_are_their_own_receipts_and_errors_are_not_turns(tmp_path):
    sb = SpoolBuilder(tmp_path / "w", variant="S0").start()
    sb.call("c1", "case_input", {})
    sb.result("c1", "case_input", INPUT.decode())
    sb.turn({"type": "text", "text": "synthetic summary"})
    sb.lines[-2]["purpose"] = "summary"
    sb.turn({"type": "text", "text": "provider failed"})
    sb.lines[-1]["stop_reason"] = "error"
    sb.end("script-exhausted")
    sb.write()
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir, config={"mode": "pi-scripted", "variant": "S0"})
    decisions = [
        store.receipt(i)
        for i in result.invocations
        if store.receipt(i).invocation_kind == "decision"
    ]
    assert [d.metadata["role"] for d in decisions] == ["agent", "compaction-summary", "agent"]
    assert "compaction-summary" in decisions[1].labels
    script = recorded_script(store, "pi-run")
    assert [t.expect_replay_key for t in script.turns] == [sb.replay_keys[0]]  # error skipped
    assert [t.content[0].text for t in script.summary_turns] == ["synthetic summary"]


def test_live_spool_start_with_cost_cap_and_provider_error_ingests(tmp_path):
    sb = SpoolBuilder(tmp_path / "w", variant="S0").start()
    sb.lines[0].update(provider="anthropic", model="some-model", scripted=False)
    sb.lines[0]["max_cost_usd"] = "0.50"
    sb.turn({"type": "text", "text": ""})
    sb.lines[-2]["source"] = "before_provider_request"
    sb.lines[-1].update(stop_reason="error", cost_usd=None)
    sb.end("provider-error")
    sb.write()
    store, task = make_store(tmp_path / "store")
    result = ingest(store, task, sb.dir, config={"mode": "pi-live", "variant": "S0"})
    assert (result.status, result.complete) == ("provider-error", True)
