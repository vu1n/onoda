"""External ORCA evaluator skeleton (M2-004; BUILD_SPEC §5, ASSUMPTIONS A3/A6).

A protected derivation over `OrcaObservation`s. It is a *reconstructed*
evaluator (A3): its checks are conservative harness rules, not the original
paper's evaluator, and none of its verdicts is a chemistry claim.

Checks, each assigned "pass" | "fail" | "unresolved":

- `execution_status`: normal termination and a converged SCF.
- `parse_integrity`: no parser `Problem`s. A synthetic observation (mutated or
  test-constructed bytes) is never allowed to pass this check: it is
  `unresolved` at best, and `metrics["observation.synthetic"]` says so, so a
  synthetic result can never be presented as an authentic one.
- `state_identification`: excited-state counts per multiplicity against the
  case's declared request. States without a stated multiplicity cannot be
  attributed and leave the check unresolved. No energy-based matching.
- `reference_comparison` (only with a reference): states are paired by
  declared identity — multiplicity, then rank by the program's root number
  within that multiplicity — never by nearest energy. When both sides state a
  symmetry label they must agree. Differences are exact `Decimal`
  subtractions (candidate − reference) recorded as decimal strings. A policy
  without a justified tolerance (A6) reports the differences and leaves the
  check unresolved; a tolerance is only accepted together with the
  assumptions-register id that justifies it.

Outcome precedence for `reference_comparison`: definite structural
discrepancies (unpaired states, symmetry disagreement) → "fail"; anything that
prevents a numeric judgement (unit mismatch, missing value, unattributable
state, tolerance unresolved) → "unresolved"; otherwise |diff| <= tolerance.

Storage mirrors `labrun.evaluation`: the reference lives only in an
evaluator-visibility store, the observation is read (never written) from the
agent store, and every evaluation is a brand new `Evaluation` record.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from labrun.errors import CapabilityError
from labrun.evaluation import _next_evaluation_id
from labrun.records import Evaluation, Usage
from labrun.science.contract import CaseManifest, ExcitedState, OrcaObservation, Quantity
from labrun.store import Store

EVALUATOR = "orca-smoke"
EVALUATOR_VERSION = "1"
REFERENCE_SCHEMA = "labrun.orca-case-reference/1"

QuantityName = Literal["excited_states.energy_au", "final_energy"]


class ComparisonPolicy(BaseModel):
    """How a candidate observation is compared with a reference observation.

    `tolerance` is a non-negative decimal string in `tolerance_units`; `None`
    means no justified tolerance exists yet (A6) and the comparison can only
    report differences. A tolerance must cite `tolerance_source`, an
    assumptions-register id; construction without one is rejected.
    `kind` separates reexecution agreement from accuracy against a reference
    (BUILD_SPEC §5): they are different policies, never interchangeable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    kind: Literal["reexecution", "reference-accuracy"]
    quantities: tuple[QuantityName, ...] = ("excited_states.energy_au", "final_energy")
    pairing: Literal["multiplicity-and-root-order"] = "multiplicity-and-root-order"
    tolerance: str | None = None
    tolerance_units: str | None = None
    tolerance_source: str | None = None

    @model_validator(mode="after")
    def _justified_tolerance(self) -> ComparisonPolicy:
        if self.tolerance is None:
            if self.tolerance_units is not None:
                raise ValueError("tolerance_units given without a tolerance")
            return self
        if not self.tolerance_source:
            raise ValueError("a tolerance requires tolerance_source (an assumptions id)")
        if not self.tolerance_units:
            raise ValueError("a tolerance requires tolerance_units")
        tol = _decimal(self.tolerance)
        if tol is None or tol < 0:
            raise ValueError(f"tolerance {self.tolerance!r} is not a non-negative decimal")
        return self


POLICIES: dict[str, ComparisonPolicy] = {
    p.policy_id: p
    for p in (
        ComparisonPolicy(
            policy_id="orca-reexecution@1",
            kind="reexecution",
            tolerance=None,
            tolerance_source="A6",
        ),
    )
}


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except decimal.InvalidOperation:
        return None
    return value if value.is_finite() else None


def _exact_diff(a: Decimal, b: Decimal) -> Decimal:
    """a - b with no rounding (raises if the context would round)."""
    with decimal.localcontext() as ctx:
        ctx.prec = max(len(a.as_tuple().digits), len(b.as_tuple().digits)) + 64
        ctx.traps[decimal.Inexact] = True
        ctx.traps[decimal.Rounded] = True
        return a - b


def _fmt(value: Decimal) -> str:
    return format(value, "f")


# ------------------------------------------------------------------ reference


def publish_case_reference(
    evaluator_store: Store,
    *,
    case_id: str,
    observation: OrcaObservation,
    reference_version: str,
    provenance: dict[str, Any],
) -> str:
    """Store a reference observation for a case. Returns its digest.

    Written with `visibility="evaluator"`; an agent-visibility store refuses
    it (`VisibilityError`), so a reference never lands where an agent reads.
    """
    payload = {
        "schema": REFERENCE_SCHEMA,
        "case_id": case_id,
        "reference_version": reference_version,
        "provenance": provenance,
        "observation": observation.to_json(),
    }
    labels = ["evaluator-reference"] + (["synthetic"] if observation.synthetic else [])
    return evaluator_store.put_json(payload, visibility="evaluator", labels=labels).digest


# ------------------------------------------------------------------ checks

Checked = tuple[str, dict[str, str]]  # (assignment, metrics)


def _execution_status(obs: OrcaObservation) -> Checked:
    if obs.termination == "error" or obs.scf == "not_converged":
        return "fail", {"execution_status.reason": f"termination={obs.termination} scf={obs.scf}"}
    if obs.termination == "absent" or obs.scf == "absent":
        return "unresolved", {
            "execution_status.reason": f"termination={obs.termination} scf={obs.scf}"
        }
    return "pass", {}


def _parse_integrity(obs: OrcaObservation) -> Checked:
    metrics = {"problems.count": str(len(obs.problems))}
    states = {p.state for p in obs.problems}
    if "error" in states:
        errors = sorted({p.field for p in obs.problems if p.state == "error"})
        return "fail", {**metrics, "parse_integrity.reason": "error in " + ",".join(errors)}
    if states:
        fields = sorted({f"{p.field}:{p.state}" for p in obs.problems})
        return "unresolved", {**metrics, "parse_integrity.reason": ",".join(fields)}
    if obs.synthetic:
        return "unresolved", {
            **metrics,
            "parse_integrity.reason": "synthetic observation: authentic parse not established",
        }
    return "pass", metrics


def _by_multiplicity(states: list[ExcitedState]) -> dict[int, list[ExcitedState]]:
    grouped: dict[int, list[ExcitedState]] = {}
    for s in states:
        assert s.multiplicity is not None
        grouped.setdefault(s.multiplicity, []).append(s)
    return {m: sorted(v, key=lambda s: s.root) for m, v in sorted(grouped.items())}


def _unattributable(obs: OrcaObservation) -> str | None:
    """Why the observation's states cannot be attributed to a declared identity."""
    if obs.excited_states_status in ("ambiguous", "error"):
        return f"excited states {obs.excited_states_status}"
    if any(p.field.startswith("excited_states") for p in obs.problems):
        return "excited states have parser problems"
    if any(s.multiplicity is None for s in obs.excited_states):
        return "excited state without a stated multiplicity"
    seen = [(s.multiplicity, s.root) for s in obs.excited_states]
    if len(seen) != len(set(seen)):
        return "duplicate (multiplicity, root) labels"
    return None


def _state_identification(obs: OrcaObservation, case: CaseManifest) -> Checked:
    expected = case.request.roots_by_multiplicity
    metrics = {f"states.expected.m{m}": str(n) for m, n in sorted(expected.items())}
    if obs.excited_states_status == "absent" and not expected:
        return "pass", metrics
    reason = _unattributable(obs)
    if reason is None and obs.excited_states_status == "absent":
        reason = "excited states absent but requested"
    if reason is not None:
        return "unresolved", {**metrics, "state_identification.reason": reason}
    observed = {m: len(v) for m, v in _by_multiplicity(obs.excited_states).items()}
    metrics.update({f"states.observed.m{m}": str(n) for m, n in observed.items()})
    mismatched = sorted(
        m for m in set(expected) | set(observed) if expected.get(m, 0) != observed.get(m, 0)
    )
    if mismatched:
        detail = ",".join(
            f"m{m}:expected={expected.get(m, 0)},observed={observed.get(m, 0)}" for m in mismatched
        )
        return "fail", {**metrics, "state_identification.reason": "count mismatch " + detail}
    return "pass", metrics


def _compare_quantity(
    key: str, cand: Quantity | None, ref: Quantity | None, policy: ComparisonPolicy
) -> tuple[str | None, dict[str, str]]:
    """Returns (blocking reason or None, metrics); None reason means within tolerance."""
    if cand is None or ref is None:
        side = "candidate" if cand is None else "reference"
        return f"{key}: {side} value missing", {}
    if cand.units != ref.units:
        return f"{key}: units differ ({cand.units} vs {ref.units})", {}
    a, b = _decimal(cand.value), _decimal(ref.value)
    if a is None or b is None:
        return f"{key}: value is not a finite decimal", {}
    diff = _exact_diff(a, b)
    metrics = {f"diff.{key}": _fmt(diff), f"diff.{key}.units": cand.units}
    if policy.tolerance is None:
        return f"tolerance unresolved ({policy.tolerance_source or 'A6'})", metrics
    if cand.units != policy.tolerance_units:
        return f"{key}: units {cand.units} differ from tolerance units", metrics
    if abs(diff) > Decimal(policy.tolerance):
        return "out-of-tolerance", metrics
    return None, metrics


def _reference_comparison(
    obs: OrcaObservation, ref: OrcaObservation, policy: ComparisonPolicy
) -> Checked:
    metrics: dict[str, str] = {}
    discrepancies: list[str] = []  # definite: the check fails
    blockers: list[str] = []  # no numeric judgement possible: unresolved
    out_of_tolerance = False

    if "excited_states.energy_au" in policy.quantities:
        reasons = [
            (side, _unattributable(o)) for side, o in (("candidate", obs), ("reference", ref))
        ]
        reasons = [f"{side}: {r}" for side, r in reasons if r is not None]
        if reasons:
            blockers.extend(reasons)
        else:
            cand, refs = _by_multiplicity(obs.excited_states), _by_multiplicity(ref.excited_states)
            unpaired_c: list[str] = []
            unpaired_r: list[str] = []
            for m in sorted(set(cand) | set(refs)):
                cs, rs = cand.get(m, []), refs.get(m, [])
                for rank, (c, r) in enumerate(zip(cs, rs, strict=False), start=1):
                    tag = f"m{m}.r{rank}"
                    metrics[f"pair.{tag}"] = (
                        f"candidate root {c.root} sym {c.symmetry or '-'}; "
                        f"reference root {r.root} sym {r.symmetry or '-'}"
                    )
                    if c.symmetry and r.symmetry and c.symmetry != r.symmetry:
                        discrepancies.append(
                            f"{tag}: symmetry {c.symmetry} vs reference {r.symmetry}"
                        )
                    reason, qm = _compare_quantity(
                        f"energy_au.{tag}", c.energy_au, r.energy_au, policy
                    )
                    metrics.update(qm)
                    if reason == "out-of-tolerance":
                        out_of_tolerance = True
                    elif reason is not None:
                        blockers.append(reason)
                unpaired_c += [f"m{m}.root{s.root}" for s in cs[len(rs) :]]
                unpaired_r += [f"m{m}.root{s.root}" for s in rs[len(cs) :]]
            if unpaired_c or unpaired_r:
                metrics["unpaired.candidate"] = ",".join(unpaired_c) or "-"
                metrics["unpaired.reference"] = ",".join(unpaired_r) or "-"
                discrepancies.append("unpaired states")

    if "final_energy" in policy.quantities:
        reason, qm = _compare_quantity("final_energy", obs.final_energy, ref.final_energy, policy)
        metrics.update(qm)
        if reason == "out-of-tolerance":
            out_of_tolerance = True
        elif reason is not None:
            blockers.append(reason)

    symmetry = [d for d in discrepancies if "symmetry" in d]
    if symmetry:
        metrics["discrepancy.symmetry"] = "; ".join(symmetry)
    if discrepancies:
        metrics["reference_comparison.reason"] = "; ".join(discrepancies)
        return "fail", metrics
    if blockers:
        metrics["reference_comparison.reason"] = "; ".join(dict.fromkeys(blockers))
        return "unresolved", metrics
    if out_of_tolerance:
        metrics["reference_comparison.reason"] = "difference exceeds tolerance"
        return "fail", metrics
    return "pass", metrics


# ------------------------------------------------------------------ evaluate


def evaluate_observation(
    agent_store: Store,
    evaluator_store: Store,
    *,
    run_id: str,
    observation_digest: str,
    case: CaseManifest,
    policy: str,
    reference: str | None,
) -> Evaluation:
    """Evaluate one observation and record a new, distinct `Evaluation`.

    Reads the observation from `agent_store` (read only) and the reference
    from `evaluator_store`; writes only to `evaluator_store`. Re-evaluating
    creates another record; an earlier one is never touched.
    """
    if policy not in POLICIES:
        raise CapabilityError(f"unknown comparison policy {policy!r}")
    pol = POLICIES[policy]
    if "agent" in evaluator_store.allowed_visibility:
        raise CapabilityError("evaluator store must not accept agent-visible artifacts")
    if evaluator_store.root.resolve() == agent_store.root.resolve():
        raise CapabilityError("evaluator store and agent store must be distinct")

    obs = OrcaObservation.model_validate(agent_store.get_json(observation_digest))

    checks = {
        "execution_status": _execution_status(obs),
        "parse_integrity": _parse_integrity(obs),
        "state_identification": _state_identification(obs, case),
    }
    reference_version = "none"
    metrics: dict[str, str] = {
        "case.id": case.case_id,
        "case.review": case.review.status,
        "observation.synthetic": "true" if obs.synthetic else "false",
        "policy": pol.policy_id,
        "policy.kind": pol.kind,
        "policy.tolerance": pol.tolerance if pol.tolerance is not None else "unresolved",
    }
    if pol.tolerance_source:
        metrics["policy.tolerance_source"] = pol.tolerance_source
    if pol.tolerance_units:
        metrics["policy.tolerance_units"] = pol.tolerance_units

    if reference is not None:
        if evaluator_store.descriptor(reference).visibility != "evaluator":
            raise CapabilityError(f"reference {reference} is not an evaluator-visibility object")
        payload = evaluator_store.get_json(reference)
        if payload.get("schema") != REFERENCE_SCHEMA:
            raise CapabilityError(f"reference {reference} is not a case reference")
        ref_obs = OrcaObservation.model_validate(payload["observation"])
        reference_version = payload["reference_version"]
        metrics["reference.case_id"] = payload["case_id"]
        metrics["reference.synthetic"] = "true" if ref_obs.synthetic else "false"
        checks["reference_comparison"] = _reference_comparison(obs, ref_obs, pol)

    assignments: dict[str, str] = {}
    for name, (verdict, check_metrics) in checks.items():
        assignments[name] = verdict
        metrics.update(check_metrics)

    evaluation = Evaluation.new(
        evaluation_id=_next_evaluation_id(evaluator_store, run_id, EVALUATOR, EVALUATOR_VERSION),
        run_id=run_id,
        evaluator=EVALUATOR,
        evaluator_version=EVALUATOR_VERSION,
        output_refs={"observation": observation_digest},
        reference_version=reference_version,
        assignments=assignments,
        unresolved=[name for name, v in assignments.items() if v == "unresolved"],
        metrics=metrics,
        usage=Usage(units={}, measured=False, simulated=True, cost_usd="0.00"),
        created_at=evaluator_store.clock.now(),
    )
    evaluator_store.put_record(evaluation)
    return evaluation
