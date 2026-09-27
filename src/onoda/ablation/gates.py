"""M3b two-gate comparison and the S3 interaction report (SOL_PI_DELTA §5).

Pure functions over `OutcomeVector`s and `Tolerances`: no store, no files, no
Pi. `pool` sums one arm's runs into a `PooledArm`; `compare_arm` applies the
capability and efficiency gates from `ArmComparison`'s own docstring;
`non_dominated` keeps the eligible arms nothing else beats on every declared
efficiency metric; `interaction` puts S3's delta beside the sum of the S2a-d
deltas, never as that sum (A41).

Every "which way is better" judgement goes through `_gain`, so the gates and
domination cannot disagree about a metric's direction. All arithmetic is
`Decimal`; outputs are decimal text (A21).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal

from onoda.ablation.contract import (
    ACTIVATION_METRICS,
    CAPABILITY_METRICS,
    EFFICIENCY_METRICS,
    REDUCER_PROBE_METRICS,
    SINGLE_ARMS,
    ArmComparison,
    CheckResult,
    DecimalText,
    MetricCheck,
    OutcomeVector,
    Tolerances,
    Verdict,
    _Frozen,
    tolerance_for,
)

DIRECTIONS = {**CAPABILITY_METRICS, **EFFICIENCY_METRICS}

# --------------------------------------------------------------------- pool


class PooledArm(_Frozen):
    """One arm's runs, summed metric-by-metric, plus the per-arm facts the report shows.

    A metric is `None` when any run left it unreported (a missing key for
    `capability`/`activation`/`reducer_probe`, or a `None` value for
    `efficiency`). `activation_by_model[model][mechanism]` is "k/n": of the n
    runs with that model, k activated the mechanism (A16).
    """

    arm: str
    case_ids: tuple[str, ...]
    runs: int
    unsettled: tuple[str, ...]  # run ids whose spool did not end `settled`
    capability: dict[str, DecimalText | None]
    efficiency: dict[str, DecimalText | None]
    activation: dict[str, DecimalText | None]
    reducer_probe: dict[str, DecimalText | None]
    mechanism_config: tuple[str, ...]  # distinct sol-pi.json digests; "none" = SoL-Pi off
    activation_by_model: dict[str, dict[str, str]]


def _sum(names: Sequence[str], dicts: Sequence[Mapping[str, str | None]]) -> dict[str, str | None]:
    """Sum `names` across `dicts`; `None` if any dict lacks the name or reports `None`."""
    out: dict[str, str | None] = {}
    for name in names:
        values = [d.get(name) for d in dicts]
        out[name] = None if None in values else str(sum(map(Decimal, values), Decimal(0)))
    return out


def _activation_by_model(vectors: Sequence[OutcomeVector]) -> dict[str, dict[str, str]]:
    by_model: dict[str, list[OutcomeVector]] = {}
    for v in vectors:
        by_model.setdefault(v.model, []).append(v)
    return {
        model: {
            m: f"{sum(1 for v in runs if int(v.activation.get(m, '0')) > 0)}/{len(runs)}"
            for m in ACTIVATION_METRICS
        }
        for model, runs in sorted(by_model.items())
    }


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
        unsettled=tuple(v.run_id for v in vectors if v.status != "settled"),
        capability=_sum(list(CAPABILITY_METRICS), [v.capability for v in vectors]),
        efficiency=_sum(list(EFFICIENCY_METRICS), [v.efficiency for v in vectors]),
        activation=_sum(list(ACTIVATION_METRICS), [v.activation for v in vectors]),
        reducer_probe=_sum(list(REDUCER_PROBE_METRICS), [v.reducer_probe for v in vectors]),
        mechanism_config=tuple(sorted({v.mechanism_config or "none" for v in vectors})),
        activation_by_model=_activation_by_model(vectors),
    )


# --------------------------------------------------------------- compare_arm


def _gain(name: str, baseline: str | None, candidate: str | None) -> Decimal | None:
    """How much better `candidate` is than `baseline` on `name` (negative = worse)."""
    if baseline is None or candidate is None:
        return None
    delta = Decimal(candidate) - Decimal(baseline)
    return -delta if DIRECTIONS[name] == "lower" else delta


def _check(
    name: str,
    b: str | None,
    c: str | None,
    tolerance: Decimal | None,
    *,
    efficiency: bool,
    case_mismatch: bool,
) -> MetricCheck:
    """Capability passes if it worsens by at most `tolerance`; efficiency if strictly better."""
    gain = None if case_mismatch else _gain(name, b, c)
    result: CheckResult
    if gain is None or (not efficiency and tolerance is None):
        result = "unresolved"
    elif efficiency:
        result = "pass" if gain > 0 else "fail"  # a tie is not an improvement (A41)
    else:
        result = "pass" if -gain <= tolerance else "fail"
    return MetricCheck(
        metric=name,
        baseline=b,
        candidate=c,
        delta=None if gain is None else str(Decimal(c) - Decimal(b)),
        tolerance=None if efficiency or tolerance is None else str(tolerance),
        result=result,
    )


def compare_arm(
    baseline: PooledArm,
    candidate: PooledArm,
    tolerances: Tolerances | None,
    efficiency_gate: Sequence[str],
) -> ArmComparison:
    """One candidate arm against the baseline under SoL-Pi's two gates.

    A41: if the two arms did not run the same cases the same number of times,
    or either arm has a run that did not settle (cut off by a budget, a
    provider error, or never ended: its sums are partial), every check is
    `unresolved` and so is the verdict, whatever the pooled sums show.
    """
    mismatch = (
        Counter(baseline.case_ids) != Counter(candidate.case_ids)
        or bool(baseline.unsettled)
        or bool(candidate.unsettled)
    )
    capability = tuple(
        _check(
            name,
            baseline.capability.get(name),
            candidate.capability.get(name),
            tolerance_for(name, tolerances),
            efficiency=False,
            case_mismatch=mismatch,
        )
        for name in CAPABILITY_METRICS
    )
    efficiency = tuple(
        _check(
            name,
            baseline.efficiency.get(name),
            candidate.efficiency.get(name),
            None,
            efficiency=True,
            case_mismatch=mismatch,
        )
        for name in efficiency_gate
    )
    cap = {c.result for c in capability}
    capability_gate: CheckResult = (
        "fail" if "fail" in cap else "unresolved" if "unresolved" in cap else "pass"
    )
    eff = {c.result for c in efficiency}
    efficiency_gate_result: CheckResult = (
        "pass" if "pass" in eff else "unresolved" if "unresolved" in eff else "fail"
    )
    verdict: Verdict
    if "fail" in (capability_gate, efficiency_gate_result):
        verdict = "rejected"
    elif capability_gate == efficiency_gate_result == "pass":
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


def _dominates(other: PooledArm, arm: PooledArm, efficiency_gate: Sequence[str]) -> bool:
    """`other` is no worse than `arm` on every metric and better on one; unknown never dominates."""
    gains = [_gain(n, arm.efficiency.get(n), other.efficiency.get(n)) for n in efficiency_gate]
    if any(g is None for g in gains):
        return False
    return all(g >= 0 for g in gains) and any(g > 0 for g in gains)


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
    return tuple(
        arm
        for arm in eligible
        if not any(
            other != arm and _dominates(pooled[other], pooled[arm], efficiency_gate)
            for other in eligible
        )
    )


# ----------------------------------------------------------------- interaction


class InteractionTerm(_Frozen):
    """S3's delta from baseline beside the sum of the S2a-d deltas, and their difference."""

    full_delta: DecimalText | None
    sum_single_deltas: DecimalText | None
    interaction: DecimalText | None


def _delta(field_name: str, base: PooledArm, other: PooledArm, name: str) -> Decimal | None:
    b = getattr(base, field_name).get(name)
    o = getattr(other, field_name).get(name)
    return None if b is None or o is None else Decimal(o) - Decimal(b)


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
    fields = (("capability", CAPABILITY_METRICS), ("efficiency", EFFICIENCY_METRICS))
    for field_name, metrics in fields:
        for name in metrics:
            full_delta = _delta(field_name, baseline, full, name)
            singles_d = [
                _delta(field_name, baseline, singles[a], name) if a in singles else None
                for a in SINGLE_ARMS
            ]
            sum_single = None if None in singles_d else sum(singles_d, Decimal(0))
            both = full_delta is not None and sum_single is not None
            result[name] = InteractionTerm(
                full_delta=None if full_delta is None else str(full_delta),
                sum_single_deltas=None if sum_single is None else str(sum_single),
                interaction=str(full_delta - sum_single) if both else None,
            )
    return result
