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
2. Identity. A `supported` claim is identified with a reference row when its
   (spin, irrep) picks exactly one row, or `normalize_character` of its
   character picks exactly one among several. Rows two claims land on are
   identified by neither. Nothing is ever paired by energy or energy order,
   so two states of one irrep with no usable character stay `unidentified`
   however well their order matches (the paper's rule).

Each reference row ends `identified`, `unidentified` (a supported claim of
its spin and irrep exists but could not be pinned to it) or `missing`.
`coverage` counts identified rows (a count, so it sums across cases; the
fraction is `coverage` / `coverage.total`); `mae_ev` is over identified rows
only and `unresolved` when there are none.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import NamedTuple

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
from onoda.science.orca_parser import PARSER_ID, parse_orca_output
from onoda.science.vte import (
    LoadedVteCase,
    VteReferenceRow,
    VteReferences,
    normalize_character,
    normalize_irrep,
)
from onoda.store import Store

VTE_EVALUATOR = "vte-claims"
VTE_EVALUATOR_VERSION = "1"
CLAIM_VERDICTS = ("supported", "contradicted", "unsupported")
IDENTITY_VERDICTS = ("identified", "unidentified")
REFERENCE_VERDICTS = ("identified", "unidentified", "missing")

# Parser fields whose Problems make a job's transition energies untrustworthy.
_BLOCKING_FIELDS = frozenset({"mr_states", "casscf_converged"})
# Mean places: the inputs print 3; the mean of n of them is rounded, not truncated.
_MEAN_PLACES = Decimal("0.000001")


class VteJob(NamedTuple):
    """One broker job of the run, as the run record holds it (see `run._vte_jobs`)."""

    output: bytes
    executed_here: bool
    cache_hit: bool


def mean_ev(values: list[Decimal]) -> str:
    """Mean of eV errors as decimal text (`_MEAN_PLACES`), or `unresolved` over nothing."""
    if not values:
        return "unresolved"
    return format(
        (sum(values, Decimal(0)) / len(values)).quantize(_MEAN_PLACES, ROUND_HALF_EVEN), "f"
    )


def stage_vte_bundle(
    case: LoadedVteCase,
    dest: Path,
    *,
    instruction: str,
    docs: Mapping[str, bytes] = {},
    ladder: bytes | None = None,
    synthetic_ladder: bool = False,
    max_executions: int = DEFAULT_MAX_EXECUTIONS,
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
    (dest / "task.json").write_bytes(canonical_bytes(bundle.model_dump(mode="json", by_alias=True)))
    return bundle


def _ref_key(row: VteReferenceRow) -> str:
    return f"ref.{row.spin}{row.irrep}({row.character})"


def _pt2_method(references: VteReferences) -> str:
    """The listing a claim is read from: the reference method without its basis."""
    return references.method.split("/", 1)[0]


def _grade_value(
    cs: ClaimedVte, jobs: Mapping[str, VteJob], method: str, synthetic: bool
) -> tuple[str, dict[str, str]]:
    """(claim verdict, detail metrics) for one claimed state's value."""
    job = jobs.get(cs.job_id)
    if job is None:
        return "unsupported", {"reason": "job_not_in_run"}
    if not (job.executed_here or job.cache_hit):
        return "unsupported", {"reason": "job_not_executed"}
    obs = parse_orca_output(job.output, synthetic=synthetic)
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


def _candidates(
    cs: ClaimedVte, rows: tuple[VteReferenceRow, ...]
) -> tuple[list[VteReferenceRow], str | None]:
    """Reference rows this supported claim could be, and why it is not one, if so."""
    irrep = normalize_irrep(cs.irrep)
    same = [r for r in rows if r.spin == cs.spin and r.irrep == irrep]
    if not same:
        return [], "no_reference"
    if len(same) == 1:
        return same, None
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
    executed_here, cache_hit) from the run record. `claim=None` (no
    `report_vtes`) leaves every reference row `missing`. `synthetic` marks
    scripted runs whose outputs are synthetic text. Writes one new
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
    for tag, cs in zip(tags, states, strict=True):
        verdict, detail = _grade_value(cs, run_jobs, method, synthetic)
        assignments[tag] = verdict
        metrics[f"{tag}.job"] = cs.job_id
        metrics.update({f"{tag}.{k}": v for k, v in detail.items()})
        if verdict == "supported":
            supported.append((tag, cs))

    # Identity: a row is identified only by exactly one supported claim.
    landed: dict[str, list[str]] = {}
    for tag, cs in supported:
        picked, why = _candidates(cs, rows)
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
    metrics["abs_error_sum_ev"] = format(sum(errors, Decimal(0)), "f")
    metrics["mae_ev"] = mean_ev(errors)
    metrics["false_success_claims"] = str(
        claim_verdicts.count("contradicted") + claim_verdicts.count("unsupported")
    )
    metrics["case.id"] = m.case_id
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
