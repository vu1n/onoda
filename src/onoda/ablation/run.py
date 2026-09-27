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

from pathlib import Path
from typing import Any

import yaml

from onoda.ablation.contract import (
    ALLOWED_SPLITS,
    FULL_ARM,
    REPORT_SCHEMA,
    SINGLE_ARMS,
    AblationSpec,
    OutcomeVector,
    Tolerances,
)
from onoda.ablation.gates import compare_arm, interaction, non_dominated, pool
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


def _under(base: Path, rel: str, what: str) -> Path:
    """`rel` resolved inside the spec's directory: a spec cannot point the agent at other files."""
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()):
        raise OnodaError(f"{what} {rel!r} is outside the spec's directory")
    return path


def _read(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise OnodaError(f"cannot read {what} {path}: {exc}") from exc


def _validated(model: type, data: Any, what: str) -> Any:
    try:
        return model.model_validate(data)
    except ValueError as exc:
        raise OnodaError(f"invalid {what}: {exc}") from exc


def run_id_for(ablation_id: str, arm: str, case_id: str) -> str:
    return f"{ablation_id}-{arm}-{case_id}"


def _prepare(
    spec_path: Path, cases_root: Path
) -> tuple[AblationSpec, Tolerances | None, bytes | None, dict[str, tuple[LoadedCase, Script]]]:
    """Everything an ablation reads, validated before the first Pi launch."""
    base = spec_path.parent
    spec: AblationSpec = _validated(AblationSpec, _load_yaml(spec_path, "spec"), "ablation spec")
    tolerances: Tolerances | None = None
    if spec.tolerances is not None:
        path = _under(base, spec.tolerances, "tolerances")
        tolerances = _validated(Tolerances, _load_yaml(path, "tolerances"), "tolerances")
    ladder = None
    if spec.ladder is not None:
        ladder = _read(_under(base, spec.ladder, "ladder"), "ladder")

    manifests = case_manifests(cases_root)
    cases: dict[str, tuple[LoadedCase, Script]] = {}
    for c in spec.cases:
        if c.case_id not in manifests:
            raise OnodaError(f"unknown case {c.case_id!r}; see `onoda case list`")
        # The split comes from the manifest alone: a held-out case's output bytes
        # are never opened, not even to verify their digest (A42).
        split = (_load_yaml(manifests[c.case_id], "case manifest") or {}).get("split")
        if split not in ALLOWED_SPLITS:
            raise OnodaError(
                f"case {c.case_id} is in split {split!r}; an ablation reads only "
                f"{', '.join(ALLOWED_SPLITS)} cases (A42)"
            )
        case = load_case(manifests[c.case_id])
        script_bytes = _read(_under(base, c.script, "script"), f"script for case {c.case_id}")
        try:
            script = Script.model_validate_json(script_bytes)
        except ValueError as exc:
            raise OnodaError(f"invalid script for case {c.case_id}: {exc}") from exc
        cases[c.case_id] = (case, script)
    return spec, tolerances, ladder, cases


def build_report(
    spec: AblationSpec,
    tolerances: Tolerances | None,
    vectors: dict[str, list[OutcomeVector]],
) -> dict[str, Any]:
    """The report body for `vectors` (arm -> its runs), without store references. Pure."""
    pooled = {arm: pool(vs) for arm, vs in vectors.items()}
    baseline = pooled[spec.baseline]
    comparisons = [
        compare_arm(baseline, pooled[arm], tolerances, spec.efficiency_gate)
        for arm in spec.arms
        if arm != spec.baseline
    ]
    # S3 against the sum of S2a-d only makes sense from a baseline below both.
    interaction_terms = None
    if FULL_ARM in pooled and spec.baseline not in (FULL_ARM, *SINGLE_ARMS):
        singles = {a: pooled[a] for a in SINGLE_ARMS if a in pooled}
        interaction_terms = {
            m: t.model_dump(mode="json")
            for m, t in interaction(baseline, singles, pooled[FULL_ARM]).items()
        }
    all_vectors = [v for vs in vectors.values() for v in vs]
    synthetic = any(v.synthetic for v in all_vectors)
    return {
        "schema": REPORT_SCHEMA,
        "ablation_id": spec.ablation_id,
        "evidence": "synthetic" if synthetic else "measured",
        "caveat": SYNTHETIC_CAVEAT if synthetic else None,
        "baseline": spec.baseline,
        "efficiency_gate": list(spec.efficiency_gate),
        "runs": [v.model_dump(mode="json") for v in all_vectors],
        "arms": {arm: p.model_dump(mode="json") for arm, p in pooled.items()},
        "comparisons": [c.model_dump(mode="json") for c in comparisons],
        "non_dominated": list(non_dominated(comparisons, pooled, spec.efficiency_gate)),
        "interaction": interaction_terms,
    }


def run_ablation(
    spec_path: str | Path,
    *,
    store: Store,
    evaluator_store: Store,
    work_dir: str | Path,
    cases_root: Path = CASES_ROOT,
    timeout_s: float = 300,
) -> tuple[str, dict[str, Any]]:
    """Run every arm on every case, then write the report; returns (report digest, report).

    There is no resume: an ablation that stops partway keeps the runs it made,
    and a rerun needs a new `ablation_id` (A41).
    """
    if store.root.resolve() == evaluator_store.root.resolve() or (
        "agent" in evaluator_store.allowed_visibility
    ):
        raise OnodaError(
            "the evaluator store must be a separate store that refuses agent-visible objects"
        )
    spec, tolerances, ladder, cases = _prepare(Path(spec_path), cases_root)
    run_ids = [run_id_for(spec.ablation_id, a, c) for a in spec.arms for c in cases]
    if taken := [r for r in run_ids if store.has_record("run", r)]:
        raise OnodaError(
            f"ablation {spec.ablation_id} already has runs ({', '.join(taken)}); "
            "ablations do not resume, so give the spec a new ablation_id"
        )
    work_dir = Path(work_dir) / spec.ablation_id
    if work_dir.exists() and any(work_dir.iterdir()):
        raise OnodaError(f"work directory {work_dir} is not empty")

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

    report = build_report(spec, tolerances, vectors)
    report["spec"] = evaluator_store.put_json(
        spec.model_dump(mode="json", by_alias=True),
        visibility="evaluator",
        labels=["ablation-spec"],
    ).digest
    report["tolerances"] = (
        evaluator_store.put_json(
            tolerances.model_dump(mode="json", by_alias=True),
            visibility="evaluator",
            labels=["ablation-tolerances"],
        ).digest
        if tolerances is not None
        else None
    )
    digest = evaluator_store.put_json(
        report,
        visibility="evaluator",
        labels=["ablation-report", *(["synthetic"] if report["evidence"] == "synthetic" else [])],
    ).digest
    return digest, report
