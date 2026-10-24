"""Broker fault injection end to end on the real pinned Pi (broker-fault-injection).

Offline under the scripted provider, with the labelled stand-in "ORCA" of
`test_pi_vte_end_to_end` (a marker line, then the authentic ketene SC-NEVPT2
output, whatever the input). Every observation is synthetic: this checks the
record and the leak routes, not any chemistry.
"""

from __future__ import annotations

import re

import pytest
from conftest import requires_pi
from test_orca_faults import CASSCF_ONLY, PLAN_YAML, TARGET
from test_pi_vte_end_to_end import (
    FIXTURE,
    STAND_IN_MARKER,
    case,
    jobs,
    run,
    runs,
    short,  # noqa: F401  (fixture)
    stand_in,
    state,
    stores,
    tool,
)

from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi import vte as pi_vte
from onoda.adapters.pi.contract import Script
from onoda.adapters.pi.fork import fork_point
from onoda.canonical import sha256_digest
from onoda.errors import ForkError
from onoda.science.orca_broker import read_journal
from onoda.science.orca_faults import cut_before_nevpt2, load_fault_plan

pytestmark = requires_pi
# Words of the record that name the fault ("default" is ordinary ORCA text, so not "fault").
TELLS = re.compile(rb"inject|\bfault|killed-before|first-eligible", re.IGNORECASE)


def scripted(*turns: list[dict]) -> Script:
    return Script.model_validate({"source": "test_fault", "turns": [{"content": t} for t in turns]})


def faulted_script() -> Script:
    """Submit the eligible target, reread it and claim from it: nothing is left to cite."""
    return scripted(
        [tool("x1", "orca_submit", input=TARGET)],
        [tool("o1", "orca_output", job_id="j0001", find="NEVPT2")],
        [tool("r1", "report_vtes", states=[state("j0001")], notes="synthetic scripted claim")],
        [{"type": "text", "text": "reported"}],
    )


def files_under(root) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_a_fired_fault_is_recorded_hidden_from_the_agent_and_never_credited(short):  # noqa: F811
    store, evaluator = stores(short)
    binary = stand_in(short)
    pi = run(store, short, "trial", binary, faulted_script(), fault_plan=PLAN_YAML)
    assert pi.ingest.complete, pi.stderr_tail
    assert runs(binary) == 1

    plan, canonical = load_fault_plan(PLAN_YAML)
    plan_sha = sha256_digest(canonical)
    record = store.run_record("trial")
    cfg = record.config
    assert cfg["fault_plan"] == {"id": "kill-nevpt2", "fault": plan.fault, "sha256": plan_sha}
    assert store.get_bytes(plan_sha) == canonical
    assert "fault-injected" in record.labels and "synthetic" in record.labels  # scripted stays so
    real = STAND_IN_MARKER.encode() + b"\n" + FIXTURE.read_bytes()
    assert cfg["fault_injection"] == {
        "outcome": "fired",
        "job_id": "j0001",
        "real_output": sha256_digest(real),
    }
    assert store.get_bytes(cfg["fault_injection"]["real_output"]) == real

    job = jobs(store, "trial")["j0001"]
    served = store.get_bytes(job.outputs["log"])
    assert served == cut_before_nevpt2(real)
    assert job.status == "failed" and job.metadata["executed_here"] is True
    assert "fault-injected" in job.labels
    assert job.metadata["injected"]["outcome"] == "fired"
    assert job.metadata["injected"]["plan_sha256"] == plan_sha
    assert job.metadata["real_output"] == sha256_digest(real)
    assert job.metadata["real_status"] == "completed"
    assert set(job.outputs) == {"log"}  # the real output is not a visible output

    # Leaks: nothing the agent could reach holds the plan, the real output or the word.
    reachable = files_under(short / "trial")
    for path, data in reachable.items():
        if "/broker/" in path:
            continue  # the host's own journal and real-output copy, outside the Pi work dir
        assert b"< NEVPT2  >" not in data, path
        assert b"kill-nevpt2" not in data and plan_sha.encode() not in data, path
        assert plan_sha.removeprefix("sha256:").encode() not in data, path
        assert not TELLS.search(data), path
        # The timing tell: the wall limit and a job's duration stay host-side.
        assert b"max_wall" not in data and b"duration_ms" not in data, path
    assert any("/pi/sessions/" in p for p in reachable)  # resumed context was checked too

    # The claim cites a job that printed no NEVPT2 state: never credited.
    ev = pi_vte.evaluate_vte_run(store, evaluator, "trial", case())
    assert ev.metrics["coverage"] == "0" and ev.assignments["claim.01"] != "supported"

    report = pi_vte.vte_report(store, evaluator, ["trial"])
    arm = f"S1+kill-nevpt2@{plan_sha.removeprefix('sha256:')[:12]}"
    assert list(report["arms"]) == [arm]
    fault = report["arms"][arm]["fault"]
    assert plan_sha in fault["note"] and fault["not_fired"] == []
    assert (fault["jobs_after_fault"], fault["requests_after_fault"]) == (0, 3)

    # Replay serves the served bytes and keeps the injected record; nothing runs.
    out = pi_run.replay_run_pi(store, "trial", case(), work_dir=short / "replay")
    assert (out["status"], out["executions"]) == ("match", 0)
    replayed = [e for e in read_journal(short / "replay" / "broker" / "broker.jsonl") if e.job_id]
    assert replayed[0].injected is not None and replayed[0].injected.outcome == "fired"
    assert runs(binary) == 1

    with pytest.raises(ForkError, match="fault plan"):
        fork_point(store, "trial", "any")


def test_a_plan_that_never_triggers_is_reported_apart(short):  # noqa: F811
    store, evaluator = stores(short)
    binary = stand_in(short)
    s = scripted(
        [tool("x1", "orca_submit", input=CASSCF_ONLY)],
        [tool("r1", "report_vtes", states=[state("j0001")])],
        [{"type": "text", "text": "reported"}],
    )
    pi = run(store, short, "quiet", binary, s, fault_plan=PLAN_YAML)
    assert pi.ingest.complete, pi.stderr_tail
    record = store.run_record("quiet")
    assert record.config["fault_injection"] == {"outcome": "not-triggered"}
    assert "fault-injected" not in record.labels
    assert "injected" not in jobs(store, "quiet")["j0001"].metadata
    pi_vte.evaluate_vte_run(store, evaluator, "quiet", case())
    (arm,) = pi_vte.vte_report(store, evaluator, ["quiet"])["arms"]
    assert arm.startswith("S1+kill-nevpt2@") and arm.endswith(":not-fired")
