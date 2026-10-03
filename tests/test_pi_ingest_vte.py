"""Pi spool ingest of VTE runs (VTE-004): `task.execute` against the broker journal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_pi_ingest import SpoolBuilder

from onoda.adapters.pi.ingest import ingest_spool, run_claim, run_vte_claim
from onoda.adapters.pi.vte import _grade_value, _vte_jobs
from onoda.adapters.pi.vte_contract import BrokerReply, ClaimedVte, VteClaim
from onoda.canonical import sha256_digest
from onoda.clock import LogicalClock
from onoda.errors import IntegrityError, OnodaError
from onoda.records import TaskManifest
from onoda.replay import replay_run
from onoda.store import Store

CASE_ID = "questdb-synthetic"
STRUCTURE = b"3\nsynthetic structure fixture\nO 0 0 0\nH 0 0 1\nH 0 1 0\n"
INPUT_A = "! synthetic ORCA input fixture A\n* xyzfile 0 1 structure.xyz\n"
INPUT_B = "! synthetic ORCA input fixture B\n* xyzfile 0 1 structure.xyz\n"
LOG_A = b"SYNTHETIC ORCA OUTPUT FIXTURE A\nNEVPT2 synthetic\n"
LOG_B = b"SYNTHETIC ORCA OUTPUT FIXTURE B\n"
CLAIM = {
    "schema": "onoda.pi-vte-claim/1",
    "case_id": CASE_ID,
    "states": [
        {
            "spin": 1,
            "irrep": "A2",
            "character": "n,pi*",
            "energy": {"text": "4.31", "unit": "eV"},
            "job_id": "j0001",
        }
    ],
}
CONFIG = {"mode": "pi-scripted", "variant": "S3"}


def _hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reply(job_id: str, inp: str, log: bytes, *, cache_hit=False, status="completed", **kw):
    return BrokerReply(
        status=status,
        job_id=job_id,
        cache_hit=cache_hit,
        executed_here=not cache_hit,
        input_sha256=_hex(inp.encode()),
        output_sha256=_hex(log),
        **kw,
    )


def submit(sb: SpoolBuilder, call_id: str, job_id: str, inp: str, log: bytes, *, cache_hit=False):
    sb.call(call_id, "orca_submit", {"input": inp})
    sb.line(
        "task.execute",
        tool_call_id=call_id,
        job_id=job_id,
        status="completed",
        input=sb.put(inp.encode()),
        output=sb.put(log),
        cache_hit=cache_hit,
        executed_here=not cache_hit,
    )
    sb.result(call_id, "orca_submit", log.decode())


def vte_run(root: Path, *, claim: dict | None = None) -> SpoolBuilder:
    """Structure, one executed job, the same input again from the cache, a VTE claim."""
    sb = SpoolBuilder(root).start()
    sb.call("c1", "structure", {})
    sb.result("c1", "structure", STRUCTURE.decode())
    submit(sb, "c2", "j0001", INPUT_A, LOG_A)
    submit(sb, "c3", "j0002", INPUT_A, LOG_A, cache_hit=True)
    claim = CLAIM if claim is None else claim
    sb.call("c4", "report_vtes", claim)
    # Compact JSON, so the claim's bytes differ from the tool input's (whose
    # descriptor, committed first, would otherwise carry the labels).
    claim_bytes = json.dumps(claim, separators=(",", ":")).encode()
    sb.line("task.claim", tool_call_id="c4", claim=sb.put(claim_bytes))
    sb.result("c4", "report_vtes", "claim recorded")
    sb.turn({"type": "text", "text": "done"})
    sb.end()
    sb.write()
    return sb


JOURNAL = (
    BrokerReply(status="refused", reason="synthetic refusal before any job"),
    reply("j0001", INPUT_A, LOG_A),
    reply("j0002", INPUT_A, LOG_A, cache_hit=True),
)


def make_store(root: Path, **kwargs) -> tuple[Store, TaskManifest]:
    store = Store(root, clock=LogicalClock(), **kwargs)
    structure = store.put_bytes(STRUCTURE, media_type="text/plain", labels=["task-input"])
    task = TaskManifest.new(
        task_id="pi-vte-synthetic",
        inputs={"structure": structure.digest},
        permitted_context=["task-input"],
        split="dev",
        target={"kind": "pi-vte", "case_id": CASE_ID},
        synthetic=True,
    )
    return store, task


def ingest(store, task, spool, journal=JOURNAL, **kwargs):
    return ingest_spool(
        store, spool, run_id="pi-run", task=task, config=CONFIG, broker_journal=journal, **kwargs
    )


@pytest.fixture
def ingested(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    return store, ingest(store, task, sb.dir)


def _jobs(store: Store, result) -> dict:
    receipts = (store.receipt(i) for i in result.invocations)
    return {r.metadata["job_id"]: r for r in receipts if "job_id" in r.metadata}


def _assert_nothing_recorded(store: Store) -> None:
    assert store.run_ids_with_journals() == []
    assert not store.has_record("run", "pi-run")


# ---------------------------------------------------------------- ingest


def test_execution_and_cache_hit_become_their_receipts(ingested):
    store, result = ingested
    jobs = _jobs(store, result)
    executed, cached = jobs["j0001"], jobs["j0002"]
    assert executed.invocation_kind == "execution"
    assert executed.metadata["executed_here"] is True and executed.metadata["cache_hit"] is False
    assert "orca-local" in executed.labels and "synthetic" in executed.labels
    assert cached.invocation_kind == "read"
    assert cached.metadata["executed_here"] is False and cached.metadata["cache_hit"] is True
    assert "orca-local" not in cached.labels and {"acquired", "cache-hit"} <= set(cached.labels)
    for receipt in (executed, cached):
        assert receipt.inputs == {"input": sha256_digest(INPUT_A.encode())}
        assert receipt.outputs == {"log": sha256_digest(LOG_A)}
        assert receipt.status == "succeeded" and receipt.metadata["broker_journal"] == "agrees"
    # Identical specs, distinct receipts: the spec digest is not an execution id.
    assert executed.invocation_id != cached.invocation_id
    assert store.get_bytes(executed.outputs["log"]) == LOG_A
    # The orca_submit tool receipt is not a second execution of the job.
    tools = [store.receipt(i) for i in result.invocations]
    submits = [r for r in tools if r.metadata.get("tool_name") == "orca_submit"]
    assert len(submits) == 2 and all(r.invocation_kind == "read" for r in submits)


def test_each_job_is_followed_by_a_job_collected_checkpoint(ingested):
    store, result = ingested
    events = store.events("pi-run")
    committed = {e.payload.get("invocation_id"): e.seq for e in events}
    collected = [e for e in events if e.payload.get("boundary") == "job-collected"]
    jobs = _jobs(store, result)
    assert [e.payload["step_key"] for e in collected] == [
        f"checkpoint:job-collected:{_seq(events, jobs[job])}" for job in ("j0001", "j0002")
    ]
    for receipt, cp_event in zip((jobs["j0001"], jobs["j0002"]), collected, strict=True):
        assert committed[receipt.invocation_id] < cp_event.seq
    finalized = store.checkpoint(result.checkpoints[-1])
    assert finalized.visible["job.j0001"] == sha256_digest(LOG_A)
    assert finalized.visible["job.j0002.input"] == sha256_digest(INPUT_A.encode())
    assert [j["job_id"] for j in finalized.jobs] == ["j0001", "j0002"]
    report = replay_run(store, "pi-run")
    assert report["external_calls"] == 0
    assert [c["boundary"] for c in report["checkpoints"]] == [
        "initialized",
        "job-collected",
        "job-collected",
        "finalized",
    ]


def _seq(events, receipt) -> int:
    """The spool seq of a job's `task.execute` line, from its receipt's step key."""
    event = next(e for e in events if e.payload.get("invocation_id") == receipt.invocation_id)
    return int(event.payload["step_key"].removeprefix("execute:"))


def test_vte_claim_is_a_pi_vte_claim_artifact(ingested):
    store, result = ingested
    assert run_vte_claim(store, "pi-run") == VteClaim.model_validate(CLAIM)
    assert run_claim(store, "pi-run") is None
    assert "pi-vte-claim" in store.descriptor(result.claim).labels
    event = next(e for e in store.events("pi-run") if e.type == "pi.claim")
    assert event.payload["schema"] == "onoda.pi-vte-claim/1"
    assert replay_run(store, "pi-run")["final"]["final"] == CLAIM


def test_every_ref_is_committed_before_its_event(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    seen: list[bool] = []
    original = store.append_event

    def checked(run_id, **kwargs):
        seen.extend(store.ref_exists(r) for r in (kwargs.get("payload_refs") or {}).values())
        return original(run_id, **kwargs)

    store.append_event = checked
    ingest(store, task, sb.dir)
    assert seen and all(seen)


def test_reingest_is_a_noop_and_an_interrupted_ingest_continues(tmp_path):
    sb = vte_run(tmp_path / "w")
    clean, task = make_store(tmp_path / "clean")
    first = ingest(clean, task, sb.dir)
    again = ingest(clean, task, sb.dir)
    assert again.noop and again.invocations == first.invocations

    seen = 0

    def crash(at: str) -> None:
        nonlocal seen
        if at == "event.appended":
            seen += 1
            if seen == 12:
                raise RuntimeError("simulated crash")

    crashing, task = make_store(tmp_path / "crash", crash_hook=crash)
    with pytest.raises(RuntimeError):
        ingest(crashing, task, sb.dir)
    reopened = Store(tmp_path / "crash", clock=LogicalClock("2026-06-01T00:00:00Z"))
    resumed = ingest(reopened, task, sb.dir)
    assert resumed.complete and resumed.invocations == first.invocations
    assert resumed.checkpoints == first.checkpoints


def test_broker_jobs_no_line_cites_are_recorded_as_unspooled(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    journal = (*JOURNAL, reply("j0003", INPUT_B, LOG_B))
    ingest(store, task, sb.dir, journal=journal)
    [event] = [e for e in store.events("pi-run") if e.type == "pi.broker-unspooled"]
    assert event.payload["jobs"] == ["j0003"]


def test_a_journal_with_only_cited_jobs_records_no_unspooled_event(ingested):
    store, _ = ingested
    assert not any(e.type == "pi.broker-unspooled" for e in store.events("pi-run"))


def test_stand_in_jobs_are_labelled_synthetic(tmp_path):
    class StandInReply(BrokerReply):
        stand_in: bool = True

    sb = SpoolBuilder(tmp_path / "w").start()
    submit(sb, "c1", "j0001", INPUT_A, LOG_A)
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    journal = [StandInReply(**reply("j0001", INPUT_A, LOG_A).model_dump())]
    result = ingest(store, task, sb.dir, journal=journal)
    receipt = _jobs(store, result)["j0001"]
    assert receipt.metadata["stand_in"] is True
    assert {"stand-in", "not-orca", "synthetic"} <= set(receipt.labels)
    assert "orca-local" not in receipt.labels
    assert "synthetic" in store.descriptor(receipt.outputs["log"]).labels


def test_failed_job_is_a_failed_execution(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    sb.call("c1", "orca_submit", {"input": INPUT_B})
    sb.line(
        "task.execute",
        tool_call_id="c1",
        job_id="j0001",
        status="failed",
        input=sb.put(INPUT_B.encode()),
        output=sb.put(LOG_B),
        cache_hit=False,
        executed_here=True,
    )
    sb.result("c1", "orca_submit", LOG_B.decode())
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    journal = [reply("j0001", INPUT_B, LOG_B, status="failed")]
    result = ingest(store, task, sb.dir, journal=journal)
    receipt = _jobs(store, result)["j0001"]
    assert receipt.invocation_kind == "execution" and receipt.status == "failed"


def test_a_job_that_never_ran_is_never_an_execution_receipt(tmp_path):
    """`executed_here: false` without a cache hit (the binary failed to launch)."""
    sb = SpoolBuilder(tmp_path / "w").start()
    sb.call("c1", "orca_submit", {"input": INPUT_B})
    sb.line(
        "task.execute",
        tool_call_id="c1",
        job_id="j0001",
        status="failed",
        input=sb.put(INPUT_B.encode()),
        output=sb.put(b""),
        cache_hit=False,
        executed_here=False,
    )
    sb.result("c1", "orca_submit", "")
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    launch_error = reply("j0001", INPUT_B, b"", status="failed").model_copy(
        update={"executed_here": False}
    )
    result = ingest(store, task, sb.dir, journal=[launch_error])
    receipt = _jobs(store, result)["j0001"]
    assert receipt.invocation_kind == "read" and receipt.status == "failed"
    assert receipt.metadata["executed_here"] is False and receipt.metadata["cache_hit"] is False
    assert "orca-local" not in receipt.labels and "cache-hit" not in receipt.labels
    assert {"acquired", "not-executed"} <= set(receipt.labels)
    # Grading still refuses to read a value from it.
    jobs = _vte_jobs(store, "pi-run")
    cs = ClaimedVte.model_validate(CLAIM["states"][0])
    assert _grade_value(cs, jobs, "SC-NEVPT2", True) == (
        "unsupported",
        {"reason": "job_not_executed"},
    )


# ----------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "field,value",
    [
        ("output_sha256", _hex(LOG_B)),
        ("input_sha256", _hex(INPUT_B.encode())),
        ("status", "failed"),
    ],
)
def test_journal_mismatch_is_refused_and_records_nothing(tmp_path, field, value):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    bad = reply("j0001", INPUT_A, LOG_A).model_copy(update={field: value})
    with pytest.raises(IntegrityError, match="disagrees with the broker journal"):
        ingest(store, task, sb.dir, journal=(bad, JOURNAL[2]))
    _assert_nothing_recorded(store)


class CalledReply(BrokerReply):
    """A journal reply that names the tool call it answered, as the broker journals it."""

    tool_call_id: str | None = None


def test_a_journal_naming_another_tool_call_is_refused(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    other = CalledReply(**JOURNAL[1].model_dump(), tool_call_id="c9")
    with pytest.raises(IntegrityError, match=r"disagrees with the broker journal on \['tool_call"):
        ingest(store, task, sb.dir, journal=(other, JOURNAL[2]))
    _assert_nothing_recorded(store)


def test_a_journal_naming_the_same_tool_call_is_accepted(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    same = CalledReply(**JOURNAL[1].model_dump(), tool_call_id="c2")
    assert ingest(store, task, sb.dir, journal=(same, JOURNAL[2])).complete


def test_execution_the_broker_served_from_cache_is_refused(tmp_path):
    """The spool says ORCA ran; the broker says it served a cache hit."""
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    journal = (reply("j0001", INPUT_A, LOG_A, cache_hit=True), JOURNAL[2])
    with pytest.raises(IntegrityError, match=r"\['cache_hit', 'executed_here'\]"):
        ingest(store, task, sb.dir, journal=journal)
    _assert_nothing_recorded(store)


def test_job_the_broker_never_ran_is_refused(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="j0002, which the broker never ran"):
        ingest(store, task, sb.dir, journal=JOURNAL[:2])
    _assert_nothing_recorded(store)


def test_task_execute_without_a_journal_is_refused(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(OnodaError, match="needs the broker journal"):
        ingest(store, task, sb.dir, journal=None)
    _assert_nothing_recorded(store)


def test_a_journal_answering_a_job_twice_is_refused(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="answers job j0001 twice"):
        ingest(store, task, sb.dir, journal=(*JOURNAL, JOURNAL[1]))
    _assert_nothing_recorded(store)


def test_sha256_prefixed_journal_digests_compare_as_hex(tmp_path):
    sb = vte_run(tmp_path / "w")
    store, task = make_store(tmp_path / "store")
    prefixed = [
        r.model_copy(
            update={
                "input_sha256": f"sha256:{r.input_sha256}",
                "output_sha256": f"sha256:{r.output_sha256}",
            }
        )
        for r in JOURNAL[1:]
    ]
    assert ingest(store, task, sb.dir, journal=prefixed).complete


def _single_execute(root: Path, *, tool="orca_submit", job_id="j0001", twice=False) -> Path:
    sb = SpoolBuilder(root).start()
    sb.call("c1", tool, {"input": INPUT_A})
    for n in range(2 if twice else 1):
        sb.line(
            "task.execute",
            tool_call_id="c1",
            job_id=job_id if n == 0 else "j0002",
            status="completed",
            input=sb.put(INPUT_A.encode()),
            output=sb.put(LOG_A),
            cache_hit=False,
            executed_here=True,
        )
    sb.end()
    return sb.write()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"tool": "bash"}, "not from an allowed orca_submit call"),
        ({"job_id": "job-1"}, "not a job id"),
        ({"twice": True}, "second task.execute for c1"),
    ],
)
def test_malformed_task_execute_is_refused(tmp_path, kwargs, match):
    spool = _single_execute(tmp_path / "w", **kwargs)
    store, task = make_store(tmp_path / "store")
    journal = [reply("j0001", INPUT_A, LOG_A), reply("j0002", INPUT_A, LOG_A)]
    with pytest.raises(IntegrityError, match=match):
        ingest(store, task, spool, journal=journal)
    _assert_nothing_recorded(store)


def test_invalid_vte_claim_is_refused(tmp_path):
    bad = {**CLAIM, "states": [{**CLAIM["states"][0], "job_id": "job-1"}]}
    sb = vte_run(tmp_path / "w", claim=bad)
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="not a valid VteClaim"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)


def test_vte_claim_through_report_states_is_refused(tmp_path):
    sb = SpoolBuilder(tmp_path / "w").start()
    sb.call("c1", "report_states", CLAIM)
    sb.line("task.claim", tool_call_id="c1", claim=sb.put(CLAIM))
    sb.end()
    sb.write()
    store, task = make_store(tmp_path / "store")
    with pytest.raises(IntegrityError, match="not a valid Claim"):
        ingest(store, task, sb.dir)
    _assert_nothing_recorded(store)
