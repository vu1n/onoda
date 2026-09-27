"""M4-006: SoL-Pi gates, the GEPA adapter, and the recorded optimization loop.

Offline and fast: a fake `Runner` whose outcomes follow marker words in the
candidate's text, and a scripted reflection callable. Outcomes are M3b's
ablation `OutcomeVector`s built by hand with every ablation metric key; gate
tolerances here are test fixture values (A47), not operator-chosen ones.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from gepa.core.adapter import EvaluationBatch

from onoda.ablation.contract import (
    ACTIVATION_METRICS,
    CAPABILITY_METRICS,
    EFFICIENCY_METRICS,
    REDUCER_PROBE_METRICS,
    OutcomeVector,
    Tolerances,
)
from onoda.ablation.gates import PooledArm, pool
from onoda.adapters.pi.contract import SEED_CANDIDATE, HarnessCandidate
from onoda.clock import LogicalClock
from onoda.errors import OnodaError
from onoda.optimize.adapter import (
    OnodaAdapter,
    Rollout,
    SearchLedger,
    TraceStep,
    Trial,
    claim_score,
    objective_scores,
    reflective_record,
)
from onoda.optimize.contract import CandidateRecord, GateSpec, SplitAssignment
from onoda.optimize.gates import SolPiGates, gate_decision, non_dominated
from onoda.optimize.loop import GEPA_STATE_FILE, run_optimization
from onoda.store import Store

REDUCER_MARKER = "REDUCER-RECEIPT-7f3a9c"
EVALREF_MARKER = "EVALUATOR-REFERENCE-91c2e4"
ARM = "S1r"  # the variant every optimizer trial runs under
SPEC = GateSpec(
    tolerances=Tolerances(
        capability={m: "0" for m in CAPABILITY_METRICS},
        justification="test fixture tolerances (A47), not operator-chosen",
    ),
    efficiency_gate=("requests.agent", "tokens.total", "cost_usd"),
)
SPLIT = SplitAssignment(seed=7, acceptance=("c1", "c2", "c3"), final=("f1", "f2"), exploratory=True)
SEED_DIGEST = SEED_CANDIDATE.digest()


def vec(
    supported: int = 3,
    missing: int = 0,
    fs: int = 0,
    requests: str | None = "10",
    tokens: str | None = "1000",
    cost: str | None = "0.50",
    case: str = "x",
    run: str | None = None,
    status: str | None = "settled",
) -> OutcomeVector:
    """One run's ablation vector; `fs` false-success claims are contradicted ones."""
    capability = dict.fromkeys(CAPABILITY_METRICS, "0")
    capability.update(
        {
            "count.supported": str(supported),
            "count.missing": str(missing),
            "count.contradicted": str(fs),
            "false_success_claims": str(fs),
        }
    )
    efficiency: dict[str, str | None] = dict.fromkeys(EFFICIENCY_METRICS, "0")
    efficiency.update({"requests.agent": requests, "tokens.total": tokens, "cost_usd": cost})
    return OutcomeVector(
        run_id=run or f"run-{case}",
        arm=ARM,
        case_id=case,
        model="scripted",
        synthetic=True,
        status=status,
        capability=capability,
        efficiency=efficiency,
        activation=dict.fromkeys(ACTIVATION_METRICS, "0"),
        reducer_probe=dict.fromkeys(REDUCER_PROBE_METRICS, "0"),
        mechanism_config=None,
        evaluation_id=f"eval-{run or case}",
    )


def tot(**kw) -> PooledArm:
    return pool([vec(**kw)])


# ------------------------------------------------------------------- gates


def test_gate_spec_is_unified_on_ablation_tolerances():
    assert SPEC.model_dump(by_alias=True)["schema"] == "onoda.gate-spec/2"
    assert set(SPEC.tolerances.capability) == set(CAPABILITY_METRICS)
    # false_success_claims may be left out: tolerance_for fixes it at 0.
    rest = {m: "0" for m in CAPABILITY_METRICS if m != "false_success_claims"}
    GateSpec(
        tolerances=Tolerances(capability=rest, justification="f"), efficiency_gate=("cost_usd",)
    )


def test_gate_rejects_capability_regression_beyond_tolerance():
    spec = SPEC.model_copy(
        update={
            "tolerances": Tolerances(
                capability={**SPEC.tolerances.capability, "count.supported": "1"},
                justification="fixture",
            )
        }
    )
    ok, reasons = gate_decision(tot(supported=3), tot(supported=1, requests="5"), spec)
    assert not ok and any("capability regression: count.supported" in r for r in reasons)
    # Within tolerance, with an efficiency gain: accepted.
    ok, reasons = gate_decision(tot(supported=3), tot(supported=2, requests="5"), spec)
    assert ok and reasons == ("efficiency gain: requests.agent 10 -> 5",)


def test_gate_rejects_any_false_success_increase():
    ok, reasons = gate_decision(tot(fs=2), tot(fs=3, requests="1", cost="0.01"), SPEC)
    assert not ok and "false-success increase: 2 -> 3" in reasons
    assert any("capability regression: count.contradicted" in r for r in reasons)


def test_gate_rejects_no_efficiency_gain():
    ok, reasons = gate_decision(tot(), tot(), SPEC)
    assert not ok and reasons[0].startswith("no efficiency gain")
    # Worse on one efficiency metric, equal elsewhere: still no gain.
    ok, _ = gate_decision(tot(), tot(tokens="1200"), SPEC)
    assert not ok
    # A metric outside the declared efficiency gate never counts as a gain.
    cheaper_bytes = vec()
    cheaper_bytes = cheaper_bytes.model_copy(
        update={"efficiency": {**cheaper_bytes.efficiency, "context_bytes.total": "-1"}}
    )
    ok, _ = gate_decision(tot(), pool([cheaper_bytes]), SPEC)
    assert not ok


def test_gate_accepts_otherwise():
    # Better capability, fewer false successes, cheaper on one metric, dearer on another.
    ok, reasons = gate_decision(
        tot(supported=3, fs=1), tot(supported=4, fs=0, cost="0.40", tokens="1100"), SPEC
    )
    assert ok and reasons == ("efficiency gain: cost_usd 0.50 -> 0.40",)
    # Every failed gate is reported, not just the first.
    ok, reasons = gate_decision(tot(), tot(supported=2, fs=1), SPEC)
    assert not ok
    assert any(r.startswith("capability regression: count.supported") for r in reasons)
    assert "false-success increase: 0 -> 1" in reasons
    assert any(r.startswith("no efficiency gain") for r in reasons)


def test_capability_gain_counts_only_when_the_spec_allows_it():
    # More claims supported, same cost: SoL-Pi's gates as published reject it.
    parent, child = tot(supported=2, missing=1), tot(supported=3, missing=0)
    ok, reasons = gate_decision(parent, child, SPEC)
    assert not ok and reasons[0].startswith("no efficiency gain")
    gain_spec = SPEC.model_copy(update={"capability_gain": True})
    ok, reasons = gate_decision(parent, child, gain_spec)
    assert ok and reasons == (
        "capability gain: count.supported 2 -> 3",
        "capability gain: count.missing 1 -> 0",
    )
    # A gain never buys a regression elsewhere, and no gain is still no gain.
    ok, reasons = gate_decision(tot(supported=2, missing=1), tot(supported=3, fs=1), gain_spec)
    assert not ok and "false-success increase: 0 -> 1" in reasons
    ok, _ = gate_decision(tot(), tot(), gain_spec)
    assert not ok
    # Unresolved (different cases) is never accepted, however good the sums look.
    ok, _ = gate_decision(parent, tot(supported=3, case="y"), gain_spec)
    assert not ok


def test_gate_does_not_accept_unresolved():
    # An unreported efficiency metric and no other gain: unresolved, not accepted.
    ok, reasons = gate_decision(tot(cost=None), tot(cost=None), SPEC)
    assert not ok and reasons == ("efficiency unresolved: cost_usd None -> None",)
    # An unsettled run makes every check unresolved, however good the sums look.
    ok, reasons = gate_decision(tot(), tot(requests="1", status="budget"), SPEC)
    assert not ok and "unsettled runs: run-x" in reasons
    # Parent and child on different cases: unresolved.
    ok, reasons = gate_decision(tot(), tot(case="y", requests="1"), SPEC)
    assert not ok and reasons[0] == "parent and proposal were not run on the same cases"
    # A capability metric with no declared tolerance is unresolved (the spec forbids
    # one, so drive compare_arm's unresolved path through a spec built unchecked).
    loose = GateSpec.model_construct(
        tolerances=Tolerances(capability={}, justification="none"),
        efficiency_gate=SPEC.efficiency_gate,
    )
    ok, reasons = gate_decision(tot(), tot(requests="1"), loose)
    assert not ok and any(r.startswith("capability unresolved: count.supported") for r in reasons)


def test_non_dominated_on_hand_built_frontier():
    pooled = {
        "a": tot(supported=5, cost="0.90"),  # most capable, dearest
        "b": tot(supported=2, missing=3, cost="0.10"),  # least capable, cheapest
        "c": tot(supported=4, missing=1, cost="0.95"),  # dominated by a
        "d": tot(supported=5, cost="0.90"),  # ties a: both kept
        "e": tot(supported=5, cost="0.90", fs=1),  # a with a false success
        "f": tot(supported=3, missing=2, cost="0.50", requests="4"),  # fewer requests: kept
        "g": tot(supported=9, cost=None),  # unknown cost: kept, and dominates nothing
    }
    assert non_dominated(pooled, SPEC) == ["a", "b", "d", "f", "g"]
    # Unknown never dominates: without g's unknown cost it would beat a, d and f.
    assert non_dominated({"a": pooled["a"], "g": pooled["g"]}, SPEC) == ["a", "g"]


def rollout_like(outcome):
    return SimpleNamespace(outcomes=(outcome,))


def proposal(before, after):
    return SimpleNamespace(
        eval_before=SimpleNamespace(outputs=[rollout_like(v) for v in before]),
        eval_after=SimpleNamespace(outputs=[rollout_like(v) for v in after]),
    )


def test_solpi_gates_criterion_reads_vectors_through_gepa_outputs():
    seen = []
    gates = SolPiGates(SPEC, on_decision=lambda *a: seen.append(a))
    p_before = [vec(case="c1"), vec(case="c2")]
    better = [vec(case="c1", requests="4"), vec(case="c2")]
    good = proposal(p_before, better)
    assert gates.should_accept(good, state=None)
    parent_total, child_total = seen[0][1], seen[0][2]
    assert isinstance(parent_total, PooledArm) and isinstance(child_total, PooledArm)
    assert parent_total.efficiency["requests.agent"] == "20"
    assert child_total.efficiency["requests.agent"] == "14"

    failed = proposal(p_before, [None, better[1]])
    assert not gates.should_accept(failed, state=None)
    assert "trial failed" in gates.reject_reason(failed, state=None)

    elsewhere = proposal(p_before, [vec(case="c3", requests="1")] * 2)
    assert not gates.should_accept(elsewhere, state=None)
    assert "not run on the same cases" in gates.reject_reason(elsewhere, state=None)

    other_arm = vec(case="c2", requests="1").model_copy(update={"arm": "S1"})
    mixed = proposal(p_before, [better[0], other_arm])
    assert not gates.should_accept(mixed, state=None)


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
                supported=supported,
                missing=max(0, 3 - supported - fs),
                requests=str(requests),
                fs=fs,
                case=case_id,
                run=f"run-{trial_id}",
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
    assert any("capability regression: count.supported" in r for r in regress.reasons)
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
            assert "optimizer-outcome" in store.descriptor(o).labels
            v = OutcomeVector.model_validate(store.get_json(o))
            assert v.arm == ARM and v.case_id in SPLIT.acceptance
    for e in decisions:
        d = store.get_json(e.payload_refs["decision"])
        assert sorted(d["parent_total"]["case_ids"]) == sorted(d["child_total"]["case_ids"])
    _, valsets = journal(tmp_path, "optimizer.valset")
    for e in valsets:
        assert "optimizer-valset" in store.descriptor(e.payload_refs["total"]).labels
        total = PooledArm.model_validate(store.get_json(e.payload_refs["total"]))
        assert sorted(total.case_ids) == sorted(SPLIT.acceptance)

    # The accepted child dominates the seed, so the final set is just the child.
    assert list(result.non_dominated) == [accepted.candidate]
    assert store.get_json(result.final)["non_dominated"] == [accepted.candidate]


def test_runner_never_sees_a_final_split_case(tmp_path):
    runner = SpyRunner()
    optimize(tmp_path, runner, ScriptedReflection(["EFFICIENT"]))
    seen = {case for _, case, _ in runner.calls}
    assert seen and seen <= set(SPLIT.acceptance) and not seen & set(SPLIT.final)

    ledger = SearchLedger(Store(tmp_path / "x"), "s", max_trials=5, max_reflections=1)
    adapter = OnodaAdapter(
        runner, ledger, cases=SPLIT.acceptance, efficiency_gate=SPEC.efficiency_gate
    )
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
    assert list(result.non_dominated) == [SEED_DIGEST]


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
    adapter = OnodaAdapter(runner, ledger, cases=("c1",), efficiency_gate=SPEC.efficiency_gate)
    batch = adapter.evaluate(["c1"], dict(SEED_CANDIDATE.components), capture_traces=True)
    (r,) = batch.trajectories
    assert {s.source for s in r.steps} == {"agent", "raw_tool"}
    assert batch.scores == [1.0]
    assert batch.objective_scores[0]["neg.efficiency.requests.agent"] == -10.0
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
    # count.unresolved is excluded from the denominator.
    half = vec(supported=1, missing=1)
    half = half.model_copy(update={"capability": {**half.capability, "count.unresolved": "4"}})
    assert str(claim_score(half)) == "0.5"
    assert claim_score(vec(supported=0)) == 0
    assert str(claim_score(vec(supported=1, fs=1))) == "0.5"  # contradicted is graded
    obj = objective_scores(vec(supported=2, fs=1, cost="0.25"), SPEC.efficiency_gate)
    assert obj["capability.count.supported"] == 2.0
    assert obj["neg.capability.false_success_claims"] == -1.0
    assert obj["neg.efficiency.cost_usd"] == -0.25
    assert set(obj) == {
        *(f"{'neg.' if d == 'lower' else ''}capability.{m}" for m, d in CAPABILITY_METRICS.items()),
        *(f"neg.efficiency.{m}" for m in SPEC.efficiency_gate),
    }
    assert objective_scores(vec(cost=None), SPEC.efficiency_gate)[
        "neg.efficiency.cost_usd"
    ] == float("-inf")
    with pytest.raises(ValueError):
        Trial(case_id="c", run_id="r", outcome=vec(), verdicts={"m1.r1": "probably"})


def test_reflection_feedback_names_reason_classes_and_verdict_meanings():
    r = Rollout(
        "c",
        "t1",
        "d",
        vec(),
        None,
        Decimal("0.5"),
        verdicts={"m1.r1": "contradicted", "m3.r6": "unsupported", "m1.r2": "supported"},
        reasons={"m1.r1": "unit"},
    )
    feedback = reflective_record(r)["Feedback"]
    assert "m1.r1=contradicted (unit)" in feedback and "m3.r6=unsupported (" not in feedback
    assert "unsupported: the output has no state with this (multiplicity, root)" in feedback
    assert "(unit): reported in a different unit" in feedback
    assert "supported: matches" not in feedback  # only failing verdicts are explained
    # With repeats: one trace (the worst run's), feedback for every run.
    good = replace(r, trial_id="t2", score=Decimal(1), verdicts={"m1.r1": "supported"}, reasons={})
    good = replace(good, steps=(TraceStep(kind="response", source="agent", text="GOOD RUN"),))
    both = replace(r, repeats=(good,))
    rec = reflective_record(both)
    assert "Run 1: Claim score 0.5" in rec["Feedback"] and "Run 2: Claim score 1" in rec["Feedback"]
    assert "GOOD RUN" not in rec["Generated Outputs"]
    with pytest.raises(ValueError):
        Trial(case_id="c", run_id="r", outcome=vec(), reasons={"m1.r1": "the answer is 0.19"})


def test_failed_trials_are_charged_and_their_text_not_kept(tmp_path):
    runner = SpyRunner(crash_cases=("c2",))
    result = optimize(tmp_path, runner, ScriptedReflection(["EFFICIENT"]), max_reflections=1)
    store, failed = journal(tmp_path, "optimizer.trial-failed")
    assert failed and all(e.payload["error"] == "RuntimeError" for e in failed)
    assert result.trials == len(runner.calls)
    assert EVALREF_MARKER.encode() not in all_bytes(tmp_path / "opt")
    # A pool member with a failed trial has no total, so the frontier is empty.
    assert result.non_dominated == {} and result.final is None


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


def test_repeats_run_every_case_that_many_times_and_bind_the_search(tmp_path):
    runner = SpyRunner()
    result = optimize(
        tmp_path, runner, ScriptedReflection(["EFFICIENT"]), max_reflections=1, repeats=2
    )
    per_pair = Counter((digest, case) for digest, case, _ in runner.calls)
    assert per_pair and all(n % 2 == 0 for n in per_pair.values())
    assert result.trials == len(runner.calls)
    store, searches = journal(tmp_path, "optimizer.search")
    assert [e.payload["repeats"] for e in searches] == [2]
    # Every run of a proposal's minibatch is cited by its candidate record.
    _, decisions = journal(tmp_path, "optimizer.decision")
    decision = store.get_json(decisions[0].payload_refs["decision"])
    assert len(decision["cases"]) == 2 and len(decision["child_total"]["case_ids"]) == 4
    with pytest.raises(OnodaError, match="another repeat count"):
        optimize(tmp_path, SpyRunner(), ScriptedReflection([]), max_reflections=2, repeats=3)
