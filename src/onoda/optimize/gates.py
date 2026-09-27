"""SoL-Pi's promotion gates as a GEPA acceptance criterion (SOL_PI_DELTA §5).

GEPA's default criterion compares summed minibatch scores. SoL-Pi promotes on
something stricter and multi-dimensional, so `SolPiGates` replaces it: GEPA
evaluates the parent and the proposal on the same minibatch (`eval_before`,
`eval_after`), the adapter carries each trial's `OutcomeVector` through the
outputs, and the gates compare the two slice totals:

- every capability metric within the `GateSpec` tolerance of the parent;
- no increase at all in false-success claims (tolerance fixed at 0);
- at least one efficiency metric strictly better (lower).

All comparisons use `Decimal` on the vectors' decimal text; GEPA's float
scores play no part in the decision. A trial that failed has no vector, and a
proposal whose minibatch has one is rejected: nothing can be compared.

`non_dominated` is the same ordering used for the final report: capability
higher, false success lower, efficiency lower, each metric its own axis.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, Protocol

from onoda.optimize.contract import (
    CAPABILITY_METRICS,
    EFFICIENCY_METRICS,
    GateSpec,
    OutcomeVector,
)


def gate_decision(
    parent: OutcomeVector, child: OutcomeVector, spec: GateSpec
) -> tuple[bool, tuple[str, ...]]:
    """Judge `child` against `parent` (slice totals over the same cases).

    Returns (accepted, reasons). On rejection the reasons name every failed
    gate; on acceptance they name the efficiency metrics that improved.
    """
    failures: list[str] = []
    for m in CAPABILITY_METRICS:
        p, c = Decimal(parent.capability[m]), Decimal(child.capability[m])
        tol = spec.capability_tolerance[m]
        assert tol is not None  # GateSpec refuses unset tolerances (A43)
        if c < p - Decimal(tol):
            failures.append(
                f"capability regression: {m} {parent.capability[m]} -> "
                f"{child.capability[m]} exceeds tolerance {tol}"
            )
    if child.false_success_claims > parent.false_success_claims + spec.false_success_tolerance:
        failures.append(
            f"false-success increase: {parent.false_success_claims} -> {child.false_success_claims}"
        )
    gains = [
        f"efficiency gain: {m} {parent.efficiency[m]} -> {child.efficiency[m]}"
        for m in EFFICIENCY_METRICS
        if Decimal(child.efficiency[m]) < Decimal(parent.efficiency[m])
    ]
    if not gains:
        failures.append("no efficiency gain: no efficiency metric strictly lower than the parent")
    if failures:
        return False, tuple(failures)
    return True, tuple(gains)


def _axes(v: OutcomeVector) -> tuple[Decimal, ...]:
    """Every axis oriented so that larger is better."""
    return (
        *(Decimal(v.capability[m]) for m in CAPABILITY_METRICS),
        Decimal(-v.false_success_claims),
        *(-Decimal(v.efficiency[m]) for m in EFFICIENCY_METRICS),
    )


def dominates(a: OutcomeVector, b: OutcomeVector) -> bool:
    """`a` is at least as good as `b` on every axis and strictly better on one."""
    xa, xb = _axes(a), _axes(b)
    return all(x >= y for x, y in zip(xa, xb, strict=True)) and xa != xb


def non_dominated(vectors: Sequence[OutcomeVector]) -> list[OutcomeVector]:
    """The vectors no other vector dominates, in input order (ties are all kept)."""
    return [v for v in vectors if not any(dominates(w, v) for w in vectors if w is not v)]


class CarriesOutcome(Protocol):
    """What the gates need from a GEPA output: the trial's vector, or None if it failed."""

    @property
    def outcome(self) -> OutcomeVector | None: ...


# (proposal, parent total, child total, accepted, reasons); totals are None
# when a failed trial left nothing to compare.
DecisionObserver = Callable[
    [Any, OutcomeVector | None, OutcomeVector | None, bool, tuple[str, ...]], None
]


def _total(outputs: Sequence[CarriesOutcome] | None) -> OutcomeVector | None:
    if not outputs:
        return None
    vectors = [o.outcome for o in outputs]
    if any(v is None for v in vectors):
        return None
    return OutcomeVector.total([v for v in vectors if v is not None])


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
        parent = _total(before.outputs if before is not None else None)
        child = _total(after.outputs if after is not None else None)
        if parent is None or child is None:
            accepted, reasons = False, ("a minibatch trial failed; nothing to compare",)
        elif sorted(parent.cases) != sorted(child.cases):
            accepted, reasons = False, ("parent and proposal were not run on the same cases",)
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
