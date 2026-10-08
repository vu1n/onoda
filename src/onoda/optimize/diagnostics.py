"""Hill-climb diagnostics: noise, headroom, case-level intervals, leaked text.

These report; none of them changes a gate verdict. The gates stay zero
tolerance (promotion-gates); these numbers say how far to trust a verdict.

- `case_totals` sums each metric per case over its repeats, the unit of
  resampling (cases are independent, repeats of one case are not).
- `bootstrap_delta` resamples cases with replacement and reports a percentile
  interval for `candidate - baseline` on one metric. Decimal throughout; the
  RNG is seeded so a report is reproducible.
- `headroom` names the cases where every claim was supported in every run: a
  candidate cannot improve capability there, only cost.
- `leaked_text` finds case ids and long decimals (printed energies) that a
  proposal adds over its parent: the climb copying dev-set answers into the
  prompt instead of learning a rule.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

from onoda.ablation.contract import CAPABILITY_METRICS, OutcomeVector

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
# Only these may be nonzero in a saturated case: every claim supported, nothing else.
_SHORTFALL = tuple(m for m in CAPABILITY_METRICS if m != "count.supported")
# ORCA prints energies with 6+ fractional digits; 5 is enough to be an answer, not a rule.
_LONG_DECIMAL = re.compile(r"-?\d+\.\d{5,}")


def case_totals(
    vectors: Iterable[OutcomeVector], metrics: Sequence[str]
) -> dict[str, dict[str, Decimal | None]]:
    """Per case, each metric summed over its runs; None when any run left it unreported."""
    out: dict[str, dict[str, Decimal | None]] = {}
    for v in vectors:
        row = out.setdefault(v.case_id, dict.fromkeys(metrics, Decimal(0)))
        for m in metrics:
            raw = v.capability.get(m, v.efficiency.get(m))
            prev = row[m]
            row[m] = None if raw is None or prev is None else prev + Decimal(raw)
    return out


def bootstrap_delta(
    baseline: Mapping[str, Mapping[str, Decimal | None]],
    candidate: Mapping[str, Mapping[str, Decimal | None]],
    metric: str,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, str] | None:
    """95% case-bootstrap interval of `sum(candidate) - sum(baseline)` on `metric`.

    Returns None when the sides ran different cases or a total is unreported.
    `excludes_zero` false means the change is within run-to-run noise.
    """
    cases = sorted(baseline)
    if not cases or sorted(candidate) != cases:
        return None
    deltas: list[Decimal] = []
    for c in cases:
        b, k = baseline[c].get(metric), candidate[c].get(metric)
        if b is None or k is None:
            return None
        deltas.append(k - b)
    rng = random.Random(seed)
    n = len(deltas)
    sums = sorted(
        sum((deltas[rng.randrange(n)] for _ in range(n)), Decimal(0)) for _ in range(resamples)
    )
    low, high = sums[int(resamples * 0.025)], sums[int(resamples * 0.975) - 1]
    return {
        "delta": str(sum(deltas, Decimal(0))),
        "low": str(low),
        "high": str(high),
        "excludes_zero": str(low > 0 or high < 0).lower(),
    }


def intervals(
    baseline: Iterable[OutcomeVector],
    candidate: Iterable[OutcomeVector],
    metrics: Sequence[str],
    *,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, dict[str, str] | None]:
    """`bootstrap_delta` for each metric, candidate minus baseline.

    All None when a case has a different number of runs on the two sides (a
    failed run): its sums would differ by the missing sample, not by the change.
    """
    baseline, candidate = list(baseline), list(candidate)
    if Counter(v.case_id for v in baseline) != Counter(v.case_id for v in candidate):
        return dict.fromkeys(metrics)
    b, k = case_totals(baseline, metrics), case_totals(candidate, metrics)
    return {m: bootstrap_delta(b, k, m, seed=seed) for m in metrics}


def headroom(vectors: Iterable[OutcomeVector]) -> dict[str, object]:
    """Which cases are saturated (every run all-supported) and which still have room."""
    totals = case_totals(vectors, CAPABILITY_METRICS)
    saturated = sorted(
        c
        for c, row in totals.items()
        if all(row[m] == 0 for m in _SHORTFALL) and (row["count.supported"] or 0) > 0
    )
    return {
        "saturated_cases": saturated,
        "open_cases": sorted(set(totals) - set(saturated)),
        "saturated": bool(totals) and len(saturated) == len(totals),
    }


def leaked_text(
    parent: Mapping[str, str], child: Mapping[str, str], case_ids: Iterable[str]
) -> tuple[str, ...]:
    """Case ids and long decimals in `child`'s components that its parent did not contain.

    Text already in the parent is not new, so a seed that carries an example
    value does not block every descendant; only what a proposal adds counts.
    """
    ids = tuple(sorted(case_ids, key=len, reverse=True))
    found: list[str] = []
    for name, text in sorted(child.items()):
        before = parent.get(name, "")
        for cid in ids:
            if cid in text and cid not in before:
                found.append(f"{name}: case id {cid!r}")
        for num in sorted(set(_LONG_DECIMAL.findall(text))):
            if num not in before:
                found.append(f"{name}: value {num}")
    return tuple(found)
