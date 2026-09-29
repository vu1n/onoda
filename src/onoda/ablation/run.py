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

A live ablation (`live=`) runs the same arms against a real provider inside
the sandbox (A50), with no scripts. It runs in rounds, one round being every
arm on one case, and stops before a round that `max_cost_usd` cannot cover at
the per-run cap, so the arms it compares always hold the same runs. Its report
is labelled measured and carries the short-run caveat instead.
"""

from __future__ import annotations

from decimal import Decimal
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
from onoda.adapters.pi.runtime import LiveSettings
from onoda.adapters.pi.sandbox import Sandbox
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

LIVE_CAVEAT = (
    "Live runs of one model on short ORCA output-reading tasks. SoL-Pi's savings are "
    "measured on multi-hour runs, and short runs never accept or reject a mechanism "
    "(SOL_PI_DELTA §5). Exploratory; this report is not a mechanism verdict."
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


def run_id_for(ablation_id: str, arm: str, case_id: str, repeat: int | None = None) -> str:
    """`repeat` (1-based) is appended only for specs with `repeats` > 1."""
    base = f"{ablation_id}-{arm}-{case_id}"
    return base if repeat is None else f"{base}-r{repeat}"


def _prepare(
    spec_path: Path, cases_root: Path
) -> tuple[
    AblationSpec, Tolerances | None, bytes | None, dict[str, tuple[LoadedCase, Script | None]]
]:
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
    cases: dict[str, tuple[LoadedCase, Script | None]] = {}
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
        if c.script is None:
            cases[c.case_id] = (case, None)
            continue
        script_bytes = _read(_under(base, c.script, "script"), f"script for case {c.case_id}")
        try:
            script = Script.model_validate_json(script_bytes)
        except ValueError as exc:
            raise OnodaError(f"invalid script for case {c.case_id}: {exc}") from exc
        cases[c.case_id] = (case, script)
    return spec, tolerances, ladder, cases


def _check_mode(
    spec: AblationSpec,
    cases: dict[str, tuple[LoadedCase, Script | None]],
    live: LiveSettings | None,
    reducer: tuple[str, str] | None,
    sandbox: Sandbox | None,
    max_cost_usd: Decimal | None,
) -> None:
    """Scripted and live ablations each get exactly their own inputs; refused before any run."""
    scripted = [c for c, (_, s) in cases.items() if s is not None]
    if live is None:
        if missing := [c for c in cases if c not in scripted]:
            raise OnodaError(f"a scripted ablation needs a script for every case: {missing}")
        if reducer is not None or max_cost_usd is not None:
            raise OnodaError("reducer and max_cost_usd are live-ablation settings")
        return
    if scripted:
        raise OnodaError(f"a live ablation takes no scripts; these cases name one: {scripted}")
    live.validate()
    if sandbox is None:
        raise OnodaError("a live ablation runs Pi in the sandbox (A50); none was given")
    if max_cost_usd is None or max_cost_usd <= 0:
        raise OnodaError("a live ablation needs a positive max_cost_usd for the whole run")
    if reducer is None and any(
        "evidencePreservingReducer" in VARIANTS[a].mechanisms for a in spec.arms
    ):
        raise OnodaError("an arm runs the Evidence-Preserving Reducer; name a reducer model")


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
    live: LiveSettings | None = None,
    reducer: tuple[str, str] | None = None,
    sandbox: Sandbox | None = None,
    max_cost_usd: Decimal | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run every arm on every case, then write the report; returns (report digest, report).

    Live (`live` given): `sandbox` and `max_cost_usd` (the whole ablation's cap)
    are required, and `reducer` = (provider, model) when an arm runs the
    Evidence-Preserving Reducer. Scripted: every case names a script.

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
    _check_mode(spec, cases, live, reducer, sandbox, max_cost_usd)
    repeats: list[int | None] = [None] if spec.repeats == 1 else list(range(1, spec.repeats + 1))
    run_ids = [
        run_id_for(spec.ablation_id, a, c, r) for r in repeats for c in cases for a in spec.arms
    ]
    if taken := [r for r in run_ids if store.has_record("run", r)]:
        raise OnodaError(
            f"ablation {spec.ablation_id} already has runs ({', '.join(taken)}); "
            "ablations do not resume, so give the spec a new ablation_id"
        )
    work_dir = Path(work_dir) / spec.ablation_id
    if work_dir.exists() and any(work_dir.iterdir()):
        raise OnodaError(f"work directory {work_dir} is not empty")

    vectors: dict[str, list[OutcomeVector]] = {arm: [] for arm in spec.arms}
    spent = Decimal(0)
    rounds = 0
    stopped: str | None = None
    # One round = every arm on one case, so a budget stop never leaves arms unequal.
    round_reserve = (
        Decimal(live.max_cost_usd) * len(spec.arms) if live and live.max_cost_usd else None
    )
    for repeat, (case_id, (case, script)) in ((r, item) for r in repeats for item in cases.items()):
        if max_cost_usd is not None and round_reserve is not None:
            if spent + round_reserve > max_cost_usd:
                stopped = (
                    f"budget: spent {spent} USD, a round reserves {round_reserve} USD "
                    f"against the {max_cost_usd} USD cap"
                )
                break
        for arm in spec.arms:
            needs_ladder = VARIANTS[arm].ladder
            run_id = run_id_for(spec.ablation_id, arm, case_id, repeat)
            run_case(
                store,
                case,
                run_id=run_id,
                variant_id=arm,
                work_dir=work_dir / arm / case_id / (f"r{repeat}" if repeat else ""),
                script=script,
                live=live,
                ladder=ladder if needs_ladder else None,
                synthetic_ladder=spec.synthetic_ladder and needs_ladder,
                max_requests=None if live else spec.max_requests,
                reducer=reducer if live else None,
                timeout_s=timeout_s,
                sandbox=sandbox,
            )
            claims = [e for e in store.events(run_id) if e.type == "pi.claim"]
            evaluation = evaluate_claims(
                evaluator_store,
                run_id=run_id,
                case=case,
                claim=run_claim(store, run_id),
                claim_ref=claims[-1].payload_refs["claim"] if claims else None,
            )
            vector = outcome_vector(store, evaluation, arm=arm)
            vectors[arm].append(vector)
            cost = vector.efficiency.get("cost_usd")
            if cost is None:
                if live:  # an unknown cost cannot be held to the cap
                    raise OnodaError(f"run {run_id} reported no cost; the ablation stops")
            else:
                spent += Decimal(cost)
        rounds += 1

    if rounds == 0:
        raise OnodaError(f"ablation {spec.ablation_id} ran nothing: {stopped}")
    report = build_report(spec, tolerances, vectors)
    if live:
        report["caveat"] = LIVE_CAVEAT
    report["mode"] = "live" if live else "scripted"
    report["rounds"] = {"planned": len(repeats) * len(cases), "completed": rounds}
    report["stopped"] = stopped
    report["spent_usd"] = str(spent) if live else None  # scripted costs are synthetic
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
