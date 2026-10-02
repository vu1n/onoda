"""Head-to-head gate check of one candidate against another (A54).

`compare_candidates` is how a candidate becomes the seed: it runs `baseline`
and `candidate` on the same acceptance cases, `repeats` times each, through the
same `Runner` the optimizer uses, pools each side and hands the pair to
`gate_decision` with the operator's `GateSpec`. Nothing here is a new gate.

It runs in rounds (one case, one repeat, both candidates), so a budget stop
never leaves the two sides on different runs. A run that raises is recorded by
exception name, and any failed run leaves the check `unresolved`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from onoda.ablation.gates import pool
from onoda.adapters.pi.contract import HarnessCandidate
from onoda.errors import OnodaError
from onoda.optimize.adapter import Runner
from onoda.optimize.contract import GateSpec
from onoda.optimize.gates import gate_decision

CHECK_SCHEMA = "onoda.promotion-check/1"
SIDES = ("baseline", "candidate")


def compare_candidates(
    runner: Runner,
    *,
    check_id: str,
    baseline: HarnessCandidate,
    candidate: HarnessCandidate,
    cases: Sequence[str],
    gates: GateSpec,
    repeats: int = 1,
    max_cost_usd: Decimal | None = None,
    run_reserve_usd: Decimal | None = None,
) -> dict[str, Any]:
    """Run both candidates on `cases` and judge `candidate` against `baseline`.

    `max_cost_usd` caps the whole check; a round is skipped when `spent` plus
    two runs at `run_reserve_usd` would pass it. Returns the check body.
    """
    if repeats < 1:
        raise OnodaError("repeats must be positive")
    if (max_cost_usd is None) != (run_reserve_usd is None):
        raise OnodaError("max_cost_usd and run_reserve_usd go together")
    if baseline.digest() == candidate.digest():
        raise OnodaError("baseline and candidate are the same candidate")
    harness = dict(zip(SIDES, (baseline, candidate), strict=True))
    vectors: dict[str, list[Any]] = {s: [] for s in SIDES}
    failed: list[dict[str, str]] = []
    spent = Decimal(0)
    stopped: str | None = None
    rounds = 0
    for repeat in range(1, repeats + 1):
        for case_id in cases:
            if max_cost_usd is not None and run_reserve_usd is not None:
                if spent + 2 * run_reserve_usd > max_cost_usd:
                    stopped = f"budget: spent {spent} USD of {max_cost_usd} USD"
                    break
            for side in SIDES:
                run_id = f"{check_id}-{side}-{case_id}-r{repeat}"
                try:
                    trial = runner.run(harness[side], case_id, trial_id=run_id)
                except Exception as exc:  # recorded, never fatal; the verdict goes unresolved
                    failed.append({"run": run_id, "error": type(exc).__name__})
                    continue
                vectors[side].append(trial.outcome)
                spent += Decimal(trial.outcome.efficiency.get("cost_usd") or "0")
            rounds += 1
        if stopped:
            break

    if not all(vectors.values()):
        raise OnodaError(f"check {check_id} has no completed run on one side: {failed or stopped}")
    pooled = {s: pool(vectors[s]) for s in SIDES}
    accepted, reasons = gate_decision(pooled["baseline"], pooled["candidate"], gates)
    if failed:
        accepted = False
        reasons = (f"failed runs: {', '.join(f['run'] for f in failed)}", *reasons)
    synthetic = any(v.synthetic for vs in vectors.values() for v in vs)
    return {
        "schema": CHECK_SCHEMA,
        "check_id": check_id,
        "evidence": "synthetic" if synthetic else "live",
        "baseline": baseline.digest(),
        "candidate": candidate.digest(),
        "cases": list(cases),
        "repeats": repeats,
        "rounds": {"planned": repeats * len(cases), "completed": rounds},
        "stopped": stopped,
        "failed": failed,
        "spent_usd": str(spent),
        "arms": {s: p.model_dump(mode="json") for s, p in pooled.items()},
        "accepted": accepted,
        "reasons": list(reasons),
    }
