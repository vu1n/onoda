"""M3b two-gate comparison and the S3 interaction report (SOL_PI_DELTA §5).

Pure functions over `OutcomeVector`s and `Tolerances`: no store, no files, no
Pi. `pool` sums one arm's runs into a `PooledArm`; `compare_arm` applies the
capability and efficiency gates from `ArmComparison`'s own docstring;
`non_dominated` keeps the eligible arms nothing else beats on every declared
efficiency metric; `interaction` puts S3's delta beside the sum of the S2a-d
deltas, never as that sum (A41).

All arithmetic is `Decimal`; outputs are decimal text (A21).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from onoda.ablation.contract import (
    ACTIVATION_METRICS,
    CAPABILITY_METRICS,
    EFFICIENCY_METRICS,
    FALSE_SUCCESS,
    REDUCER_PROBE_METRICS,
    SINGLE_ARMS,
    ArmComparison,
    CheckResult,
    MetricCheck,
    OutcomeVector,
    Tolerances,
    Verdict,
)

# --------------------------------------------------------------------- pool


@dataclass(frozen=True)
class PooledArm:
    """One arm's runs, summed metric-by-metric.

    A metric is `None` when any run left it unreported (a missing key for
    `capability`/`activation`/`reducer_probe`, or a `None` value for
    `efficiency`).
    """

    arm: str
    case_ids: tuple[str, ...]
    runs: int
    capability: dict[str, str | None]
    efficiency: dict[str, str | None]
    activation: dict[str, str | None]
    reducer_probe: dict[str, str | None]


def _sum(names: Sequence[str], dicts: Sequence[Mapping[str, str | None]]) -> dict[str, str | None]:
    """Sum `names` across `dicts`; `None` if any dict lacks the name or reports `None`."""
    out: dict[str, str | None] = {}
    for name in names:
        total = Decimal(0)
        reported = True
        for d in dicts:
            v = d.get(name)
            if v is None:
                reported = False
                break
            total += Decimal(v)
        out[name] = str(total) if reported else None
    return out


def pool(vectors: Sequence[OutcomeVector]) -> PooledArm:
    """Sum one arm's runs metric-by-metric. `vectors` must all share an arm."""
    if not vectors:
        raise ValueError("pool needs at least one run")
    arm = vectors[0].arm
    for v in vectors:
        if v.arm != arm:
            raise ValueError(f"vectors span multiple arms: {arm!r} and {v.arm!r}")
    return PooledArm(
        arm=arm,
        case_ids=tuple(v.case_id for v in vectors),
        runs=len(vectors),
        capability=_sum(list(CAPABILITY_METRICS), [v.capability for v in vectors]),
        efficiency=_sum(list(EFFICIENCY_METRICS), [v.efficiency for v in vectors]),
        activation=_sum(list(ACTIVATION_METRICS), [v.activation for v in vectors]),
        reducer_probe=_sum(list(REDUCER_PROBE_METRICS), [v.reducer_probe for v in vectors]),
    )


# --------------------------------------------------------------- compare_arm


def _capability_tolerance(name: str, tolerances: Tolerances | None) -> Decimal | None:
    if tolerances is not None:
        return tolerances.allowed(name)
    return Decimal(0) if name == FALSE_SUCCESS else None


def _capability_check(
    name: str,
    baseline: PooledArm,
    candidate: PooledArm,
    tolerances: Tolerances | None,
    case_mismatch: bool,
) -> MetricCheck:
    b = baseline.capability.get(name)
    c = candidate.capability.get(name)
    tol = _capability_tolerance(name, tolerances)
    result: CheckResult
    delta: Decimal | None
    if case_mismatch or b is None or c is None:
        result = "unresolved"
        delta = None
    else:
        bd, cd = Decimal(b), Decimal(c)
        delta = cd - bd
        direction = CAPABILITY_METRICS[name]
        worsening = delta if direction == "lower" else -delta
        if tol is None:
            result = "unresolved"
        elif worsening <= tol:
            result = "pass"
        else:
            result = "fail"
    return MetricCheck(
        metric=name,
        baseline=b,
        candidate=c,
        delta=str(delta) if delta is not None else None,
        tolerance=str(tol) if tol is not None else None,
        result=result,
    )


def _efficiency_check(
    name: str,
    baseline: PooledArm,
    candidate: PooledArm,
    case_mismatch: bool,
) -> MetricCheck:
    b = baseline.efficiency.get(name)
    c = candidate.efficiency.get(name)
    result: CheckResult
    delta: Decimal | None
    if case_mismatch or b is None or c is None:
        result = "unresolved"
        delta = None
    else:
        bd, cd = Decimal(b), Decimal(c)
        delta = cd - bd
        direction = EFFICIENCY_METRICS[name]
        better = cd < bd if direction == "lower" else cd > bd
        result = "pass" if better else "fail"
    return MetricCheck(
        metric=name,
        baseline=b,
        candidate=c,
        delta=str(delta) if delta is not None else None,
        tolerance=None,
        result=result,
    )


def _capability_gate(checks: Sequence[MetricCheck]) -> CheckResult:
    results = {c.result for c in checks}
    if "fail" in results:
        return "fail"
    if "unresolved" in results:
        return "unresolved"
    return "pass"


def _efficiency_gate(checks: Sequence[MetricCheck]) -> CheckResult:
    results = {c.result for c in checks}
    if "pass" in results:
        return "pass"
    if "unresolved" in results:
        return "unresolved"
    return "fail"


def compare_arm(
    baseline: PooledArm,
    candidate: PooledArm,
    tolerances: Tolerances | None,
    efficiency_gate: Sequence[str],
) -> ArmComparison:
    """One candidate arm against the baseline under SoL-Pi's two gates.

    A41: if the two arms' case sets differ, a run is missing on one side
    for at least one case, so every check is `unresolved` and the verdict is
    `unresolved` regardless of what the pooled sums happen to show.
    """
    case_mismatch = set(baseline.case_ids) != set(candidate.case_ids)
    capability = tuple(
        _capability_check(name, baseline, candidate, tolerances, case_mismatch)
        for name in CAPABILITY_METRICS
    )
    efficiency = tuple(
        _efficiency_check(name, baseline, candidate, case_mismatch) for name in efficiency_gate
    )
    capability_gate = _capability_gate(capability)
    efficiency_gate_result = _efficiency_gate(efficiency)
    verdict: Verdict
    if capability_gate == "fail" or efficiency_gate_result == "fail":
        verdict = "rejected"
    elif capability_gate == "pass" and efficiency_gate_result == "pass":
        verdict = "eligible"
    else:
        verdict = "unresolved"
    return ArmComparison(
        arm=candidate.arm,
        baseline=baseline.arm,
        capability=capability,
        efficiency=efficiency,
        capability_gate=capability_gate,
        efficiency_gate=efficiency_gate_result,
        verdict=verdict,
    )


# ------------------------------------------------------------- non_dominated


def _dominates(
    candidate_vals: Mapping[str, str | None],
    other_vals: Mapping[str, str | None],
    efficiency_gate: Sequence[str],
) -> bool:
    """True if `other` dominates `candidate`: <= on every metric, < on at least one."""
    at_least_as_good_on_all = True
    strictly_better_on_one = False
    for name in efficiency_gate:
        cv = candidate_vals.get(name)
        ov = other_vals.get(name)
        if cv is None or ov is None:
            return False  # can't establish domination without both values
        cd, od = Decimal(cv), Decimal(ov)
        direction = EFFICIENCY_METRICS[name]
        not_worse = od <= cd if direction == "lower" else od >= cd
        better = od < cd if direction == "lower" else od > cd
        if not not_worse:
            at_least_as_good_on_all = False
            break
        if better:
            strictly_better_on_one = True
    return at_least_as_good_on_all and strictly_better_on_one


def non_dominated(
    comparisons: Sequence[ArmComparison],
    pooled: Mapping[str, PooledArm],
    efficiency_gate: Sequence[str],
) -> tuple[str, ...]:
    """Eligible arm ids that no other eligible arm beats on every declared metric.

    Order follows `comparisons`. `pooled` maps arm id to its `PooledArm`
    (candidates named in `comparisons` at least).
    """
    eligible = [c.arm for c in comparisons if c.verdict == "eligible"]
    kept = []
    for arm in eligible:
        beaten = any(
            other != arm
            and _dominates(pooled[arm].efficiency, pooled[other].efficiency, efficiency_gate)
            for other in eligible
        )
        if not beaten:
            kept.append(arm)
    return tuple(kept)


# ----------------------------------------------------------------- interaction


@dataclass(frozen=True)
class InteractionTerm:
    """S3's delta from baseline beside the sum of the S2a-d deltas, and their difference."""

    full_delta: str | None
    sum_single_deltas: str | None
    interaction: str | None


def _delta(field_name: str, base: PooledArm, other: PooledArm, name: str) -> Decimal | None:
    b = getattr(base, field_name).get(name)
    o = getattr(other, field_name).get(name)
    if b is None or o is None:
        return None
    return Decimal(o) - Decimal(b)


def interaction(
    baseline: PooledArm,
    singles: Mapping[str, PooledArm],
    full: PooledArm,
) -> dict[str, InteractionTerm]:
    """Per capability and efficiency metric: S3's delta, the sum of S2a-d's, and their difference.

    `None` on a term whose own inputs are missing: `full_delta` needs the
    baseline and `full` to both report the metric; `sum_single_deltas` needs
    the baseline and every one of `SINGLE_ARMS` present in `singles` to
    report it; `interaction` needs both of those (A41: never report S3's
    delta as though it were the S2a-d sum).
    """
    result: dict[str, InteractionTerm] = {}
    fields = [("capability", CAPABILITY_METRICS), ("efficiency", EFFICIENCY_METRICS)]
    for field_name, metrics in fields:
        for name in metrics:
            full_delta = _delta(field_name, baseline, full, name)
            sum_single: Decimal | None = Decimal(0)
            for arm in SINGLE_ARMS:
                single = singles.get(arm)
                if single is None:
                    sum_single = None
                    break
                d = _delta(field_name, baseline, single, name)
                if d is None:
                    sum_single = None
                    break
                sum_single += d
            interaction_val = (
                None if full_delta is None or sum_single is None else full_delta - sum_single
            )
            result[name] = InteractionTerm(
                full_delta=str(full_delta) if full_delta is not None else None,
                sum_single_deltas=str(sum_single) if sum_single is not None else None,
                interaction=str(interaction_val) if interaction_val is not None else None,
            )
    return result
