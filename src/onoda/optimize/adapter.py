"""GEPA adapter over recorded runs: the `Runner` seam, scores, and the reflective dataset.

GEPA drives evaluation through `OnodaAdapter.evaluate(batch, candidate)`. A
batch is a list of case ids (only the acceptance slice is ever handed in;
anything else is refused before the runner is called). For each case the
adapter asks the injected `Runner` for one `Trial`: a real recorded run in
the agent store, graded by the evaluator. M4-007 implements `Runner` over Pi;
tests use a fake.

Scores. GEPA's per-example score is the case's claim precision from the
evaluator's verdict labels,

    supported / (supported + missing + contradicted + unsupported)

(`unresolved` claims are excluded; no graded claim scores 0). It is computed
as a `Decimal` and turned into a float only at the GEPA boundary; it drives
GEPA's candidate selection, never promotion, which is the SoL-Pi gates' job
(`gates.py`) over the `OutcomeVector`. `objective_scores` carries the vector
for GEPA's hybrid frontier, oriented higher-is-better: capability metrics as
is, false-success claims and efficiency metrics negated.

Reflective dataset. Built only from trace steps whose source is `agent` or
`raw_tool`, plus the verdict words per claim tag. Reducer receipts and
evaluator references are dropped here, whatever the runner sent: the adapter
does not trust the runner to have filtered them. Nothing else from a trial
(run ids aside) reaches reflection or the optimization store.

Records. `SearchLedger` writes the optimization store: every trial's
`OutcomeVector` (label `optimizer-outcome`) and a journal under the search id.
A trial is charged (journal event `optimizer.trial`) before the runner is
called, so a crash can never un-spend budget, and the experiment-level trial
and reflection budgets are counted from that journal, which survives resume.
Floats never enter the store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from gepa.core.adapter import EvaluationBatch
from pydantic import BaseModel, ConfigDict, Field, field_validator

from onoda.adapters.pi.contract import HarnessCandidate
from onoda.adapters.pi.task import VERDICTS
from onoda.errors import OnodaError
from onoda.optimize.contract import CAPABILITY_METRICS, EFFICIENCY_METRICS, OutcomeVector
from onoda.store import Store, check_safe_id

AGENT_VISIBLE_SOURCES = ("agent", "raw_tool")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._?-]{0,63}$")
_GRADED = ("supported", "missing", "contradicted", "unsupported")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TraceStep(_Frozen):
    """One step of a trial's trace, tagged with where its text came from."""

    kind: str = Field(min_length=1, max_length=64)  # request, tool_call, tool_result, claim, ...
    source: Literal["agent", "raw_tool", "reducer", "evaluator_reference"]
    text: str


class Trial(_Frozen):
    """One candidate run on one case, as the `Runner` reports it."""

    case_id: str
    run_id: str  # in the agent store
    outcome: OutcomeVector
    steps: tuple[TraceStep, ...] = ()
    verdicts: dict[str, str] = Field(default_factory=dict)  # claim tag -> verdict word only

    @field_validator("verdicts")
    @classmethod
    def _verdicts(cls, v: dict[str, str]) -> dict[str, str]:
        for tag, word in v.items():
            if not _TAG_RE.match(tag):
                raise ValueError(f"verdict tag {tag!r} is not a claim tag")
            if word not in VERDICTS:
                raise ValueError(f"verdict {word!r} for {tag} is not one of {', '.join(VERDICTS)}")
        return v


class Runner(Protocol):
    """Runs a candidate on one case and returns the graded trial (M4-007: Pi)."""

    def run(self, candidate: HarnessCandidate, case_id: str, *, trial_id: str) -> Trial: ...


class BudgetExhausted(OnodaError):
    """The search's trial or reflection budget is spent."""


def claim_score(verdicts: Mapping[str, str]) -> Decimal:
    """supported / graded claims (unresolved excluded); 0 when nothing was graded."""
    words = list(verdicts.values())
    graded = sum(words.count(w) for w in _GRADED)
    return Decimal(words.count("supported")) / graded if graded else Decimal(0)


def objective_scores(v: OutcomeVector) -> dict[str, float]:
    """The vector as GEPA objectives, all higher-is-better (floats only for GEPA)."""
    out = {f"capability.{m}": float(Decimal(v.capability[m])) for m in CAPABILITY_METRICS}
    out["neg.false_success_claims"] = float(-v.false_success_claims)
    out.update(
        {f"neg.efficiency.{m}": float(-Decimal(v.efficiency[m])) for m in EFFICIENCY_METRICS}
    )
    return out


def _failed_objectives() -> dict[str, float]:
    out = {f"capability.{m}": 0.0 for m in CAPABILITY_METRICS}
    out["neg.false_success_claims"] = float("-inf")
    out.update({f"neg.efficiency.{m}": float("-inf") for m in EFFICIENCY_METRICS})
    return out


@dataclass(frozen=True)
class Rollout:
    """GEPA's output and trajectory for one trial: agent-visible content only."""

    case_id: str
    trial_id: str
    candidate: str
    outcome: OutcomeVector | None  # None: the trial failed
    outcome_ref: str | None  # store digest of the outcome JSON
    score: Decimal
    verdicts: dict[str, str] = field(default_factory=dict)
    steps: tuple[TraceStep, ...] = ()  # sources in AGENT_VISIBLE_SOURCES only
    failed: str | None = None


# ------------------------------------------------------------------- ledger


class SearchLedger:
    """The optimization store and journal of one search, plus its budgets.

    Journal event types (run id = search id): `optimizer.search`,
    `optimizer.candidate`, `optimizer.trial`, `optimizer.outcome`,
    `optimizer.trial-failed`, `optimizer.reflection`, `optimizer.feedback`,
    `optimizer.decision`, `optimizer.valset`, `optimizer.final`. Records are
    content-addressed JSON, so rewriting one on resume is a no-op; events
    marked `once` are skipped when an identical ref is already journalled.
    """

    ACTOR = "optimizer"

    def __init__(
        self, store: Store, search_id: str, *, max_trials: int, max_reflections: int
    ) -> None:
        self.store = store
        self.search_id = check_safe_id(search_id, "search id")
        self.max_trials = max_trials
        self.max_reflections = max_reflections
        self.trials = 0
        self.reflections = 0
        self.proposals = 0
        self._seen: set[tuple[str, str]] = set()
        for e in store.events(self.search_id):
            self._count(e.type, e.payload_refs)

    def _count(self, type_: str, refs: Mapping[str, str]) -> None:
        self.trials += type_ == "optimizer.trial"
        self.reflections += type_ == "optimizer.reflection"
        self.proposals += type_ == "optimizer.feedback"
        self._seen.update((type_, r) for r in refs.values())

    def events(self, type_: str) -> list[Any]:
        return [e for e in self.store.events(self.search_id) if e.type == type_]

    def put(self, value: Any, label: str) -> str:
        return self.store.put_json(value, labels=[label]).digest

    def event(
        self,
        type_: str,
        *,
        refs: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        once: bool = False,
    ) -> None:
        refs = dict(refs or {})
        if once and refs and all((type_, r) in self._seen for r in refs.values()):
            return
        self.store.append_event(
            self.search_id,
            actor=self.ACTOR,
            type=type_,
            payload=dict(payload or {}),
            payload_refs=refs,
        )
        self._count(type_, refs)

    def charge_trial(self, candidate: str, case_id: str) -> str:
        if self.trials >= self.max_trials:
            raise BudgetExhausted(f"trial budget {self.max_trials} spent")
        trial_id = f"{self.search_id}.t{self.trials + 1:06d}"
        self.event(
            "optimizer.trial",
            payload={"trial_id": trial_id, "case": case_id, "candidate": candidate},
        )
        return trial_id

    def charge_reflection(self, component: str) -> None:
        if self.reflections >= self.max_reflections:
            raise BudgetExhausted(f"reflection budget {self.max_reflections} spent")
        self.event("optimizer.reflection", payload={"component": component})

    def record_valset(self, candidate: str, rollouts: Sequence[Rollout]) -> None:
        """A pool member's full acceptance-slice evaluation (for the final frontier)."""
        vectors = [r.outcome for r in rollouts]
        if any(v is None for v in vectors):
            self.event("optimizer.valset", payload={"candidate": candidate, "failed": True})
            return
        total = OutcomeVector.total([v for v in vectors if v is not None])
        ref = self.put(total.model_dump(mode="json", by_alias=True), "optimizer-valset")
        self.event(
            "optimizer.valset", refs={"total": ref}, payload={"candidate": candidate}, once=True
        )


# ------------------------------------------------------------------ adapter


@dataclass
class Pending:
    """A proposal between reflection and its gate decision."""

    parent: str
    feedback: str
    iteration: int
    components: dict[str, str]


class OnodaAdapter:
    """`GEPAAdapter[str, Rollout, Rollout]`: DataInst is a case id."""

    propose_new_texts = None  # the loop passes its own proposer to GEPA

    def __init__(self, runner: Runner, ledger: SearchLedger, *, cases: Sequence[str]):
        self.runner = runner
        self.ledger = ledger
        self.cases = frozenset(cases)
        # Candidate digests whose next evaluation is GEPA's full-slice one
        # (the seed at start, a child right after its gates accept it).
        self.awaiting_valset: set[str] = set()
        self.pending: dict[str, Pending] = {}

    def _trial(self, candidate: HarnessCandidate, digest: str, case_id: str) -> Rollout:
        trial_id = self.ledger.charge_trial(digest, case_id)
        try:
            trial = self.runner.run(candidate, case_id, trial_id=trial_id)
            if not isinstance(trial, Trial):
                raise TypeError("runner did not return a Trial")
            if trial.case_id != case_id or trial.outcome.candidate != digest:
                raise ValueError("runner returned a trial for another case or candidate")
            if tuple(trial.outcome.cases) != (case_id,):
                raise ValueError("trial outcome must cover exactly its case")
        except Exception as exc:  # a failed trial is data, not a crash (GEPA's contract)
            failed = type(exc).__name__  # never the message: it could quote anything
            self.ledger.event(
                "optimizer.trial-failed",
                payload={"trial_id": trial_id, "case": case_id, "error": failed},
            )
            return Rollout(case_id, trial_id, digest, None, None, Decimal(0), failed=failed)
        ref = self.ledger.put(
            trial.outcome.model_dump(mode="json", by_alias=True), "optimizer-outcome"
        )
        self.ledger.event(
            "optimizer.outcome",
            refs={"outcome": ref},
            payload={"trial_id": trial_id, "case": case_id, "run": trial.run_id},
        )
        steps = tuple(s for s in trial.steps if s.source in AGENT_VISIBLE_SOURCES)
        return Rollout(
            case_id,
            trial_id,
            digest,
            trial.outcome,
            ref,
            claim_score(trial.verdicts),
            dict(trial.verdicts),
            steps,
        )

    def evaluate(
        self, batch: list[str], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvaluationBatch[Rollout, Rollout]:
        outside = sorted(set(batch) - self.cases)
        if outside:
            raise OnodaError(f"cases outside the acceptance slice: {', '.join(outside)}")
        harness = HarnessCandidate(components=candidate)  # proposer already validated
        digest = harness.digest()
        rollouts = [self._trial(harness, digest, case_id) for case_id in batch]
        if batch and digest in self.awaiting_valset:
            self.awaiting_valset.discard(digest)
            self.ledger.record_valset(digest, rollouts)
        return EvaluationBatch(
            outputs=list(rollouts),
            scores=[float(r.score) for r in rollouts],
            trajectories=list(rollouts) if capture_traces else None,
            objective_scores=[
                objective_scores(r.outcome) if r.outcome is not None else _failed_objectives()
                for r in rollouts
            ],
        )

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[Rollout, Rollout],
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        records = [reflective_record(r) for r in eval_batch.trajectories or []]
        return {name: [dict(rec) for rec in records] for name in components_to_update}


def reflective_record(r: Rollout) -> dict[str, Any]:
    """One example for reflection: agent-visible steps and verdict words only."""
    steps = [s for s in r.steps if s.source in AGENT_VISIBLE_SOURCES]  # re-filter: never trust
    if r.failed is not None:
        feedback = f"The run failed ({r.failed}); no claims were graded."
    else:
        verdicts = ", ".join(f"{t}={w}" for t, w in sorted(r.verdicts.items())) or "none"
        feedback = f"Claim score {r.score:f}. Evaluator verdicts per claim: {verdicts}."
    return {
        "Inputs": {"case": r.case_id},
        "Generated Outputs": "\n".join(f"[{s.source} {s.kind}] {s.text}" for s in steps)
        or "(no agent-visible steps)",
        "Feedback": feedback,
    }
