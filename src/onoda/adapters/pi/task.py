"""Task bundle staging and claim grading (SOL_PI_DELTA §3, M2's evaluator pattern).

`stage_bundle` writes the directory a Pi run is pointed at (`ONODA_PI_TASK`):
the case's pinned input and output bytes, and nothing an evaluator would need
kept from the agent (no reference observation, no evaluator-visibility data).
It verifies the staged bytes against the case manifest itself, independent of
`load_case`'s own check, so a bundle can never silently drift from what it
claims to contain.

`evaluate_claims` grades the agent's `Claim` (from `report_states`) against
the deterministic parser's reading of the same pinned output bytes the agent
was given, exactly as `onoda.science.evaluator` grades an `OrcaObservation`
against a reference: read-only inputs, one freshly-derived `Evaluation`
written only to the evaluator store. States are paired by declared identity
(multiplicity, root) alone, never by nearest energy (BUILD_SPEC §5); a state
whose multiplicity is unknown on either side can never be paired with
confidence and is `unresolved`, not guessed at.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from onoda.adapters.pi.contract import Claim, ClaimedState, TaskBundle
from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import CapabilityError, IntegrityError, OnodaError
from onoda.evaluation import _next_evaluation_id
from onoda.records import Evaluation, Usage
from onoda.science.contract import ExcitedState, LoadedCase
from onoda.science.orca_parser import PARSER_ID, parse_orca_output
from onoda.store import Store

CLAIM_EVALUATOR = "pi-claims"
CLAIM_EVALUATOR_VERSION = "1"
VERDICTS = ("supported", "contradicted", "unsupported", "missing", "unresolved")


def _single_output(case: LoadedCase) -> tuple[str, bytes]:
    if len(case.output_bytes) != 1:
        raise CapabilityError(
            f"case {case.manifest.case_id!r} pins {len(case.output_bytes)} outputs; "
            "a task bundle needs exactly one"
        )
    ((name, data),) = case.output_bytes.items()
    return name, data


def stage_bundle(
    case: LoadedCase,
    dest: Path,
    *,
    instruction: str,
    docs: Mapping[str, bytes] = {},
    ladder: bytes | None = None,
    synthetic_ladder: bool = False,
) -> TaskBundle:
    """Write a `TaskBundle` for `case` under `dest`. `dest` must be empty or absent.

    Only the case's own pinned bytes, `docs` (allowed documentation, named
    `*.md`) and an optional `ladder.md` are written; nothing from the
    evaluator side (no parsed reference, no reference observation) goes into
    the bundle.
    """
    if dest.exists() and any(dest.iterdir()):
        raise OnodaError(f"stage destination {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)

    m = case.manifest
    input_name = Path(m.input.path).name
    input_sha256 = sha256_digest(case.input_bytes)
    if input_sha256 != m.input.sha256:
        raise IntegrityError(f"{m.input.path}: staged bytes do not match pinned {m.input.sha256}")

    output_key, output_bytes = _single_output(case)
    output_name = Path(m.outputs[output_key].path).name
    output_sha256 = sha256_digest(output_bytes)
    if output_sha256 != m.outputs[output_key].sha256:
        raise IntegrityError(
            f"{m.outputs[output_key].path}: staged bytes do not match pinned "
            f"{m.outputs[output_key].sha256}"
        )

    (dest / "input").mkdir()
    (dest / "input" / input_name).write_bytes(case.input_bytes)
    (dest / "output").mkdir()
    (dest / "output" / output_name).write_bytes(output_bytes)

    doc_names: list[str] = []
    if docs:
        (dest / "docs").mkdir()
        for name in sorted(docs):
            if not name.endswith(".md"):
                raise ValueError(f"doc {name!r} is not a .md file")
            (dest / "docs" / name).write_bytes(docs[name])
            doc_names.append(name)

    ladder_sha256: str | None = None
    if ladder is not None:
        (dest / "ladder.md").write_bytes(ladder)
        ladder_sha256 = sha256_digest(ladder)
    elif synthetic_ladder:
        raise ValueError("synthetic_ladder=True given without ladder bytes")

    bundle = TaskBundle(
        case_id=m.case_id,
        instruction=instruction,
        input_name=input_name,
        input_sha256=input_sha256,
        output_name=output_name,
        output_sha256=output_sha256,
        review_status=m.review.status,
        docs=tuple(doc_names),
        ladder_sha256=ladder_sha256,
        synthetic_ladder=synthetic_ladder,
    )
    (dest / "task.json").write_bytes(canonical_bytes(bundle.model_dump(mode="json", by_alias=True)))
    return bundle


def _decimal(text: str) -> Decimal:
    return Decimal(text)


def _grade_claimed(
    cs: ClaimedState,
    unresolved_roots: set[int],
    parsed_by_id: dict[tuple[int, int], ExcitedState],
) -> tuple[str, dict[str, str]]:
    """Grade one claimed state; `parsed_by_id` keys are (multiplicity, root)."""
    if cs.multiplicity is None or cs.root in unresolved_roots:
        return "unresolved", {}
    parsed = parsed_by_id.get((cs.multiplicity, cs.root))
    if parsed is None:
        return "unsupported", {}
    if cs.energy.unit != parsed.energy_au.units:
        return "contradicted", {"reason": "unit"}
    if _decimal(cs.energy.text) != _decimal(parsed.energy_au.value):
        return "contradicted", {}
    return "supported", {}


def evaluate_claims(
    evaluator_store: Store,
    *,
    run_id: str,
    case: LoadedCase,
    claim: Claim | None,
    claim_ref: str | None,
) -> Evaluation:
    """Grade `claim` against the deterministic parser's reading of `case`'s pinned output.

    Parses the case's own pinned bytes (never a store artifact the agent may
    have altered), pairs claimed and parsed excited states by (multiplicity,
    root) alone, and writes one new `Evaluation` to `evaluator_store` only.
    `claim=None` (the agent never called `report_states`) grades every
    requested state and the final energy as `missing`.
    """
    m = case.manifest
    output_key, output_bytes = _single_output(case)
    output_sha256 = sha256_digest(output_bytes)
    observation = parse_orca_output(output_bytes)

    unresolved_roots = {s.root for s in observation.excited_states if s.multiplicity is None}
    parsed_by_id = {
        (s.multiplicity, s.root): s
        for s in observation.excited_states
        if s.multiplicity is not None
    }
    attributable_by_mult: dict[int, list[ExcitedState]] = {}
    for s in observation.excited_states:
        if s.multiplicity is not None:
            attributable_by_mult.setdefault(s.multiplicity, []).append(s)
    for group in attributable_by_mult.values():
        group.sort(key=lambda s: s.root)
    unattributable_roots = sorted(unresolved_roots)

    # A requested identity is the actual (multiplicity, root) ORCA printed for that
    # multiplicity's first N states, never an assumed 1..N numbering (ORCA continues
    # root numbers across a combined singlet/triplet listing). When the multiplicity
    # is never attributed at all (A28: ADC(2)/EOM-CCSD), the identity itself is
    # unresolved, not missing.
    requested_ids: set[tuple[int, int]] = set()
    requested_unresolved: set[tuple[int, int]] = set()
    for mult, n in m.request.roots_by_multiplicity.items():
        group = attributable_by_mult.get(mult, [])
        if group:
            requested_ids.update((mult, s.root) for s in group[:n])
        else:
            for root in unattributable_roots[:n]:
                requested_ids.add((mult, root))
                requested_unresolved.add((mult, root))

    claimed_ids: dict[tuple[int | None, int], ClaimedState] = {}
    for cs in claim.states if claim is not None else ():
        claimed_ids[(cs.multiplicity, cs.root)] = cs

    assignments: dict[str, str] = {}
    metrics: dict[str, str] = {}
    counts = dict.fromkeys(VERDICTS, 0)

    def record(tag: str, verdict: str, detail: dict[str, str]) -> None:
        assignments[tag] = verdict
        counts[verdict] += 1
        for k, v in detail.items():
            metrics[f"state.{tag}.{k}"] = v

    for (mult, root), cs in claimed_ids.items():
        tag = f"m{mult if mult is not None else '?'}.r{root}"
        verdict, detail = _grade_claimed(cs, unresolved_roots, parsed_by_id)
        record(tag, verdict, detail)

    for key in sorted(requested_ids - set(claimed_ids)):
        tag = f"m{key[0]}.r{key[1]}"
        verdict = "unresolved" if key in requested_unresolved else "missing"
        record(tag, verdict, {})

    if m.request.expects_final_energy:
        claimed_final = claim.final_energy if claim is not None else None
        if claimed_final is None:
            record("final_energy", "missing", {})
        elif observation.final_energy is None:
            record("final_energy", "unsupported", {})
        elif claimed_final.unit != observation.final_energy.units:
            record("final_energy", "contradicted", {"reason": "unit"})
        elif _decimal(claimed_final.text) != _decimal(observation.final_energy.value):
            record("final_energy", "contradicted", {})
        else:
            record("final_energy", "supported", {})

    metrics.update({f"count.{v}": str(counts[v]) for v in VERDICTS})
    metrics["false_success_claims"] = str(counts["contradicted"] + counts["unsupported"])
    metrics["case.id"] = m.case_id
    metrics["claim.present"] = "true" if claim is not None else "false"
    metrics["parser"] = PARSER_ID
    metrics["output.name"] = output_key

    output_refs = {"output": output_sha256}
    if claim_ref is not None:
        output_refs["claim"] = claim_ref

    evaluation = Evaluation.new(
        evaluation_id=_next_evaluation_id(
            evaluator_store, run_id, CLAIM_EVALUATOR, CLAIM_EVALUATOR_VERSION
        ),
        run_id=run_id,
        evaluator=CLAIM_EVALUATOR,
        evaluator_version=CLAIM_EVALUATOR_VERSION,
        output_refs=output_refs,
        reference_version=f"{output_sha256}:{PARSER_ID}",
        assignments=assignments,
        unresolved=[k for k, v in assignments.items() if v == "unresolved"],
        metrics=metrics,
        usage=Usage(units={}, measured=False, simulated=False, cost_usd=None),
        created_at=evaluator_store.clock.now(),
    )
    evaluator_store.put_record(evaluation)
    return evaluation
