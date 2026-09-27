"""M4-006: SoL-Pi gates, the GEPA adapter, and the recorded optimization loop.

Offline and fast: a fake `Runner` whose outcomes follow marker words in the
candidate's text, and a scripted reflection callable. Gate tolerances here
are test fixture values (A43), not operator-chosen ones.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from gepa.core.adapter import EvaluationBatch

from onoda.adapters.pi.contract import SEED_CANDIDATE, HarnessCandidate
from onoda.clock import LogicalClock
from onoda.errors import OnodaError
from onoda.optimize.adapter import (
    OnodaAdapter,
    SearchLedger,
    TraceStep,
    Trial,
    claim_score,
    objective_scores,
)
from onoda.optimize.contract import CandidateRecord, GateSpec, OutcomeVector, SplitAssignment
from onoda.optimize.gates import SolPiGates, gate_decision, non_dominated
from onoda.optimize.loop import GEPA_STATE_FILE, run_optimization
from onoda.store import Store

REDUCER_MARKER = "REDUCER-RECEIPT-7f3a9c"
EVALREF_MARKER = "EVALUATOR-REFERENCE-91c2e4"
SPEC = GateSpec(
    capability_tolerance={"supported_claims": "0", "settled_runs": "0"},
    justification="test fixture tolerances (A43), not operator-chosen",
)
SPLIT = SplitAssignment(seed=7, acceptance=("c1", "c2", "c3"), final=("f1", "f2"), exploratory=True)
SEED_DIGEST = SEED_CANDIDATE.digest()


def vec(
    supported: str = "3",
    settled: str = "1",
    requests: str = "10",
    tokens: str = "1000",
    cost: str = "0.50",
    fs: int = 0,
    candidate: str = "c",
    cases: tuple[str, ...] = ("x",),
) -> OutcomeVector:
    return OutcomeVector(
        candidate=candidate,
        cases=cases,
        runs=tuple(f"run-{c}" for c in cases),
        capability={"supported_claims": supported, "settled_runs": settled},
        efficiency={"requests": requests, "total_tokens": tokens, "cost_usd": cost},
        false_success_claims=fs,
    )


# ------------------------------------------------------------------- gates


def test_gate_rejects_capability_regression_beyond_tolerance():
    spec = GateSpec(
        capability_tolerance={"supported_claims": "1", "settled_runs": "0"},
        justification="fixture",
    )
    ok, reasons = gate_decision(vec(supported="3"), vec(supported="1", requests="5"), spec)
    assert not ok and any("capability regression: supported_claims" in r for r in reasons)
    # Within tolerance, with an efficiency gain: accepted.
    ok, reasons = gate_decision(vec(supported="3"), vec(supported="2", requests="5"), spec)
    assert ok and reasons == ("efficiency gain: requests 10 -> 5",)


def test_gate_rejects_any_false_success_increase():
    ok, reasons = gate_decision(vec(fs=2), vec(fs=3, requests="1", cost="0.01"), SPEC)
    assert not ok and reasons == ("false-success increase: 2 -> 3",)


def test_gate_rejects_no_efficiency_gain():
    ok, reasons = gate_decision(vec(), vec(), SPEC)
    assert not ok and reasons[0].startswith("no efficiency gain")
    # Worse on one efficiency metric, equal elsewhere: still no gain.
    ok, _ = gate_decision(vec(), vec(tokens="1200"), SPEC)
    assert not ok


def test_gate_accepts_otherwise():
    # Better capability, fewer false successes, cheaper on one metric, dearer on another.
    ok, reasons = gate_decision(
        vec(supported="3", fs=1), vec(supported="4", fs=0, cost="0.40", tokens="1100"), SPEC
    )
    assert ok and reasons == ("efficiency gain: cost_usd 0.50 -> 0.40",)
    # Every failed gate is reported, not just the first.
    ok, reasons = gate_decision(vec(), vec(supported="2", fs=1), SPEC)
    assert not ok and len(reasons) == 3


def test_non_dominated_on_hand_built_frontier():
    a = vec(candidate="a", supported="5", cost="0.90")  # most capable, dearest
    b = vec(candidate="b", supported="2", cost="0.10")  # least capable, cheapest
    c = vec(candidate="c", supported="4", cost="0.95")  # dominated by a
    d = vec(candidate="d", supported="5", cost="0.90")  # ties a: both kept
    e = vec(candidate="e", supported="5", cost="0.90", fs=1)  # a with a false success
    f = vec(candidate="f", supported="3", cost="0.50", requests="4")  # fewer requests: kept
    front = non_dominated([a, b, c, d, e, f])
    assert [v.candidate for v in front] == ["a", "b", "d", "f"]


def rollout_like(outcome):
    return SimpleNamespace(outcome=outcome)


def proposal(before, after):
    return SimpleNamespace(
        eval_before=SimpleNamespace(outputs=[rollout_like(v) for v in before]),
        eval_after=SimpleNamespace(outputs=[rollout_like(v) for v in after]),
    )


def test_solpi_gates_criterion_reads_vectors_through_gepa_outputs():
    seen = []
    gates = SolPiGates(SPEC, on_decision=lambda *a: seen.append(a))
    p_before = [vec(candidate="p", cases=("c1",)), vec(candidate="p", cases=("c2",))]
    better = [vec(candidate="k", cases=("c1",), requests="4"), vec(candidate="k", cases=("c2",))]
    good = proposal(p_before, better)
    assert gates.should_accept(good, state=None)
    parent_total, child_total = seen[0][1], seen[0][2]
    assert (
        parent_total.efficiency["requests"] == "20" and child_total.efficiency["requests"] == "14"
    )

    failed = proposal(p_before, [None, better[1]])
    assert not gates.should_accept(failed, state=None)
    assert "trial failed" in gates.reject_reason(failed, state=None)

    elsewhere = proposal(p_before, [vec(candidate="k", cases=("c3",), requests="1")] * 2)
    assert not gates.should_accept(elsewhere, state=None)


# ------------------------------------------------------------ fake runner


class SpyRunner:
    """Outcomes follow marker words in the candidate text:

    EFFICIENT: 4 fewer requests each; REGRESS: 2 fewer supported claims;
    SLOPPY: one false-success claim. Every trial also carries reducer and
    evaluator-reference steps that must never reach reflection or the store.
    """

    def __init__(self, crash_cases: tuple[str, ...] = ()):
        self.calls: list[tuple[str, str, str]] = []
        self.crash_cases = crash_cases

    def run(self, candidate: HarnessCandidate, case_id: str, *, trial_id: str) -> Trial:
        digest = candidate.digest()
        self.calls.append((digest, case_id, trial_id))
        if case_id in self.crash_cases:
            raise RuntimeError(f"solver blew up; reference said {EVALREF_MARKER}")
        text = "\n".join(candidate.components.values())
        requests = max(1, 10 - 4 * text.count("EFFICIENT"))
        supported = 3 - (2 if "REGRESS" in text else 0)
        fs = 1 if "SLOPPY" in text else 0
        verdicts = {f"m1.r{i}": "supported" for i in range(supported)}
        verdicts.update({f"m3.r{i}": "contradicted" for i in range(fs)})
        verdicts.update({f"m5.r{i}": "missing" for i in range(3 - supported - fs)})
        return Trial(
            case_id=case_id,
            run_id=f"run-{trial_id}",
            outcome=vec(
                supported=str(supported),
                requests=str(requests),
                fs=fs,
                candidate=digest,
                cases=(case_id,),
            ),
            steps=(
                TraceStep(kind="request", source="agent", text=f"agent looks at {case_id}"),
                TraceStep(kind="tool_result", source="raw_tool", text="ORCA printed 1.234 eV"),
                TraceStep(kind="tool_result", source="reducer", text=f"digest {REDUCER_MARKER}"),
                TraceStep(kind="claim", source="evaluator_reference", text=EVALREF_MARKER),
            ),
            verdicts=verdicts,
        )


class ScriptedReflection:
    """Appends the next scripted marker line to the component's current text.

    Appending keeps every component a search this short can reach valid
    (prompt texts, and the child prompt keeps its {prompt}); `raw` replies
    are returned verbatim instead.
    """

    def __init__(self, markers: list[str], raw: str | None = None):
        self.markers = list(markers)
        self.raw = raw
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.raw is not None:
            return self.raw
        current = prompt.split("```\n", 1)[1].split("\n```\n", 1)[0]
        marker = self.markers.pop(0) if self.markers else "SLOPPY guess"
        return f"```\n{current}\n{marker}\n```"


def optimize(tmp_path: Path, runner, reflect, **kw):
    args = dict(
        runner=runner,
        seed=SEED_CANDIDATE,
        split=SPLIT,
        gates=SPEC,
        reflect=reflect,
        store=Store(tmp_path / "opt", clock=LogicalClock()),
        search_id="search-1",
        run_dir=tmp_path / "gepa",
        max_trials=40,
        max_reflections=3,
        minibatch_size=2,
    )
    args.update(kw)
    return run_optimization(**args)


def all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def journal(tmp_path: Path, type_: str):
    store = Store(tmp_path / "opt", clock=LogicalClock())
    return store, [e for e in store.events("search-1") if e.type == type_]


# ------------------------------------------------------------------- loop


def test_loop_gates_record_every_candidate_feedback_and_decision(tmp_path):
    runner = SpyRunner()
    reflect = ScriptedReflection(
        ["EFFICIENT: batch the reads.", "REGRESS EFFICIENT", "SLOPPY EFFICIENT EFFICIENT"]
    )
    result = optimize(tmp_path, runner, reflect)

    assert result.reflections == 3 and "reflection budget" in result.stop_reason
    assert result.trials == len(runner.calls) <= 40

    store, cand_events = journal(tmp_path, "optimizer.candidate")
    records = [
        CandidateRecord.model_validate(store.get_json(e.payload_refs["record"]))
        for e in cand_events
    ]
    assert [r.decision for r in records] == ["seed", "accepted", "rejected", "rejected"]
    seed_rec, accepted, regress, sloppy = records
    assert seed_rec.candidate == SEED_DIGEST and seed_rec.origin == "seed"
    assert accepted.parents == (SEED_DIGEST,) and accepted.reasons[0].startswith("efficiency gain")
    assert any("capability regression: supported_claims" in r for r in regress.reasons)
    assert any("false-success increase" in r for r in sloppy.reasons)
    assert result.accepted == (accepted,)

    # Every candidate, feedback packet and decision is a labelled store record.
    _, feedback = journal(tmp_path, "optimizer.feedback")
    _, decisions = journal(tmp_path, "optimizer.decision")
    assert len(feedback) == 3 and len(decisions) == 3
    for e, key, label in (
        [(e, "record", "optimizer-candidate") for e in cand_events]
        + [(e, "packet", "optimizer-feedback") for e in feedback]
        + [(e, "decision", "optimizer-decision") for e in decisions]
    ):
        assert label in store.descriptor(e.payload_refs[key]).labels
    for rec in records[1:]:
        assert rec.feedback in {e.payload_refs["packet"] for e in feedback}
        assert rec.outcomes and all(store.has_object(o) for o in rec.outcomes)
        for o in rec.outcomes:
            assert OutcomeVector.model_validate(store.get_json(o)).candidate == rec.candidate
    for e in decisions:
        d = store.get_json(e.payload_refs["decision"])
        assert d["parent_total"]["cases"] == d["child_total"]["cases"]

    # The accepted child dominates the seed, so the final set is just the child.
    assert [v.candidate for v in result.non_dominated] == [accepted.candidate]
    assert store.get_json(result.final)["non_dominated"] == [accepted.candidate]


def test_runner_never_sees_a_final_split_case(tmp_path):
    runner = SpyRunner()
    optimize(tmp_path, runner, ScriptedReflection(["EFFICIENT"]))
    seen = {case for _, case, _ in runner.calls}
    assert seen and seen <= set(SPLIT.acceptance) and not seen & set(SPLIT.final)

    ledger = SearchLedger(Store(tmp_path / "x"), "s", max_trials=5, max_reflections=1)
    adapter = OnodaAdapter(runner, ledger, cases=SPLIT.acceptance)
    before = len(runner.calls)
    with pytest.raises(OnodaError, match="outside the acceptance slice"):
        adapter.evaluate(["c1", "f1"], dict(SEED_CANDIDATE.components))
    assert len(runner.calls) == before


def test_invalid_proposal_is_recorded_and_never_run(tmp_path):
    runner = SpyRunner()
    reflect = ScriptedReflection([], raw="```\n\n```")  # empty text fails validation
    result = optimize(tmp_path, runner, reflect, max_reflections=2)

    assert {digest for digest, _, _ in runner.calls} == {SEED_DIGEST}
    store, cand_events = journal(tmp_path, "optimizer.candidate")
    records = [
        CandidateRecord.model_validate(store.get_json(e.payload_refs["record"]))
        for e in cand_events
    ]
    invalid = [r for r in records if r.decision == "invalid"]
    assert len(invalid) == 2 and all(r.candidate is None and not r.outcomes for r in invalid)
    assert all(r.feedback and any("empty" in x for x in r.reasons) for r in invalid)
    assert result.accepted == ()
    assert [v.candidate for v in result.non_dominated] == [SEED_DIGEST]


def test_reflective_dataset_and_records_exclude_reducer_and_reference_text(tmp_path):
    runner = SpyRunner()
    reflect = ScriptedReflection(["EFFICIENT", "REGRESS"])
    optimize(tmp_path, runner, reflect)

    assert reflect.prompts
    for prompt in reflect.prompts:
        assert "agent looks at" in prompt and "ORCA printed" in prompt  # agent-visible kept
        assert REDUCER_MARKER not in prompt and EVALREF_MARKER not in prompt
    store, feedback = journal(tmp_path, "optimizer.feedback")
    for e in feedback:
        packet = store.get_bytes(e.payload_refs["packet"]).decode()
        assert "Evaluator verdicts per claim" in packet
        assert REDUCER_MARKER not in packet and EVALREF_MARKER not in packet
    everything = all_bytes(tmp_path / "opt") + all_bytes(tmp_path / "gepa")
    assert REDUCER_MARKER.encode() not in everything
    assert EVALREF_MARKER.encode() not in everything


def test_adapter_drops_untrusted_steps_itself(tmp_path):
    runner = SpyRunner()
    ledger = SearchLedger(Store(tmp_path), "s", max_trials=5, max_reflections=1)
    adapter = OnodaAdapter(runner, ledger, cases=("c1",))
    batch = adapter.evaluate(["c1"], dict(SEED_CANDIDATE.components), capture_traces=True)
    (r,) = batch.trajectories
    assert {s.source for s in r.steps} == {"agent", "raw_tool"}
    assert batch.scores == [1.0]
    assert batch.objective_scores[0]["neg.efficiency.requests"] == -10.0
    # Even a trajectory that smuggles a reducer step is filtered when reflecting.
    smuggled = EvaluationBatch(
        outputs=[r],
        scores=[1.0],
        trajectories=[
            replace(r, steps=(*r.steps, TraceStep(kind="x", source="reducer", text=REDUCER_MARKER)))
        ],
    )
    ds = adapter.make_reflective_dataset({}, smuggled, ["rlm.tool_doc"])
    assert REDUCER_MARKER not in str(ds)


def test_scores_and_objectives_are_exact_until_the_gepa_boundary():
    assert str(claim_score({"a": "supported", "b": "missing", "c": "unresolved"})) == "0.5"
    assert claim_score({"a": "unresolved"}) == 0
    obj = objective_scores(vec(supported="2", fs=1, cost="0.25"))
    assert obj["capability.supported_claims"] == 2.0
    assert obj["neg.false_success_claims"] == -1.0 and obj["neg.efficiency.cost_usd"] == -0.25
    with pytest.raises(ValueError):
        Trial(case_id="c", run_id="r", outcome=vec(), verdicts={"m1.r1": "probably"})


def test_failed_trials_are_charged_and_their_text_not_kept(tmp_path):
    runner = SpyRunner(crash_cases=("c2",))
    result = optimize(tmp_path, runner, ScriptedReflection(["EFFICIENT"]), max_reflections=1)
    store, failed = journal(tmp_path, "optimizer.trial-failed")
    assert failed and all(e.payload["error"] == "RuntimeError" for e in failed)
    assert result.trials == len(runner.calls)
    assert EVALREF_MARKER.encode() not in all_bytes(tmp_path / "opt")
    # A pool member with a failed trial has no total, so the frontier is empty.
    assert result.non_dominated == () and result.final is None


def test_resume_from_run_dir_does_not_reevaluate_the_seed(tmp_path):
    reflect = ScriptedReflection(["EFFICIENT", "REGRESS"])
    runner = SpyRunner()
    # Seed evaluation (3) plus one full iteration (2 + 2 + 3).
    first = optimize(tmp_path, runner, reflect, max_trials=10, max_reflections=5)
    assert "trial budget" in first.stop_reason and first.reflections == 1
    assert (tmp_path / "gepa" / GEPA_STATE_FILE).exists()
    assert all(d == SEED_DIGEST for d, _, _ in runner.calls[:3])

    runner2 = SpyRunner()
    second = optimize(tmp_path, runner2, reflect, max_trials=17, max_reflections=5)
    assert second.reflections == 2 and second.trials > first.trials
    assert runner2.calls, "the resumed search did no work"
    # No full-slice seed evaluation: at most one minibatch of seed runs (as a parent).
    assert sum(d == SEED_DIGEST for d, _, _ in runner2.calls) <= 2
    # Trial ids continue from the journal; nothing is re-numbered.
    ids = [t for _, _, t in runner.calls + runner2.calls]
    assert len(set(ids)) == len(ids) == second.trials

    store, cands = journal(tmp_path, "optimizer.candidate")
    assert sum(e.payload["decision"] == "seed" for e in cands) == 1
    assert len(journal(tmp_path, "optimizer.search")[1]) == 1
    assert len(journal(tmp_path, "optimizer.valset")[1]) == 2  # seed + the accepted child

    other = SplitAssignment(seed=8, acceptance=("c1", "c2"), final=("f1",), exploratory=True)
    with pytest.raises(OnodaError, match="bound to another split"):
        optimize(tmp_path, SpyRunner(), reflect, split=other)
