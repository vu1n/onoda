"""SoL-Pi's promotion gates as a GEPA acceptance criterion (SOL_PI_DELTA §5).

GEPA's default criterion compares summed minibatch scores. SoL-Pi promotes on
something stricter and multi-dimensional, so `SolPiGates` replaces it: GEPA
evaluates the parent and the proposal on the same minibatch (`eval_before`,
`eval_after`), the adapter carries each trial's ablation `OutcomeVector`
through the outputs, each side is `pool`ed into a `PooledArm`, and
`gate_decision` hands the pair to M3b's `compare_arm` with the `GateSpec`'s
tolerances and efficiency gate. There is one implementation of the two gates
in onoda, and it lives in `onoda.ablation.gates`.

A proposal is accepted only on an `eligible` verdict. `unresolved` (an
undeclared tolerance, an unsettled run, parent and child on different cases,
an unreported efficiency metric with no other gain) is not acceptance. A
trial that failed has no vector, and a minibatch with one is rejected:
nothing can be compared. GEPA's float scores play no part in the decision.

`non_dominated` orders pool members' slice totals on every capability metric
plus the spec's efficiency gate, with M3b's directions (`gain`); an
unreported value never dominates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from onoda.ablation.contract import CAPABILITY_METRICS, FALSE_SUCCESS, MetricCheck, OutcomeVector
from onoda.ablation.gates import PooledArm, compare_arm, gain, pool
from onoda.optimize.contract import GateSpec


def _capability_reason(c: MetricCheck) -> str:
    if c.result == "unresolved":
        return f"capability unresolved: {c.metric} {c.baseline} -> {c.candidate}"
    if c.metric == FALSE_SUCCESS:
        return f"false-success increase: {c.baseline} -> {c.candidate}"
    return (
        f"capability regression: {c.metric} {c.baseline} -> {c.candidate} "
        f"exceeds tolerance {c.tolerance}"
    )


def gate_decision(
    parent: PooledArm, child: PooledArm, spec: GateSpec
) -> tuple[bool, tuple[str, ...]]:
    """Judge `child` against `parent` (pooled over the same cases) with `compare_arm`.

    Returns (accepted, reasons). accepted iff the verdict is `eligible`. On
    rejection or `unresolved` the reasons name every failing or unresolved
    check; on acceptance they name the efficiency metrics that improved.
    """
    cmp = compare_arm(parent, child, spec.tolerances, spec.efficiency_gate)
    gains = tuple(
        f"efficiency gain: {c.metric} {c.baseline} -> {c.candidate}"
        for c in cmp.efficiency
        if c.result == "pass"
    )
    if cmp.verdict == "eligible":
        return True, gains
    reasons: list[str] = []
    if sorted(parent.case_ids) != sorted(child.case_ids):
        reasons.append("parent and proposal were not run on the same cases")
    unsettled = parent.unsettled + child.unsettled
    if unsettled:
        reasons.append(f"unsettled runs: {', '.join(unsettled)}")
    reasons += [_capability_reason(c) for c in cmp.capability if c.result != "pass"]
    if cmp.efficiency_gate == "fail":
        reasons.append(
            "no efficiency gain: no gated efficiency metric "
            f"({', '.join(spec.efficiency_gate)}) strictly better than the parent"
        )
    elif cmp.efficiency_gate == "unresolved":
        reasons += [
            f"efficiency unresolved: {c.metric} {c.baseline} -> {c.candidate}"
            for c in cmp.efficiency
            if c.result == "unresolved"
        ]
    return False, tuple(reasons)


def _dominates(a: PooledArm, b: PooledArm, spec: GateSpec) -> bool:
    """`a` is no worse than `b` on every axis and better on one. Unknown never dominates."""
    gains = [gain(m, b.capability.get(m), a.capability.get(m)) for m in CAPABILITY_METRICS]
    gains += [gain(m, b.efficiency.get(m), a.efficiency.get(m)) for m in spec.efficiency_gate]
    if any(g is None for g in gains):
        return False
    return all(g >= 0 for g in gains) and any(g > 0 for g in gains)


def non_dominated(pooled: Mapping[str, PooledArm], spec: GateSpec) -> list[str]:
    """Keys (candidate digests) of the totals no other total dominates, in input order.

    Ties are all kept.
    """
    return [
        k
        for k, v in pooled.items()
        if not any(_dominates(w, v, spec) for j, w in pooled.items() if j != k)
    ]


class CarriesOutcome(Protocol):
    """What the gates need from a GEPA output: the trial's vector, or None if it failed."""

    @property
    def outcome(self) -> OutcomeVector | None: ...


# (proposal, parent total, child total, accepted, reasons); totals are None
# when a failed trial left nothing to compare.
DecisionObserver = Callable[[Any, PooledArm | None, PooledArm | None, bool, tuple[str, ...]], None]


def pool_outputs(outputs: Sequence[CarriesOutcome] | None) -> PooledArm | None:
    """The pooled total of a batch's trials, or None if any failed or they span arms."""
    if not outputs:
        return None
    vectors = [o.outcome for o in outputs]
    if any(v is None for v in vectors):
        return None
    try:
        return pool([v for v in vectors if v is not None])
    except ValueError:  # trials recorded under different variants: not one arm
        return None


class SolPiGates:
    """GEPA `AcceptanceCriterion` enforcing `gate_decision` on minibatch totals.

    `on_decision` sees every judgement exactly once per proposal (GEPA
    memoizes the criterion per batch); the optimizer loop uses it to record
    the decision before GEPA acts on it.
    """

    def __init__(self, spec: GateSpec, *, on_decision: DecisionObserver | None = None):
        self.spec = spec
        self.on_decision = on_decision
        self._reasons: dict[int, tuple[str, ...]] = {}

    def judge(self, proposal: Any) -> tuple[bool, tuple[str, ...]]:
        before, after = proposal.eval_before, proposal.eval_after
        parent = pool_outputs(before.outputs if before is not None else None)
        child = pool_outputs(after.outputs if after is not None else None)
        if parent is None or child is None:
            accepted, reasons = (
                False,
                ("a minibatch trial failed or trials span variants; nothing to compare",),
            )
        else:
            accepted, reasons = gate_decision(parent, child, self.spec)
        if self.on_decision is not None:
            self.on_decision(proposal, parent, child, accepted, reasons)
        return accepted, reasons

    def should_accept(self, proposal: Any, state: Any) -> bool:
        accepted, reasons = self.judge(proposal)
        self._reasons[id(proposal)] = reasons
        return accepted

    def reject_reason(self, proposal: Any, state: Any) -> str:
        """GEPA logs this for rejected proposals."""
        return "SoL-Pi gates: " + "; ".join(self._reasons.get(id(proposal), ("not judged",)))
