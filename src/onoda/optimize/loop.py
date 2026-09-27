"""The recorded optimization loop: GEPA proposes, SoL-Pi's gates promote, the store remembers.

`run_optimization` runs `gepa.optimize` with `OnodaAdapter`, `SolPiGates` as
the acceptance criterion, `frontier_type="hybrid"` over the adapter's
objective scores, and a proposer that wraps the caller's reflection callable
(`str -> str`, GEPA's `LanguageModel` shape; tests script it). GEPA's train
and validation sets are both the split's acceptance slice: the final slice is
never passed to GEPA, so no final case can reach the runner (the adapter
refuses one anyway).

What gets recorded in the optimization store (its own `Store`, separate from
the agent and evaluator stores), each followed by a journal event under the
search id:

- the split and gate spec the search runs under (`optimizer.search`); a
  search id is bound to them and refuses to resume under different ones;
- every candidate as a `CandidateRecord` (`optimizer.candidate`): the seed,
  each proposal that failed `HarnessCandidate` validation (`invalid`, never
  run), and each gated proposal (`accepted` / `rejected` with the reasons);
- every feedback packet, i.e. the reflective dataset a proposal came from
  (`optimizer.feedback`);
- every gate decision with the parent and child minibatch totals
  (`optimizer.decision`);
- every trial's ablation `OutcomeVector` (see `adapter.py`) and each pool
  member's acceptance-slice `PooledArm` total (`optimizer.valset`);
- the final non-dominated set over those totals (`optimizer.final`).

Budgets are experiment-level and counted from the journal: `max_trials`
counts runner calls, failed ones included; `max_reflections` counts calls to
the reflection callable. The loop stops before an iteration it could not
finish (worst case: parent and child minibatches plus a full-slice
evaluation), so a decision is never cut off mid-way by the trial budget.

Resume: GEPA's `run_dir` holds its pickled state (`gepa_state.bin`, A46:
scratch, never imported or exported). GEPA 0.1.4's engine evaluates the seed
on the validation set *before* it loads that state and then discards the
result; `_ResumableFullEval` narrows that evaluation to nothing when the
state exists, so a resumed search does not re-run the seed. Record writes
are content-addressed and idempotent; one-off journal events are skipped
when already present.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import gepa
from gepa.strategies.eval_policy import FullEvaluationPolicy
from gepa.strategies.instruction_proposal import InstructionProposalSignature
from pydantic import BaseModel, ConfigDict, ValidationError

from onoda.ablation.gates import PooledArm
from onoda.adapters.pi.contract import HarnessCandidate
from onoda.errors import OnodaError
from onoda.optimize.adapter import (
    BudgetExhausted,
    OnodaAdapter,
    Pending,
    Runner,
    SearchLedger,
)
from onoda.optimize.contract import (
    CandidateRecord,
    Decision,
    GateSpec,
    SplitAssignment,
)
from onoda.optimize.gates import DecisionObserver, SolPiGates, non_dominated
from onoda.store import Store

ReflectionFn = Callable[[str], str]

GEPA_STATE_FILE = "gepa_state.bin"

REFLECTION_TEMPLATE = (
    "You are revising the `{name}` component of a scientific-agent harness. "
    "Keep it valid for its role; only its text changes.\n\n"
    + InstructionProposalSignature.default_prompt_template
)


class OptimizationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    search_id: str
    seed: str  # HarnessCandidate.digest()
    accepted: tuple[CandidateRecord, ...]
    non_dominated: dict[str, PooledArm]  # candidate digest -> acceptance-slice total
    final: str | None  # store digest of the final-set record
    stop_reason: str
    trials: int
    reflections: int


class _QuietLogger:
    """GEPA's `Logger` swaps `sys.stdout`; this only appends to the run dir's log."""

    def __init__(self, path: Path):
        self.path = path

    def log(self, message: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")


class _ResumableFullEval(FullEvaluationPolicy):
    """Full-slice evaluation, except the seed is not re-run when GEPA resumes."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    def get_seed_eval_batch(self, loader: Any) -> list[Any]:
        if (self.run_dir / GEPA_STATE_FILE).exists():
            return []
        return list(loader.all_ids())


def _record_candidate(ledger: SearchLedger, rec: CandidateRecord, **refs: str) -> str:
    ref = ledger.put(rec.model_dump(mode="json", by_alias=True), "optimizer-candidate")
    ledger.event(
        "optimizer.candidate",
        refs={"record": ref, **refs},
        payload={"decision": rec.decision, "candidate": rec.candidate},
        once=True,
    )
    return ref


class _Proposer:
    """GEPA `custom_candidate_proposer`: record the feedback, reflect, validate."""

    def __init__(
        self, ledger: SearchLedger, adapter: OnodaAdapter, reflect: ReflectionFn, search_id: str
    ):
        self.ledger = ledger
        self.adapter = adapter
        self.reflect = reflect
        self.search_id = search_id

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        ledger = self.ledger
        if ledger.reflections + len(components_to_update) > ledger.max_reflections:
            return {}  # GEPA skips the proposal; the stopper ends the search
        parent = HarnessCandidate(components=candidate).digest()
        iteration = ledger.proposals + 1
        dataset = {k: [dict(x) for x in reflective_dataset[k]] for k in components_to_update}
        packet = {
            "schema": "onoda.optimizer-feedback/1",
            "search_id": self.search_id,
            "iteration": iteration,
            "parent": parent,
            "components": list(components_to_update),
            "dataset": dataset,
        }
        feedback = ledger.put(packet, "optimizer-feedback")
        ledger.event("optimizer.feedback", refs={"packet": feedback}, payload={"parent": parent})

        new_texts: dict[str, str] = {}
        for name in components_to_update:
            ledger.charge_reflection(name)
            prompt = InstructionProposalSignature.prompt_renderer(
                {
                    "current_instruction_doc": candidate[name],
                    "dataset_with_feedback": dataset[name],
                    "prompt_template": REFLECTION_TEMPLATE.replace("{name}", name),
                }
            )
            assert isinstance(prompt, str)  # no images in an onoda dataset
            out = self.reflect(prompt).strip()
            new_texts[name] = InstructionProposalSignature.output_extractor(out)["new_instruction"]

        child = {**candidate, **new_texts}
        pend = Pending(parent, feedback, iteration, child)

        def refuse(
            decision: Decision, reasons: tuple[str, ...], digest: str | None
        ) -> dict[str, str]:
            _record_candidate(ledger, pend.record(self.search_id, digest, decision, reasons))
            return {}  # no texts: GEPA runs nothing for this proposal

        try:
            harness = HarnessCandidate(components=child)
        except ValidationError as exc:
            return refuse("invalid", tuple(str(e["msg"]) for e in exc.errors()), None)
        if child == candidate:
            return refuse("rejected", ("proposal is identical to its parent",), parent)
        self.adapter.pending[harness.digest()] = pend
        return new_texts


def _decision_observer(
    ledger: SearchLedger, adapter: OnodaAdapter, search_id: str
) -> DecisionObserver:
    def observe(
        proposal: Any,
        parent_total: PooledArm | None,
        child_total: PooledArm | None,
        accepted: bool,
        reasons: tuple[str, ...],
    ) -> None:
        digest = HarnessCandidate(components=proposal.candidate).digest()
        pend = adapter.pending.pop(digest, None)
        if pend is None:
            raise OnodaError(f"gates judged a proposal the optimizer did not make: {digest}")
        after = proposal.eval_after.outputs if proposal.eval_after is not None else []
        decision = {
            "schema": "onoda.optimizer-decision/1",
            "search_id": search_id,
            "candidate": digest,
            "parent": pend.parent,
            "cases": [r.case_id for r in after],
            "parent_total": parent_total.model_dump(mode="json") if parent_total else None,
            "child_total": child_total.model_dump(mode="json") if child_total else None,
            "accepted": accepted,
            "reasons": list(reasons),
        }
        decision_ref = ledger.put(decision, "optimizer-decision")
        ledger.event("optimizer.decision", refs={"decision": decision_ref})
        rec = pend.record(
            search_id,
            digest,
            "accepted" if accepted else "rejected",
            reasons,
            outcomes=tuple(r.outcome_ref for r in after if r.outcome_ref is not None),
        )
        _record_candidate(ledger, rec, decision=decision_ref)
        if accepted:
            adapter.awaiting_valset.add(digest)

    return observe


def _bind_search(ledger: SearchLedger, split: SplitAssignment, spec: GateSpec) -> None:
    split_ref = ledger.put(split.model_dump(mode="json", by_alias=True), "optimizer-split")
    spec_ref = ledger.put(spec.model_dump(mode="json", by_alias=True), "optimizer-gates")
    for e in ledger.events("optimizer.search"):
        if e.payload_refs != {"split": split_ref, "gates": spec_ref}:
            raise OnodaError(f"search {ledger.search_id} is bound to another split or gate spec")
    ledger.event("optimizer.search", refs={"split": split_ref, "gates": spec_ref}, once=True)


def _final_set(ledger: SearchLedger, spec: GateSpec) -> tuple[dict[str, PooledArm], str | None]:
    refs: dict[str, str] = {}
    totals: dict[str, PooledArm] = {}
    for e in ledger.events("optimizer.valset"):
        cand, ref = e.payload.get("candidate"), e.payload_refs.get("total")
        if ref is not None and cand not in totals:
            refs[cand] = ref
            totals[cand] = PooledArm.model_validate(ledger.store.get_json(ref))
    front = non_dominated(totals, spec)
    if not front:
        return {}, None
    record = {
        "schema": "onoda.optimizer-final/2",
        "search_id": ledger.search_id,
        "non_dominated": front,
        "totals": [refs[c] for c in front],
        "pool": sorted(totals),
    }
    ref = ledger.put(record, "optimizer-final")
    ledger.event("optimizer.final", refs={"final": ref}, once=True)
    return {c: totals[c] for c in front}, ref


def run_optimization(
    *,
    runner: Runner,
    seed: HarnessCandidate,
    split: SplitAssignment,
    gates: GateSpec,
    reflect: ReflectionFn,
    store: Store,
    search_id: str,
    run_dir: str | os.PathLike[str],
    max_trials: int,
    max_reflections: int,
    minibatch_size: int = 2,
) -> OptimizationResult:
    """Search from `seed` over `split.acceptance`; see the module docstring.

    Calling it again with the same `run_dir`, `search_id` and store resumes
    the search; the budgets are totals for the search, not per call.
    """
    if min(max_trials, max_reflections, minibatch_size) < 1:
        raise OnodaError("budgets and minibatch size must be positive")
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    ledger = SearchLedger(store, search_id, max_trials=max_trials, max_reflections=max_reflections)
    _bind_search(ledger, split, gates)
    seed_digest = seed.digest()
    _record_candidate(
        ledger,
        CandidateRecord(
            search_id=search_id,
            iteration=0,
            candidate=seed_digest,
            components=seed.components,
            origin="seed",
            decision="seed",
        ),
    )

    cases = list(split.acceptance)
    adapter = OnodaAdapter(runner, ledger, cases=cases, efficiency_gate=gates.efficiency_gate)
    resuming = (run_path / GEPA_STATE_FILE).exists()
    if not any(
        e.payload.get("candidate") == seed_digest for e in ledger.events("optimizer.valset")
    ):
        adapter.awaiting_valset.add(seed_digest)

    iteration_cost = 2 * minibatch_size + len(cases)
    stop_reason = "stopped"

    def stopper(state: Any) -> bool:
        nonlocal stop_reason
        if ledger.reflections >= max_reflections:
            stop_reason = f"reflection budget spent ({ledger.reflections}/{max_reflections})"
            return True
        if ledger.trials + iteration_cost > max_trials:
            stop_reason = (
                f"trial budget cannot cover another iteration ({ledger.trials}/{max_trials} "
                f"used, up to {iteration_cost} needed)"
            )
            return True
        return False

    if not resuming and ledger.trials + len(cases) > max_trials:
        stop_reason = "trial budget cannot cover the seed evaluation"
    else:
        try:
            gepa.optimize(
                seed_candidate=dict(seed.components),
                trainset=cases,
                valset=cases,
                adapter=adapter,
                custom_candidate_proposer=_Proposer(ledger, adapter, reflect, search_id),
                acceptance_criterion=SolPiGates(
                    gates, on_decision=_decision_observer(ledger, adapter, search_id)
                ),
                frontier_type="hybrid",
                val_evaluation_policy=_ResumableFullEval(run_path),
                reflection_minibatch_size=minibatch_size,
                skip_perfect_score=False,  # a perfect score can still get cheaper
                stop_callbacks=[stopper],
                run_dir=str(run_path),
                logger=_QuietLogger(run_path / "run_log.txt"),
                track_best_outputs=False,
                cache_evaluation=False,
                use_merge=False,
                seed=split.seed,
                raise_on_exception=True,
            )
        except BudgetExhausted as exc:  # backstop; the stopper normally ends first
            stop_reason = str(exc)
            for digest, pend in adapter.pending.items():
                reasons = ("budget spent before the gates could judge it",)
                _record_candidate(ledger, pend.record(search_id, digest, "rejected", reasons))
            adapter.pending.clear()

    front, final_ref = _final_set(ledger, gates)
    accepted = [
        CandidateRecord.model_validate(store.get_json(e.payload_refs["record"]))
        for e in ledger.events("optimizer.candidate")
        if e.payload.get("decision") == "accepted"
    ]
    return OptimizationResult(
        search_id=search_id,
        seed=seed_digest,
        accepted=tuple(accepted),
        non_dominated=front,
        final=final_ref,
        stop_reason=stop_reason,
        trials=ledger.trials,
        reflections=ledger.reflections,
    )
