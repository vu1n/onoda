"""Run a declared M3b ablation end to end and write its report (SOL_PI_DELTA §5).

`run_ablation` validates everything it will read before the first Pi launch:
the spec, the tolerance file, the ladder, every case (known, pinned, and in an
allowed split, A42), every script, and that none of the ablation's run ids
exist yet. It then runs every arm on every case with
`onoda.adapters.pi.run.run_case` (agent store), grades each claim with
`evaluate_claims` (evaluator store), reads each run's `OutcomeVector`, pools
and compares the arms, and writes one `onoda.ablation-report/1` object to the
evaluator store. The report cites evaluations, so it never enters the agent
store.

A report over scripted runs is labelled synthetic and carries the A40 caveat:
it exercises the harness and is not a verdict about any mechanism.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from onoda.ablation.contract import (
    ACTIVATION_METRICS,
    ALLOWED_SPLITS,
    FULL_ARM,
    REPORT_SCHEMA,
    SINGLE_ARMS,
    AblationSpec,
    OutcomeVector,
    Tolerances,
)
from onoda.ablation.gates import PooledArm, compare_arm, interaction, non_dominated, pool
from onoda.ablation.metrics import outcome_vector
from onoda.adapters.pi.contract import VARIANTS, Script
from onoda.adapters.pi.ingest import run_claim
from onoda.adapters.pi.run import run_case
from onoda.adapters.pi.task import evaluate_claims
from onoda.errors import OnodaError
from onoda.science.contract import CASES_ROOT, LoadedCase, case_manifests, load_case
from onoda.store import Store

SYNTHETIC_CAVEAT = (
    "Scripted runs (A32): every arm replays the same pre-written turns, so capability "
    "metrics exercise the harness, and tokens and cost are the script's synthetic figures. "
    "context_bytes are measured payload sizes from the real Pi + SoL-Pi process, a traffic "
    "proxy, not tokens. Short runs never accept or reject a mechanism (SOL_PI_DELTA §5). "
    "This report is not a mechanism verdict (A40)."
)


def _load_yaml(path: Path, what: str) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise OnodaError(f"cannot read {what} {path}: {exc}") from exc


def _validated(model: type, data: Any, what: str) -> Any:
    try:
        return model.model_validate(data)
    except ValueError as exc:
        raise OnodaError(f"invalid {what}: {exc}") from exc


def run_id_for(ablation_id: str, arm: str, case_id: str) -> str:
    return f"{ablation_id}-{arm}-{case_id}"


def _activation_by_model(vectors: Sequence[OutcomeVector]) -> dict[str, dict[str, str]]:
    """model -> mechanism -> "k/n": of n runs with that model, k activated the mechanism (A16)."""
    by_model: dict[str, list[OutcomeVector]] = {}
    for v in vectors:
        by_model.setdefault(v.model, []).append(v)
    return {
        model: {
            m: f"{sum(1 for v in runs if int(v.activation[m]) > 0)}/{len(runs)}"
            for m in ACTIVATION_METRICS
        }
        for model, runs in sorted(by_model.items())
    }


def run_ablation(
    spec_path: str | Path,
    *,
    store: Store,
    evaluator_store: Store,
    work_dir: str | Path,
    cases_root: Path = CASES_ROOT,
    timeout_s: float = 300,
) -> dict[str, Any]:
    """Run every arm on every case, then write and return the report (with its digest)."""
    spec_path = Path(spec_path)
    base = spec_path.parent
    spec: AblationSpec = _validated(AblationSpec, _load_yaml(spec_path, "spec"), "ablation spec")
    tolerances: Tolerances | None = None
    if spec.tolerances is not None:
        tolerances = _validated(
            Tolerances, _load_yaml(base / spec.tolerances, "tolerances"), "tolerances"
        )
    ladder = (base / spec.ladder).read_bytes() if spec.ladder is not None else None

    manifests = case_manifests(cases_root)
    cases: dict[str, tuple[LoadedCase, Script]] = {}
    for c in spec.cases:
        if c.case_id not in manifests:
            raise OnodaError(f"unknown case {c.case_id!r}; see `onoda case list`")
        case = load_case(manifests[c.case_id])
        if case.manifest.split not in ALLOWED_SPLITS:
            raise OnodaError(
                f"case {c.case_id} is in split {case.manifest.split!r}; an ablation reads only "
                f"{', '.join(ALLOWED_SPLITS)} cases (A42)"
            )
        try:
            script = Script.model_validate_json((base / c.script).read_bytes())
        except (OSError, ValueError) as exc:
            raise OnodaError(f"invalid script for case {c.case_id}: {exc}") from exc
        cases[c.case_id] = (case, script)

    run_ids = [run_id_for(spec.ablation_id, a, c) for a in spec.arms for c in cases]
    if taken := [r for r in run_ids if store.has_record("run", r)]:
        raise OnodaError(f"ablation {spec.ablation_id} already has runs: {', '.join(taken)}")

    work_dir = Path(work_dir)
    vectors: dict[str, list[OutcomeVector]] = {arm: [] for arm in spec.arms}
    for arm in spec.arms:
        needs_ladder = VARIANTS[arm].ladder
        for case_id, (case, script) in cases.items():
            run_id = run_id_for(spec.ablation_id, arm, case_id)
            run_case(
                store,
                case,
                run_id=run_id,
                variant_id=arm,
                work_dir=work_dir / arm / case_id,
                script=script,
                ladder=ladder if needs_ladder else None,
                synthetic_ladder=spec.synthetic_ladder and needs_ladder,
                max_requests=spec.max_requests,
                timeout_s=timeout_s,
            )
            claims = [e for e in store.events(run_id) if e.type == "pi.claim"]
            evaluation = evaluate_claims(
                evaluator_store,
                run_id=run_id,
                case=case,
                claim=run_claim(store, run_id),
                claim_ref=claims[-1].payload_refs["claim"] if claims else None,
            )
            vectors[arm].append(outcome_vector(store, evaluation, arm=arm))

    pooled: dict[str, PooledArm] = {arm: pool(vs) for arm, vs in vectors.items()}
    baseline = pooled[spec.baseline]
    comparisons = [
        compare_arm(baseline, pooled[arm], tolerances, spec.efficiency_gate)
        for arm in spec.arms
        if arm != spec.baseline
    ]
    interaction_terms = None
    if FULL_ARM in pooled and spec.baseline not in (FULL_ARM, *SINGLE_ARMS):
        singles = {a: pooled[a] for a in SINGLE_ARMS if a in pooled}
        interaction_terms = {
            m: asdict(t) for m, t in interaction(baseline, singles, pooled[FULL_ARM]).items()
        }

    all_vectors = [v for vs in vectors.values() for v in vs]
    synthetic = any(v.synthetic for v in all_vectors)
    spec_ref = evaluator_store.put_json(
        spec.model_dump(mode="json", by_alias=True),
        visibility="evaluator",
        labels=["ablation-spec"],
    ).digest
    tolerances_ref = (
        evaluator_store.put_json(
            tolerances.model_dump(mode="json", by_alias=True),
            visibility="evaluator",
            labels=["ablation-tolerances"],
        ).digest
        if tolerances is not None
        else None
    )
    report = {
        "schema": REPORT_SCHEMA,
        "ablation_id": spec.ablation_id,
        "spec": spec_ref,
        "tolerances": tolerances_ref,
        "evidence": "synthetic" if synthetic else "measured",
        "caveat": SYNTHETIC_CAVEAT if synthetic else None,
        "baseline": spec.baseline,
        "efficiency_gate": list(spec.efficiency_gate),
        "runs": [v.model_dump(mode="json") for v in all_vectors],
        "arms": {arm: asdict(p) for arm, p in pooled.items()},
        "mechanism_config": {
            arm: sorted({v.mechanism_config or "none" for v in vs}) for arm, vs in vectors.items()
        },
        "activation_by_model": {arm: _activation_by_model(vs) for arm, vs in vectors.items()},
        "comparisons": [c.model_dump(mode="json") for c in comparisons],
        "non_dominated": list(non_dominated(comparisons, pooled, spec.efficiency_gate)),
        "interaction": interaction_terms,
    }
    digest = evaluator_store.put_json(
        report,
        visibility="evaluator",
        labels=["ablation-report", *(["synthetic"] if synthetic else [])],
    ).digest
    return {"report": digest, **report}
