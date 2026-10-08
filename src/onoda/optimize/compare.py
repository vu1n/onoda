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
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from onoda.ablation.contract import CAPABILITY_METRICS, OutcomeVector
from onoda.ablation.gates import pool
from onoda.adapters.pi.contract import HarnessCandidate
from onoda.errors import OnodaError
from onoda.optimize.adapter import Runner
from onoda.optimize.contract import GateSpec
from onoda.optimize.diagnostics import headroom, intervals
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
    two runs at `run_reserve_usd` would pass it. Under a cap, a failed run is
    charged its reserve and a run that reports no cost stops the check and
    rejects the candidate, as a live ablation does. Returns the check body.
    """
    if baseline.digest() == candidate.digest():
        raise OnodaError("baseline and candidate are the same candidate")
    sides = _run_sides(
        runner,
        check_id,
        dict(zip(SIDES, (baseline, candidate), strict=True)),
        cases,
        repeats,
        max_cost_usd,
        run_reserve_usd,
    )
    vectors, failed, unpriced = sides.vectors, sides.failed, sides.unpriced
    if not all(vectors.values()):
        raise OnodaError(
            f"check {check_id} has no completed run on one side: {failed or sides.stopped}"
        )
    pooled = {s: pool(vectors[s]) for s in SIDES}
    accepted, reasons = gate_decision(pooled["baseline"], pooled["candidate"], gates)
    if failed:
        accepted = False
        reasons = (f"failed runs: {', '.join(f['run'] for f in failed)}", *reasons)
    if unpriced:
        accepted = False
        reasons = (f"run {unpriced} reported no cost under a spend cap", *reasons)
    synthetic = any(v.synthetic for vs in vectors.values() for v in vs)
    return {
        "schema": CHECK_SCHEMA,
        "check_id": check_id,
        "evidence": "synthetic" if synthetic else "live",
        "baseline": baseline.digest(),
        "candidate": candidate.digest(),
        "cases": list(cases),
        "repeats": repeats,
        "rounds": {"planned": repeats * len(cases), "completed": sides.rounds},
        "stopped": sides.stopped,
        "failed": failed,
        "spent_usd": str(sides.spent),
        "arms": {s: p.model_dump(mode="json") for s, p in pooled.items()},
        "accepted": accepted,
        "reasons": list(reasons),
        # Advisory (diagnostics.py): never part of the verdict above.
        "intervals": intervals(vectors["baseline"], vectors["candidate"], _metrics(gates)),
        "headroom": headroom(vectors["baseline"]),
    }


@dataclass
class _Sides:
    vectors: dict[str, list[OutcomeVector]]
    failed: list[dict[str, str]] = field(default_factory=list)
    spent: Decimal = Decimal(0)
    stopped: str | None = None
    unpriced: str | None = None
    rounds: int = 0


def _metrics(gates: GateSpec) -> tuple[str, ...]:
    return (*CAPABILITY_METRICS, *gates.efficiency_gate)


def _run_sides(
    runner: Runner,
    check_id: str,
    harness: dict[str, HarnessCandidate],
    cases: Sequence[str],
    repeats: int,
    max_cost_usd: Decimal | None,
    run_reserve_usd: Decimal | None,
) -> _Sides:
    """Run every side on every case, in rounds (one case, one repeat, all sides)."""
    if repeats < 1:
        raise OnodaError("repeats must be positive")
    if (max_cost_usd is None) != (run_reserve_usd is None):
        raise OnodaError("max_cost_usd and run_reserve_usd go together")
    out = _Sides(vectors={s: [] for s in harness})
    for repeat in range(1, repeats + 1):
        for case_id in cases:
            if max_cost_usd is not None and run_reserve_usd is not None:
                if out.spent + len(harness) * run_reserve_usd > max_cost_usd:
                    out.stopped = f"budget: spent {out.spent} USD of {max_cost_usd} USD"
                    break
            for side, cand in harness.items():
                run_id = f"{check_id}-{side}-{case_id}-r{repeat}"
                try:
                    trial = runner.run(cand, case_id, trial_id=run_id)
                except Exception as exc:  # recorded, never fatal; the verdict goes unresolved
                    out.failed.append({"run": run_id, "error": type(exc).__name__})
                    if run_reserve_usd is not None:  # it may have spent up to its reserve
                        out.spent += run_reserve_usd
                    continue
                out.vectors[side].append(trial.outcome)
                cost = trial.outcome.efficiency.get("cost_usd")
                if cost is not None:
                    out.spent += Decimal(cost)
                elif max_cost_usd is not None:  # an unknown cost cannot be held to the cap
                    out.unpriced = run_id
            out.rounds += 1
            if out.unpriced:
                out.stopped = f"run {out.unpriced} reported no cost; the check stops"
                break
        if out.stopped:
            break
    return out


NOISE_SCHEMA = "onoda.noise-check/1"
NOISE_SIDES = ("a", "b")


def noise_check(
    runner: Runner,
    *,
    check_id: str,
    candidate: HarnessCandidate,
    cases: Sequence[str],
    gates: GateSpec,
    repeats: int = 1,
    max_cost_usd: Decimal | None = None,
    run_reserve_usd: Decimal | None = None,
) -> dict[str, Any]:
    """Run one candidate twice as two sides (A/A) and report how far they disagree.

    Any capability difference between the sides is run-to-run noise; a gate
    that rejects `b` against `a` would reject a real change the same way. Run
    this before a search, at the search's repeat count, to see whether that
    count can resolve the gains being climbed for. Report only: it sets no
    tolerance and promotes nothing.
    """
    sides = _run_sides(
        runner,
        check_id,
        dict.fromkeys(NOISE_SIDES, candidate),
        cases,
        repeats,
        max_cost_usd,
        run_reserve_usd,
    )
    if not all(sides.vectors.values()):
        raise OnodaError(
            f"check {check_id} has no completed run on one side: {sides.failed or sides.stopped}"
        )
    a, b = (sides.vectors[s] for s in NOISE_SIDES)
    pooled = {s: pool(sides.vectors[s]) for s in NOISE_SIDES}
    gate_passes, reasons = gate_decision(pooled["a"], pooled["b"], gates)
    spread = intervals(a, b, _metrics(gates))
    noisy = sorted(
        m for m in CAPABILITY_METRICS if (iv := spread.get(m)) is not None and iv["delta"] != "0"
    )
    return {
        "schema": NOISE_SCHEMA,
        "check_id": check_id,
        "evidence": "synthetic" if any(v.synthetic for v in (*a, *b)) else "live",
        "candidate": candidate.digest(),
        "cases": list(cases),
        "repeats": repeats,
        "rounds": {"planned": repeats * len(cases), "completed": sides.rounds},
        "stopped": sides.stopped,
        "failed": sides.failed,
        "spent_usd": str(sides.spent),
        "arms": {s: p.model_dump(mode="json") for s, p in pooled.items()},
        "intervals": spread,
        "headroom": headroom((*a, *b)),
        # Capability metrics that moved with nothing changed: under zero
        # tolerance, a one-run gate on these is reading noise.
        "noisy_capability": noisy,
        "gate_on_self": {"passes": gate_passes, "reasons": list(reasons)},
    }
