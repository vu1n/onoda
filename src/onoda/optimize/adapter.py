"""GEPA adapter over recorded runs: the `Runner` seam, scores, and the reflective dataset.

GEPA drives evaluation through `OnodaAdapter.evaluate(batch, candidate)`. A
batch is a list of case ids (only the acceptance slice is ever handed in;
anything else is refused before the runner is called). For each case the
adapter asks the injected `Runner` for one `Trial`: a real recorded run in
the agent store, graded by the evaluator. M4-007 implements `Runner` over Pi;
tests use a fake.

Scores. A trial's outcome is M3b's per-run ablation `OutcomeVector`
(`arm` = the run's variant). GEPA's per-example score is the case's claim
precision from its capability counts,

    count.supported / (count.supported + count.missing
                       + count.contradicted + count.unsupported)

(`count.unresolved` is excluded; no graded claim scores 0). It is computed
as a `Decimal` and turned into a float only at the GEPA boundary; it drives
GEPA's candidate selection, never promotion, which is the SoL-Pi gates' job
(`gates.py`). `objective_scores` carries every capability metric and the
spec's gated efficiency metrics for GEPA's hybrid frontier, oriented
higher-is-better with M3b's directions ("lower" metrics negated); an
unreported value is `-inf`.

Reflective dataset. Built only from trace steps whose source is `agent` or
`raw_tool`, plus the verdict word and reason class (A53) per claim tag. Reducer receipts and
evaluator references are dropped here, whatever the runner sent: the adapter
does not trust the runner to have filtered them. Nothing else from a trial
(run ids aside) reaches reflection or the optimization store.

Records. `SearchLedger` writes the optimization store: every trial's
ablation `OutcomeVector` (label `optimizer-outcome`), each pool member's
acceptance-slice `PooledArm` (label `optimizer-valset`), and a journal under
the search id.
A trial is charged (journal event `optimizer.trial`) before the runner is
called, so a crash can never un-spend budget, and the experiment-level trial
and reflection budgets are counted from that journal, which survives resume.
Floats never enter the store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, Protocol

from gepa.core.adapter import EvaluationBatch
from pydantic import Field, field_validator

from onoda.ablation.contract import CAPABILITY_METRICS, EFFICIENCY_METRICS, OutcomeVector
from onoda.adapters.pi.contract import HarnessCandidate
from onoda.adapters.pi.task import VERDICTS
from onoda.errors import OnodaError
from onoda.optimize.contract import CandidateRecord, Decision
from onoda.optimize.gates import pool_outputs
from onoda.records import Frozen as _Frozen
from onoda.store import Store, check_safe_id

# Context: doc://onoda/optimizer-scope@0002#seed-by-promotion-only — reflection sees agent-visible steps and reason classes, never receipts or references
AGENT_VISIBLE_SOURCES = ("agent", "raw_tool")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._?-]{0,63}$")
# What each verdict word means, and the evaluator's reason classes (A53). Fixed
# text about the grading rule, never a reference value: reflection learns why a
# claim failed, not what the right answer was.
VERDICT_MEANINGS = {
    "supported": "matches the output",
    "contradicted": "the state exists in the output but the reported value differs",
    "unsupported": "the output has no state with this (multiplicity, root) identity",
    "missing": "a requested state that was not reported",
    "unresolved": "the output does not attribute this state's identity",
}
REASON_CLASSES = {"unit": "reported in a different unit than the output prints"}
_GRADED = ("count.supported", "count.missing", "count.contradicted", "count.unsupported")


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
    reasons: dict[str, str] = Field(default_factory=dict)  # claim tag -> REASON_CLASSES key

    @field_validator("reasons")
    @classmethod
    def _reasons(cls, v: dict[str, str]) -> dict[str, str]:
        for tag, reason in v.items():
            if not _TAG_RE.match(tag):
                raise ValueError(f"reason tag {tag!r} is not a claim tag")
            if reason not in REASON_CLASSES:
                raise ValueError(f"reason {reason!r} for {tag} is not a known reason class")
        return v

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
    """The search's trial, reflection or spend budget is spent."""


@dataclass(frozen=True)
class SpendCap:
    """A live search's money budget (A51), all decimal USD.

    `max_cost_usd` bounds the whole search. A trial is reserved at its own
    per-run cap (`trial_reserve`) before it runs and settles at the cost its
    outcome reports; a trial that fails or reports no cost stays charged at
    the reserve. A reflection call is reserved at its per-call cap and
    settles at the cost its receipt reports.
    """

    max_cost_usd: Decimal
    trial_reserve: Decimal
    reflection_reserve: Decimal

    def __post_init__(self) -> None:
        if min(self.max_cost_usd, self.trial_reserve, self.reflection_reserve) <= 0:
            raise OnodaError("spend cap and reserves must be positive")


def claim_score(outcome: OutcomeVector) -> Decimal:
    """count.supported / graded claims (unresolved excluded); 0 when nothing was graded."""
    graded = sum(Decimal(outcome.capability[m]) for m in _GRADED)
    return Decimal(outcome.capability["count.supported"]) / graded if graded else Decimal(0)


def _objectives(efficiency_gate: Sequence[str]) -> list[tuple[str, str, str]]:
    """(metric, vector field, direction) for every GEPA objective."""
    return [
        *((m, "capability", d) for m, d in CAPABILITY_METRICS.items()),
        *((m, "efficiency", EFFICIENCY_METRICS[m]) for m in efficiency_gate),
    ]


def _key(fld: str, metric: str, direction: str) -> str:
    return f"{'neg.' if direction == 'lower' else ''}{fld}.{metric}"


def objective_scores(v: OutcomeVector, efficiency_gate: Sequence[str]) -> dict[str, float]:
    """The vector as GEPA objectives, all higher-is-better (floats only for GEPA).

    Capability metrics (all) and the gated efficiency metrics; "lower"
    metrics are negated; an unreported value is -inf.
    """
    out: dict[str, float] = {}
    for metric, fld, direction in _objectives(efficiency_gate):
        x = getattr(v, fld).get(metric)
        if x is None:
            out[_key(fld, metric, direction)] = float("-inf")
        else:
            out[_key(fld, metric, direction)] = float(
                -Decimal(x) if direction == "lower" else Decimal(x)
            )
    return out


def _failed_objectives(efficiency_gate: Sequence[str]) -> dict[str, float]:
    return {
        _key(fld, metric, direction): 0.0 if direction == "higher" else float("-inf")
        for metric, fld, direction in _objectives(efficiency_gate)
    }


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
    reasons: dict[str, str] = field(default_factory=dict)
    steps: tuple[TraceStep, ...] = ()  # sources in AGENT_VISIBLE_SOURCES only
    failed: str | None = None
    repeats: tuple[Rollout, ...] = ()  # further trials of the same case and candidate

    @property
    def runs(self) -> tuple[Rollout, ...]:
        return (self, *self.repeats)

    @property
    def outcomes(self) -> tuple[OutcomeVector | None, ...]:
        return tuple(r.outcome for r in self.runs)

    @property
    def mean_score(self) -> Decimal:
        return sum((r.score for r in self.runs), Decimal(0)) / len(self.runs)


# ------------------------------------------------------------------- ledger


class SearchLedger:
    """The optimization store and journal of one search, plus its budgets.

    Journal event types (run id = search id): `optimizer.search`,
    `optimizer.candidate`, `optimizer.trial`, `optimizer.outcome`,
    `optimizer.trial-failed`, `optimizer.reflection`, `optimizer.reflection-call`,
    `optimizer.feedback`,
    `optimizer.decision`, `optimizer.valset`, `optimizer.final`. Records are
    content-addressed JSON, so rewriting one on resume is a no-op; events
    marked `once` are skipped when an identical ref is already journalled.
    """

    ACTOR = "optimizer"

    def __init__(
        self,
        store: Store,
        search_id: str,
        *,
        max_trials: int,
        max_reflections: int,
        spend: SpendCap | None = None,
    ) -> None:
        self.store = store
        self.search_id = check_safe_id(search_id, "search id")
        self.max_trials = max_trials
        self.max_reflections = max_reflections
        self.spend = spend
        self.trials = 0
        self.reflections = 0
        self.proposals = 0
        # Spend (A51): trial id -> settled cost (None: still at its reserve), and
        # settled reflection-call costs. Counted from the journal, so it survives resume.
        self._trial_cost: dict[str, Decimal | None] = {}
        self._reflection_cost = Decimal(0)
        self._seen: set[tuple[str, str]] = set()
        for e in store.events(self.search_id):
            self._count(e.type, e.payload_refs, e.payload)

    def _count(
        self, type_: str, refs: Mapping[str, str], payload: Mapping[str, Any] | None = None
    ) -> None:
        payload = payload or {}
        self.trials += type_ == "optimizer.trial"
        self.reflections += type_ == "optimizer.reflection"
        self.proposals += type_ == "optimizer.feedback"
        if type_ == "optimizer.trial":
            self._trial_cost[payload["trial_id"]] = None
        elif type_ == "optimizer.outcome" and payload.get("cost_usd") is not None:
            self._trial_cost[payload["trial_id"]] = Decimal(payload["cost_usd"])
        elif type_ == "optimizer.reflection-call":
            # A call without a cost figure is charged at its reserve (fail closed).
            cost = payload.get("cost_usd")
            reserve = self.spend.reflection_reserve if self.spend else Decimal(0)
            self._reflection_cost += Decimal(cost) if cost is not None else reserve
        self._seen.update((type_, r) for r in refs.values())

    @property
    def spent_usd(self) -> Decimal:
        """Settled costs plus the reserve of every trial that has not settled."""
        reserve = self.spend.trial_reserve if self.spend else Decimal(0)
        costs = (c if c is not None else reserve for c in self._trial_cost.values())
        trials = sum(costs, Decimal(0))
        return trials + self._reflection_cost

    def can_afford(self, trials: int, reflections: int) -> bool:
        """Whether `trials` more trials and `reflections` more calls fit, at their reserves."""
        if self.spend is None:
            return True
        need = trials * self.spend.trial_reserve + reflections * self.spend.reflection_reserve
        return self.spent_usd + need <= self.spend.max_cost_usd

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
        self._count(type_, refs, payload)

    def charge_trial(self, candidate: str, case_id: str) -> str:
        if self.trials >= self.max_trials:
            raise BudgetExhausted(f"trial budget {self.max_trials} spent")
        if not self.can_afford(1, 0):
            assert self.spend is not None
            raise BudgetExhausted(f"spend cap {self.spend.max_cost_usd} USD cannot cover a trial")
        trial_id = f"{self.search_id}.t{self.trials + 1:06d}"
        self.event(
            "optimizer.trial",
            payload={"trial_id": trial_id, "case": case_id, "candidate": candidate},
        )
        return trial_id

    def charge_reflection(self, component: str) -> None:
        if self.reflections >= self.max_reflections:
            raise BudgetExhausted(f"reflection budget {self.max_reflections} spent")
        if not self.can_afford(0, 1):
            assert self.spend is not None
            raise BudgetExhausted(
                f"spend cap {self.spend.max_cost_usd} USD cannot cover a reflection call"
            )
        self.event("optimizer.reflection", payload={"component": component})

    def record_reflection_call(
        self, component: str, prompt: str, reply: str | None, receipt: Mapping[str, Any] | None
    ) -> None:
        """One call to the reflection model: its prompt, reply and (live) receipt."""
        refs = {"prompt": self.put({"text": prompt}, "optimizer-reflection-prompt")}
        if reply is not None:
            refs["reply"] = self.put({"text": reply}, "optimizer-reflection-reply")
        if receipt is not None:
            refs["receipt"] = self.put(dict(receipt), "optimizer-reflection-receipt")
        self.event(
            "optimizer.reflection-call",
            refs=refs,
            payload={
                "component": component,
                "cost_usd": receipt.get("cost_usd") if receipt is not None else None,
                "live": receipt is not None,
            },
        )

    def record_valset(self, candidate: str, rollouts: Sequence[Rollout]) -> None:
        """A pool member's full acceptance-slice evaluation (for the final frontier)."""
        total = pool_outputs(rollouts)
        if total is None:  # a trial failed, or the trials span variants
            self.event("optimizer.valset", payload={"candidate": candidate, "failed": True})
            return
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

    def record(
        self,
        search_id: str,
        candidate: str | None,
        decision: Decision,
        reasons: tuple[str, ...],
        outcomes: tuple[str, ...] = (),
    ) -> CandidateRecord:
        """This proposal's `CandidateRecord` (`candidate` is None when it is invalid)."""
        return CandidateRecord(
            search_id=search_id,
            iteration=self.iteration,
            candidate=candidate,
            components=self.components,
            parents=(self.parent,),
            origin="reflection",
            decision=decision,
            reasons=reasons,
            outcomes=outcomes,
            feedback=self.feedback,
        )


class OnodaAdapter:
    """`GEPAAdapter[str, Rollout, Rollout]`: DataInst is a case id."""

    propose_new_texts = None  # the loop passes its own proposer to GEPA

    def __init__(
        self,
        runner: Runner,
        ledger: SearchLedger,
        *,
        cases: Sequence[str],
        efficiency_gate: Sequence[str],
        repeats: int = 1,
    ):
        if repeats < 1:
            raise OnodaError("repeats must be positive")
        self.runner = runner
        self.repeats = repeats
        self.ledger = ledger
        self.cases = frozenset(cases)
        self.efficiency_gate = tuple(efficiency_gate)
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
            if trial.case_id != case_id or trial.outcome.case_id != case_id:
                raise ValueError("runner returned a trial for another case")
            if trial.outcome.run_id != trial.run_id:
                raise ValueError("trial outcome is for another run")
            score = claim_score(trial.outcome)  # a vector missing a count fails the trial
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
            payload={
                "trial_id": trial_id,
                "case": case_id,
                "run": trial.run_id,
                "cost_usd": trial.outcome.efficiency.get("cost_usd"),
            },
        )
        steps = tuple(s for s in trial.steps if s.source in AGENT_VISIBLE_SOURCES)
        return Rollout(
            case_id,
            trial_id,
            digest,
            trial.outcome,
            ref,
            score,
            verdicts=dict(trial.verdicts),
            reasons=dict(trial.reasons),
            steps=steps,
        )

    def evaluate(
        self, batch: list[str], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvaluationBatch[Rollout, Rollout]:
        outside = sorted(set(batch) - self.cases)
        if outside:
            raise OnodaError(f"cases outside the acceptance slice: {', '.join(outside)}")
        harness = HarnessCandidate(components=candidate)  # proposer already validated
        digest = harness.digest()
        rollouts = [self._repeated(harness, digest, case_id) for case_id in batch]
        if batch and digest in self.awaiting_valset:
            self.awaiting_valset.discard(digest)
            self.ledger.record_valset(digest, rollouts)
        return EvaluationBatch(
            outputs=list(rollouts),
            scores=[float(r.mean_score) for r in rollouts],
            trajectories=list(rollouts) if capture_traces else None,
            objective_scores=[self._objectives(r) for r in rollouts],
        )

    def _repeated(self, candidate: HarnessCandidate, digest: str, case_id: str) -> Rollout:
        """`repeats` trials of one case, as one GEPA output (the gates pool every run)."""
        first = self._trial(candidate, digest, case_id)
        rest = tuple(self._trial(candidate, digest, case_id) for _ in range(self.repeats - 1))
        return replace(first, repeats=rest) if rest else first

    def _objectives(self, r: Rollout) -> dict[str, float]:
        """Per-objective mean over the repeats; any failed run fails the example."""
        if any(v is None for v in r.outcomes):
            return _failed_objectives(self.efficiency_gate)
        per_run = [objective_scores(v, self.efficiency_gate) for v in r.outcomes if v is not None]
        return {k: sum(d[k] for d in per_run) / len(per_run) for k in per_run[0]}

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[Rollout, Rollout],
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        records = [reflective_record(r) for r in eval_batch.trajectories or []]
        return {name: [dict(rec) for rec in records] for name in components_to_update}


def _verdict_feedback(r: Rollout) -> tuple[str, set[str], set[str]]:
    """One run's feedback line, and the failing verdict words and reason classes it used."""
    if r.failed is not None:
        return f"The run failed ({r.failed}); no claims were graded.", set(), set()
    verdicts = (
        ", ".join(
            f"{t}={w}" + (f" ({r.reasons[t]})" if t in r.reasons else "")
            for t, w in sorted(r.verdicts.items())
        )
        or "none"
    )
    line = f"Claim score {r.score:f}. Evaluator verdicts per claim: {verdicts}."
    return line, {w for w in r.verdicts.values() if w != "supported"}, set(r.reasons.values())


def reflective_record(r: Rollout) -> dict[str, Any]:
    """One example for reflection: agent-visible steps, verdict words and reason classes (A53).

    With repeats, the trace shown is the lowest-scoring run's (one trace per
    case keeps the prompt within the model's context) and the feedback lists
    every run.
    """
    worst = min(r.runs, key=lambda x: (x.failed is None, x.score))
    steps = [s for s in worst.steps if s.source in AGENT_VISIBLE_SOURCES]  # re-filter
    lines, words, reasons = [], set(), set()
    for x in r.runs:
        line, w, c = _verdict_feedback(x)
        lines.append(line)
        words |= w
        reasons |= c
    if len(lines) == 1:
        feedback = lines[0]
    else:
        feedback = (
            f"{len(lines)} runs of this case; the trace is the lowest-scoring run's. "
            + " ".join(f"Run {i}: {line}" for i, line in enumerate(lines, 1))
        )
    legend = [f"{w}: {VERDICT_MEANINGS[w]}" for w in sorted(words) if w in VERDICT_MEANINGS]
    legend += [f"({c}): {REASON_CLASSES[c]}" for c in sorted(reasons)]
    if legend:
        feedback += f" Meaning: {'; '.join(legend)}."
    return {
        "Inputs": {"case": r.case_id},
        "Generated Outputs": "\n".join(f"[{s.source} {s.kind}] {s.text}" for s in steps)
        or "(no agent-visible steps)",
        "Feedback": feedback,
    }
