"""VTE bundle staging and claim grading (A57, A58).

`stage_vte_bundle` writes the directory a VTE Pi run is pointed at
(`ONODA_PI_TASK`): `task.json` (`VteTaskBundle`), the case's structure file
under `structure/`, `targets.json`, optional docs and the advisory ladder.
It reads only `LoadedVteCase.structure_bytes` and the manifest; the
references file is never opened, so no reference energy, label or character
can reach the bundle.

`evaluate_vte_claims` grades the agent's `VteClaim` in two steps, writing one
`Evaluation` (`vte-claims` v1) to the evaluator store only:

1. Value. Each claimed state cites a broker job of this run. A job the run
   neither executed nor hit in the cache, or one whose `mr_states` or
   `casscf_converged` carry any parser Problem, makes the claim
   `unsupported` (failing closed: a "lowest root is not a singlet" listing
   keeps its rows but its energies are not excitation energies from the
   ground state, and one bad row taints the listing). Otherwise the claimed
   eV text must equal, as a decimal, the energy printed for a state of the
   claimed spin, and of the claimed irrep when the output prints irreps, in
   the job's listing of the reference method (`SC-NEVPT2`); anything else is
   `contradicted`. Values come from the deterministic parser over the job's
   exact output bytes, never from anything the agent or a reducer wrote.
2. Identity. A `supported` claim is identified only when its cited job printed
   irreps (A71): without them the irrep is the agent's own label, which the
   output cannot confirm. It is then identified with a reference row when
   `normalize_character` of its character picks exactly one row of its
   (spin, irrep), even when that is the only such row. Rows two claims land on are
   identified by neither. Nothing is ever paired by energy or energy order,
   so two states of one irrep with no usable character stay `unidentified`
   however well their order matches (the paper's rule).

Each reference row ends `identified`, `unidentified` (a supported claim of
its spin and irrep exists but could not be pinned to it) or `missing`.
`coverage` counts identified rows (a count, so it sums across cases; the
fraction is `coverage` / `coverage.total`); `mae_ev` is over identified rows
only and `unresolved` when there are none. `accurate` counts identified rows
within `VTE_TOLERANCE_EV` of the reference (draft decision vte-energy-tolerance).

`evaluate_vte_run` grades a recorded VTE run (its jobs as `_vte_jobs` reads
them back from the run's receipts) and `vte_report` summarizes graded runs
per arm (A63).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, NamedTuple

from onoda.adapters.pi.ingest import run_vte_claim
from onoda.adapters.pi.task import check_doc_name, check_instruction
from onoda.adapters.pi.vte_contract import (
    DEFAULT_MAX_EXECUTIONS,
    ClaimedVte,
    VteClaim,
    VteTaskBundle,
)
from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import CapabilityError, IntegrityError, OnodaError
from onoda.evaluation import _next_evaluation_id
from onoda.records import Evaluation, Usage
from onoda.science.contract import ExcitedState
from onoda.science.orca_parser import PARSER_ID, OrcaObservation, parse_orca_output
from onoda.science.vte import (
    LoadedVteCase,
    VteReferenceRow,
    VteReferences,
    load_vte_references,
    normalize_character,
    normalize_irrep,
)
from onoda.store import Store

VTE_EVALUATOR = "vte-claims"
VTE_EVALUATOR_VERSION = (
    "3"  # 2: identity needs printed irreps (A71); 3: character always, tolerance
)
CLAIM_VERDICTS = ("supported", "contradicted", "unsupported")
IDENTITY_VERDICTS = ("identified", "unidentified")
REFERENCE_VERDICTS = ("identified", "unidentified", "missing")

# Receipt labels that mark a job's bytes as not ORCA's (`ingest._Writer.on_execute`).
_SYNTHETIC_JOB_LABELS = frozenset({"stand-in", "not-orca", "synthetic"})
# Parser fields whose Problems make a job's transition energies untrustworthy.
_BLOCKING_FIELDS = frozenset({"mr_states", "casscf_converged"})
# Context: doc://onoda/vte-energy-tolerance@draft#identified-within-tolerance — an identified row counts as accurate only within this many eV of its reference
VTE_TOLERANCE_EV = Decimal("0.3")
VTE_TOLERANCE_SOURCE = "doc://onoda/vte-energy-tolerance#identified-within-tolerance"
# Mean places: the inputs print 3; the mean of n of them is rounded, not truncated.
_MEAN_PLACES = Decimal("0.000001")


class VteJob(NamedTuple):
    """One broker job of the run, as the run record holds it (see `_vte_jobs`)."""

    output: bytes
    executed_here: bool
    cache_hit: bool
    synthetic: bool = False  # a stand-in produced the bytes (labels or metadata say so)


def mean_ev(values: list[Decimal]) -> str:
    """Mean of eV errors as decimal text (`_MEAN_PLACES`), or `unresolved` over nothing."""
    if not values:
        return "unresolved"
    return format(
        (sum(values, Decimal(0)) / len(values)).quantize(_MEAN_PLACES, ROUND_HALF_EVEN), "f"
    )


def ratio_text(numerator: int, denominator: int) -> str:
    """`numerator / denominator` as decimal text (`_MEAN_PLACES`), or `unresolved` over zero."""
    if denominator == 0:
        return "unresolved"
    return format((Decimal(numerator) / denominator).quantize(_MEAN_PLACES, ROUND_HALF_EVEN), "f")


# Context: doc://onoda/vte-report-process-metrics@draft#process-metrics — descriptive only; a run without evaluator-v3 accuracy makes its case's spread unresolved
def stability(accurate_by_case: Mapping[str, list[int | None]]) -> dict[str, Any]:
    """Cross-run spread of the accurate count per case: each run's count and max minus min.

    A case with one run has spread 0. A run graded before evaluator v3 has no
    accuracy (`None`), so its case's spread is `unresolved`, and so is the
    arm's largest spread over cases.
    """
    spread: dict[str, int | str] = {}
    for c, v in sorted(accurate_by_case.items()):
        counts = [n for n in v if n is not None]
        spread[c] = max(counts) - min(counts) if len(counts) == len(v) else "unresolved"
    known = [v for v in spread.values() if isinstance(v, int)]
    return {
        "accurate_per_run": {c: list(v) for c, v in sorted(accurate_by_case.items())},
        "accurate_spread": spread,
        "max_accurate_spread": "unresolved" if len(known) < len(spread) else max(known, default=0),
    }


def stage_vte_bundle(
    case: LoadedVteCase,
    dest: Path,
    *,
    instruction: str,
    docs: Mapping[str, bytes] = {},
    ladder: bytes | None = None,
    synthetic_ladder: bool = False,
    max_executions: int = DEFAULT_MAX_EXECUTIONS,
    report_reminder: bool = False,
) -> VteTaskBundle:
    """Write a `VteTaskBundle` for `case` under `dest`. `dest` must be empty or absent.

    Writes `task.json`, `structure/<name>` (the pinned structure bytes,
    re-verified here), `targets.json`, `docs/*.md` and `ladder.md`. Same
    refusals as `task.stage_bundle`, all before anything is written.
    """
    check_instruction(instruction)
    for name in docs:
        check_doc_name(name)
    if ladder is None and synthetic_ladder:
        raise ValueError("synthetic_ladder=True given without ladder bytes")
    m = case.manifest
    structure_name = Path(m.structure.path).name
    structure_sha256 = sha256_digest(case.structure_bytes)
    if structure_sha256 != m.structure.sha256:
        raise IntegrityError(
            f"{m.structure.path}: staged bytes do not match pinned {m.structure.sha256}"
        )
    bundle = VteTaskBundle(
        case_id=m.case_id,
        instruction=instruction,
        molecule=m.molecule,
        point_group=m.point_group,
        structure_name=structure_name,
        structure_sha256=structure_sha256,
        targets=m.targets,
        review_status=m.review.status,
        docs=tuple(sorted(docs)),
        ladder_sha256=sha256_digest(ladder) if ladder is not None else None,
        synthetic_ladder=synthetic_ladder,
        max_executions=max_executions,
        report_reminder=report_reminder,
    )
    if dest.exists() and any(dest.iterdir()):
        raise OnodaError(f"stage destination {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)

    (dest / "structure").mkdir()
    (dest / "structure" / structure_name).write_bytes(case.structure_bytes)
    targets = {
        "case_id": m.case_id,
        "point_group": m.point_group,
        "targets": [t.model_dump(mode="json") for t in m.targets],
    }
    (dest / "targets.json").write_bytes(canonical_bytes(targets))
    if docs:
        docs_dir = (dest / "docs").resolve()
        docs_dir.mkdir()
        for name in bundle.docs:
            path = (docs_dir / name).resolve()
            if path.parent != docs_dir:
                raise OnodaError(f"doc {name!r} resolves outside {docs_dir}")
            path.write_bytes(docs[name])
    if ladder is not None:
        (dest / "ladder.md").write_bytes(ladder)
    task_json = bundle.model_dump(mode="json", by_alias=True)
    if not bundle.report_reminder:
        del task_json["report_reminder"]  # bundles without the reminder keep their earlier bytes
    (dest / "task.json").write_bytes(canonical_bytes(task_json))
    return bundle


def _ref_key(row: VteReferenceRow) -> str:
    return f"ref.{row.spin}{row.irrep}({row.character})"


def _pt2_method(references: VteReferences) -> str:
    """The listing a claim is read from: the reference method without its basis."""
    return references.method.split("/", 1)[0]


# Context: doc://onoda/vte-grading-contract@0002#claim-from-one-cited-job — a value counts only if the one cited job printed it; no cross-job differences
def _grade_value(
    cs: ClaimedVte,
    jobs: Mapping[str, VteJob],
    method: str,
    synthetic: bool,
    observations: dict[str, OrcaObservation] | None = None,
) -> tuple[str, dict[str, str]]:
    """(claim verdict, detail metrics) for one claimed state's value.

    `observations` (job id -> parse) is filled as jobs are parsed, so an
    evaluation parses each job's output once however many states cite it.
    """
    job = jobs.get(cs.job_id)
    if job is None:
        return "unsupported", {"reason": "job_not_in_run"}
    if not (job.executed_here or job.cache_hit):
        return "unsupported", {"reason": "job_not_executed"}
    if observations is None:
        observations = {}
    obs = observations.get(cs.job_id)
    if obs is None:
        obs = observations[cs.job_id] = parse_orca_output(job.output, synthetic=synthetic)
    if any(p.field in _BLOCKING_FIELDS for p in obs.problems):
        return "unsupported", {"reason": "job_problem"}
    irrep = normalize_irrep(cs.irrep)

    def same_state(s: ExcitedState) -> bool:
        return s.multiplicity == cs.spin and (
            s.symmetry is None or normalize_irrep(s.symmetry) == irrep
        )

    claimed = Decimal(cs.energy.text)

    def printed(s: ExcitedState) -> bool:
        ev = s.energy_ev
        return ev is not None and ev.units == "eV" and Decimal(ev.value) == claimed

    listing = [s for s in obs.mr_states if s.method == method]
    pt2 = [s for s in listing if same_state(s)]
    detail = {"irrep_printed": "true" if any(s.symmetry for s in listing) else "false"}
    match = [s for s in pt2 if printed(s)]
    if match:
        detail["root"] = ",".join(str(s.root) for s in match)
        return "supported", detail
    if any(s.method == "CASSCF" and same_state(s) and printed(s) for s in obs.mr_states):
        return "contradicted", {**detail, "reason": "casscf_value"}
    return "contradicted", {**detail, "reason": "value" if pt2 else "no_state"}


# Context: doc://onoda/match-by-identity@0001#no-energy-order-matching — references are picked by spin, irrep and character, never by energy order
def _candidates(
    cs: ClaimedVte, rows: tuple[VteReferenceRow, ...]
) -> tuple[list[VteReferenceRow], str | None]:
    """Reference rows this supported claim could be, and why it is not one, if so."""
    irrep = normalize_irrep(cs.irrep)
    same = [r for r in rows if r.spin == cs.spin and r.irrep == irrep]
    if not same:
        return [], "no_reference"
    # A lone row of its spin and irrep still needs the character: a label alone
    # credited states several eV from the target (qv5, 2026-10-05).
    character = normalize_character(cs.character)
    picked = [r for r in same if character and r.character == character]
    return (picked, None) if len(picked) == 1 else ([], "character")


def evaluate_vte_claims(
    evaluator_store: Store,
    *,
    run_id: str,
    case: LoadedVteCase,
    references: VteReferences,
    claim: VteClaim | None,
    jobs: Mapping[str, VteJob | tuple[bytes, bool, bool]],
    claim_ref: str | None = None,
    synthetic: bool = False,
) -> Evaluation:
    """Grade `claim` against the run's own job outputs and `references`; see the module doc.

    `jobs` maps every broker job id of the run to (output bytes,
    executed_here, cache_hit[, synthetic]) from the run record. `claim=None`
    (no `report_vtes`) leaves every reference row `missing`. `synthetic` marks
    scripted runs whose outputs are synthetic text; any synthetic job (a
    stand-in's bytes, executed or served from the cache) makes it true too. Writes one new
    `Evaluation` to `evaluator_store`, which must refuse agent-visible objects.
    """
    if "agent" in evaluator_store.allowed_visibility:
        raise CapabilityError("evaluator store must not accept agent-visible artifacts")
    m = case.manifest
    if references.case_id != m.case_id:
        raise IntegrityError(f"references are for {references.case_id!r}, case is {m.case_id!r}")
    if claim is not None and claim.case_id != m.case_id:
        raise IntegrityError(f"claim is for {claim.case_id!r}, case is {m.case_id!r}")
    run_jobs = {job_id: VteJob(*job) for job_id, job in jobs.items()}
    synthetic = synthetic or any(job.synthetic for job in run_jobs.values())
    for job_id, job in run_jobs.items():
        if job.executed_here and job.cache_hit:
            raise IntegrityError(f"job {job_id}: a cache hit is never an execution")
    rows = references.rows
    ref_keys = [_ref_key(r) for r in rows]
    if len(set(ref_keys)) != len(ref_keys):
        raise IntegrityError(f"references for {m.case_id!r} repeat a (spin, irrep, character)")
    method = _pt2_method(references)

    assignments: dict[str, str] = {}
    metrics: dict[str, str] = {}
    states = claim.states if claim is not None else ()
    tags = [f"claim.{i:02d}" for i in range(1, len(states) + 1)]
    supported: list[tuple[str, ClaimedVte]] = []
    observations: dict[str, OrcaObservation] = {}
    for tag, cs in zip(tags, states, strict=True):
        verdict, detail = _grade_value(cs, run_jobs, method, synthetic, observations)
        assignments[tag] = verdict
        metrics[f"{tag}.job"] = cs.job_id
        metrics.update({f"{tag}.{k}": v for k, v in detail.items()})
        if verdict == "supported":
            supported.append((tag, cs))

    # Identity: a row is identified only by exactly one supported claim.
    landed: dict[str, list[str]] = {}
    for tag, cs in supported:
        picked, why = _candidates(cs, rows)
        if metrics.get(f"{tag}.irrep_printed") != "true":
            picked, why = [], "irrep_not_printed"
        if why is not None:
            assignments[f"{tag}.identity"] = "unidentified"
            metrics[f"{tag}.identity_reason"] = why
        else:
            landed.setdefault(_ref_key(picked[0]), []).append(tag)
    claimed_by: dict[str, str] = {}
    for key, claim_tags in landed.items():
        for tag in claim_tags:
            if len(claim_tags) == 1:
                assignments[f"{tag}.identity"] = "identified"
                metrics[f"{tag}.reference"] = key
                claimed_by[key] = tag
            else:
                assignments[f"{tag}.identity"] = "unidentified"
                metrics[f"{tag}.identity_reason"] = "conflict"

    supported_identities = {(cs.spin, normalize_irrep(cs.irrep)) for _, cs in supported}
    by_tag = dict(supported)
    errors: list[Decimal] = []
    for row, key in zip(rows, ref_keys, strict=True):
        if key in claimed_by:
            tag = claimed_by[key]
            error = Decimal(by_tag[tag].energy.text) - Decimal(row.energy_ev)
            errors.append(abs(error))
            assignments[key] = "identified"
            metrics[f"{key}.claim"] = tag
            metrics[f"{key}.error_ev"] = format(error, "f")
            within = abs(error) <= VTE_TOLERANCE_EV
            metrics[f"{key}.accuracy"] = "within" if within else "outside"
        elif (row.spin, row.irrep) in supported_identities:
            assignments[key] = "unidentified"
        else:
            assignments[key] = "missing"

    claim_verdicts = [assignments[t] for t in tags]
    for v in CLAIM_VERDICTS:
        metrics[f"count.claim.{v}"] = str(claim_verdicts.count(v))
    for v in IDENTITY_VERDICTS:
        metrics[f"count.claim.{v}"] = str(sum(assignments.get(f"{t}.identity") == v for t in tags))
    ref_verdicts = [assignments[k] for k in ref_keys]
    for v in REFERENCE_VERDICTS:
        metrics[f"count.reference.{v}"] = str(ref_verdicts.count(v))
    metrics["coverage"] = str(len(errors))
    metrics["coverage.total"] = str(len(rows))
    metrics["accurate"] = str(sum(e <= VTE_TOLERANCE_EV for e in errors))
    metrics["tolerance_ev"] = format(VTE_TOLERANCE_EV, "f")
    metrics["tolerance_source"] = VTE_TOLERANCE_SOURCE
    metrics["abs_error_sum_ev"] = format(sum(errors, Decimal(0)), "f")
    metrics["mae_ev"] = mean_ev(errors)
    metrics["false_success_claims"] = str(
        claim_verdicts.count("contradicted") + claim_verdicts.count("unsupported")
    )
    metrics["case.id"] = m.case_id
    metrics["case.review"] = m.review.status  # an unreviewed case never reads as reviewed (A26)
    metrics["claim.present"] = "true" if claim is not None else "false"
    metrics["parser"] = PARSER_ID
    metrics["reference.method"] = references.method
    metrics["synthetic"] = "true" if synthetic else "false"

    output_refs = {f"job.{j}": sha256_digest(job.output) for j, job in sorted(run_jobs.items())}
    if claim_ref is not None:
        output_refs["claim"] = claim_ref

    evaluation = Evaluation.new(
        evaluation_id=_next_evaluation_id(
            evaluator_store, run_id, VTE_EVALUATOR, VTE_EVALUATOR_VERSION
        ),
        run_id=run_id,
        evaluator=VTE_EVALUATOR,
        evaluator_version=VTE_EVALUATOR_VERSION,
        output_refs=output_refs,
        reference_version=f"{m.references.sha256}:{PARSER_ID}",
        assignments=assignments,
        unresolved=[],
        metrics=metrics,
        usage=Usage(units={}, measured=False, simulated=False, cost_usd=None),
        created_at=evaluator_store.clock.now(),
    )
    evaluator_store.put_record(evaluation)
    return evaluation


def _vte_jobs(store: Store, run_id: str) -> dict[str, VteJob]:
    """Every broker job of the run as its record holds it: log bytes and how it was obtained.

    Reads the receipt metadata `ingest._Writer.on_execute` writes per `task.execute`.
    """
    jobs: dict[str, VteJob] = {}
    for event in store.events(run_id):
        if event.type != "invocation.committed":
            continue
        receipt = store.receipt(event.payload["invocation_id"])
        job_id = receipt.metadata.get("job_id")
        if job_id is None or "log" not in receipt.outputs:
            continue
        jobs[job_id] = VteJob(
            store.get_bytes(receipt.outputs["log"]),
            receipt.metadata["executed_here"] is True,
            receipt.metadata["cache_hit"] is True,
            receipt.metadata.get("stand_in") is True
            or bool(_SYNTHETIC_JOB_LABELS & set(receipt.labels)),
        )
    return jobs


# A job counts as failed when the broker said so or the parser found it did not
# finish cleanly; an RHF job without a CASSCF table is not a failure.
_FAILED_JOB_FIELDS = frozenset({"termination", "casscf_converged"})


class VteJobCounts(NamedTuple):
    jobs: int
    failed: int
    rejected: int  # failed before any SCF or CASSCF iteration: ORCA refused the input


def vte_job_counts(store: Store, run_id: str) -> VteJobCounts:
    """Jobs, failed jobs and rejected inputs of a VTE run: every broker job it named.

    Cache hits are included.
    """
    jobs = failed = rejected = 0
    for event in store.events(run_id):
        if event.type != "invocation.committed":
            continue
        receipt = store.receipt(event.payload["invocation_id"])
        if receipt.metadata.get("job_id") is None or "log" not in receipt.outputs:
            continue
        log = store.get_bytes(receipt.outputs["log"])
        jobs += 1
        failed += job_failed(receipt.status, log)
        rejected += job_input_rejected(receipt.status, log)
    return VteJobCounts(jobs, failed, rejected)


def job_failed(status: str, output: bytes) -> bool:
    """Whether one ORCA job failed: the broker said so, or its output did not finish cleanly."""
    if status == "failed":
        return True
    return any(p.field in _FAILED_JOB_FIELDS for p in parse_orca_output(output).problems)


# The first line of an SCF iteration table (DIIS/SOSCF), a CASSCF macro-iteration
# with ORCA's default step, and a TRAH iteration table, as ORCA 6.1.1 prints them.
_ITERATED = re.compile(
    rb"^(?:Iteration +Energy \(Eh\)|MACRO-ITERATION +\d+:| +Iter\. +energy )", re.M
)


def job_input_rejected(status: str, output: bytes) -> bool:
    """Whether a failed job stopped before any SCF or CASSCF iteration: ORCA refused its input.

    A job that iterated and then failed (did not converge, diverged, or was
    killed at the time limit) is not counted. Neither is an empty output: ORCA
    prints its header before it reads the input, so an empty log is the broker's
    (output over the cap, a broker or launch error), not ORCA refusing the input.
    """
    return bool(output) and job_failed(status, output) and not _ITERATED.search(output)


def evaluate_vte_run(
    store: Store, evaluator_store: Store, run_id: str, case: LoadedVteCase
) -> Evaluation:
    """Grade a VTE run's last claim against its own jobs and the case's references (A58).

    The references are read here, on the evaluator side, and the evaluation
    goes to `evaluator_store` only. A scripted run, a run configured with a
    stand-in, or one citing any job a stand-in produced (including a cache
    hit of a stand-in's entry) is graded as synthetic.
    """
    run = store.run_record(run_id)
    if run.config.get("task_kind") != "vte":
        raise OnodaError(f"run {run_id} is not a VTE run")
    if run.config.get("case_id") != case.manifest.case_id:
        raise OnodaError(f"run {run_id} was recorded from case {run.config.get('case_id')!r}")
    claims = [e for e in store.events(run_id) if e.type == "pi.claim"]
    synthetic = "synthetic" in run.labels or bool(run.config.get("orca", {}).get("stand_in"))
    return evaluate_vte_claims(
        evaluator_store,
        run_id=run_id,
        case=case,
        references=load_vte_references(case),
        claim=run_vte_claim(store, run_id),
        jobs=_vte_jobs(store, run_id),
        claim_ref=claims[-1].payload_refs["claim"] if claims else None,
        synthetic=synthetic,
    )


# Context: doc://onoda/decision-models-shadow-only@0001#never-grade — only vte-claims evaluations are graded; shadow answers never are
def _latest_vte_evaluation(evaluator_store: Store, run_id: str) -> Evaluation:
    prefix = f"{run_id}.{VTE_EVALUATOR}-v"
    ids = sorted(i for i in evaluator_store.list_record_ids("evaluation") if i.startswith(prefix))
    if not ids:
        raise OnodaError(f"run {run_id} has no {VTE_EVALUATOR} evaluation; run `pi evaluate-vte`")
    ev = evaluator_store.get_record("evaluation", ids[-1])
    assert isinstance(ev, Evaluation)
    return ev


def vte_report(
    store: Store, evaluator_store: Store, run_ids: Iterable[str], *, arms: tuple[str, ...] = ()
) -> dict[str, Any]:
    """`vte_report_runs` over runs that share one store and one evaluator store."""
    return vte_report_runs(((store, evaluator_store, r) for r in run_ids), arms=arms)


def vte_report_runs(
    runs: Iterable[tuple[Store, Store, str]], *, arms: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Coverage, MAE and false successes per arm over graded VTE runs; never a verdict (A63).

    Each run is read from its own (store, evaluator store), so runs recorded in
    parallel, one store each, are pooled the same way as runs in one store.
    An arm is a run's variant. Per arm: identified reference rows over all
    rows of its runs (`coverage`), how many of those are within the tolerance
    (`accurate`), MAE over every identified row of every run, the false-success
    claim count, the runs that never reported (`runs_without_claim`, so a
    timed-out run is not read as an agent that found nothing), and the ORCA
    jobs the arm named with how many failed (`jobs`, `jobs_failed`) and how many of
    those ORCA refused before iterating (`jobs_rejected`). Process metrics
    (the Nature Reviews Methods Primer's "cross-run stability" and efficiency,
    doi 10.1038/s43586-026-00534-y): `jobs_per_accurate` and
    `jobs_failed_share` as decimal text, and `stability`, each case's accurate
    count per run in run order with its spread (max minus min). When exactly
    two arms are compared (or `arms` names two), `common_target_mae_ev` restricts both to targets
    (case, reference row) each arm identified in at least one run, averaging
    an arm's repeats per target first, so both arms are measured on the same
    states. Values are decimal text; a mean over nothing is `unresolved`.
    Runs graded as synthetic make the report synthetic: not a chemistry or
    mechanism result.
    """
    per_arm: dict[str, dict[str, Any]] = {}
    errors: dict[str, dict[tuple[str, str], list[Decimal]]] = {}
    accurate_by_case: dict[str, dict[str, list[int | None]]] = {}
    synthetic = False
    for store, evaluator_store, run_id in runs:
        run = store.run_record(run_id)
        if run.config.get("task_kind") != "vte":
            raise OnodaError(f"run {run_id} is not a VTE run")
        arm = run.config["variant"]
        if arms and arm not in arms:
            continue
        ev = _latest_vte_evaluation(evaluator_store, run_id)
        m = ev.metrics
        synthetic |= m.get("synthetic") == "true"
        a = per_arm.setdefault(
            arm,
            {
                "runs": [],
                "coverage": 0,
                "coverage_total": 0,
                "accurate": 0,
                "false_success_claims": 0,
                "runs_without_claim": 0,
                "jobs": 0,
                "jobs_failed": 0,
                "jobs_rejected": 0,
            },
        )
        counts = vte_job_counts(store, run_id)
        a["jobs"] += counts.jobs
        a["jobs_failed"] += counts.failed
        a["jobs_rejected"] += counts.rejected
        a["runs"].append(run_id)
        a["coverage"] += int(m["coverage"])
        a["accurate"] += int(m.get("accurate", 0))  # absent before evaluator v3
        accurate_by_case.setdefault(arm, {}).setdefault(m["case.id"], []).append(
            int(m["accurate"]) if "accurate" in m else None
        )
        a["runs_without_claim"] += m["claim.present"] == "false"  # never reported, e.g. timed out
        a["coverage_total"] += int(m["coverage.total"])
        a["false_success_claims"] += int(m["false_success_claims"])
        by_target = errors.setdefault(arm, {})
        for key, value in m.items():
            if key.startswith("ref.") and key.endswith(".error_ev"):
                target = (m["case.id"], key.removesuffix(".error_ev"))
                by_target.setdefault(target, []).append(abs(Decimal(value)))
    for arm, a in per_arm.items():
        a["mae_ev"] = mean_ev([e for errs in errors[arm].values() for e in errs])
        a["identified_targets"] = len(errors[arm])
        pre_v3 = any(n is None for v in accurate_by_case[arm].values() for n in v)
        # A run graded before evaluator v3 adds jobs but no accuracy: no ratio over mixed runs.
        a["jobs_per_accurate"] = "unresolved" if pre_v3 else ratio_text(a["jobs"], a["accurate"])
        a["jobs_failed_share"] = ratio_text(a["jobs_failed"], a["jobs"])
        a["stability"] = stability(accurate_by_case[arm])
    report: dict[str, Any] = {
        "arms": per_arm,
        "synthetic": synthetic,
        "note": (
            "synthetic runs (scripted agent or stand-in ORCA): not a chemistry or mechanism result"
            if synthetic
            else "counts and errors only; no arm is declared better"
        ),
    }
    compared = arms if len(arms) == 2 else tuple(sorted(per_arm)) if len(per_arm) == 2 else ()
    if compared and all(arm in per_arm for arm in compared):
        common = set(errors[compared[0]]) & set(errors[compared[1]])
        report["common_targets"] = [f"{case}:{ref}" for case, ref in sorted(common)]
        report["common_target_mae_ev"] = {
            arm: mean_ev(
                [sum(errors[arm][t], Decimal(0)) / len(errors[arm][t]) for t in sorted(common)]
            )
            for arm in compared
        }
    return report
