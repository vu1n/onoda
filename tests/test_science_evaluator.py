"""External ORCA evaluator skeleton (M2-004).

Every observation here is test-constructed from the contract models and
labelled `synthetic=True`; the values are plausible in form but invented, and
are not ORCA results.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from labrun.canonical import sha256_digest
from labrun.clock import LogicalClock
from labrun.errors import CapabilityError, VisibilityError
from labrun.science import evaluator as ev
from labrun.science.contract import (
    CaseManifest,
    Evidence,
    ExcitedState,
    OrcaObservation,
    Problem,
    Quantity,
    case_manifests,
    load_case,
)
from labrun.science.evaluator import (
    POLICIES,
    ComparisonPolicy,
    evaluate_observation,
    publish_case_reference,
)
from labrun.store import Store

SOURCE = sha256_digest(b"test-constructed stand-in output, not an ORCA run")


def _ev(line: int = 1) -> Evidence:
    return Evidence(artifact=SOURCE, line=line, text="test-constructed line")


def _q(value: str, units: str = "au") -> Quantity:
    return Quantity(value=value, units=units, evidence=_ev())


def _state(root: int, mult: int | None, energy: str, sym: str | None = None) -> ExcitedState:
    return ExcitedState(root=root, multiplicity=mult, symmetry=sym, energy_au=_q(energy))


def _states(base: str, *, sym: bool = True, mults=(1, 3), n: int = 5) -> list[ExcitedState]:
    """n roots per multiplicity at base + 0.01*(root) (+0.1 for triplets)."""
    out = []
    for m in mults:
        for r in range(1, n + 1):
            offset = f"0.{r:02d}" if m == 1 else f"0.{10 + r:02d}"
            energy = _add(base, offset)
            out.append(_state(r, m, energy, ("Au" if r % 2 else "Bg") if sym else None))
    return out


def _add(a: str, b: str) -> str:
    return str(Decimal(a) + Decimal(b))


def obs(**kw) -> OrcaObservation:
    fields = dict(
        parser="test-constructed",
        source=SOURCE,
        synthetic=True,
        program_version="test",
        termination="normal",
        scf="converged",
        scf_cycles=12,
        final_energy=_q("-100.000000000000", "Eh"),
        method_family="tddft",
        excited_states_status="present",
        excited_states=_states("0.100000"),
    )
    fields.update(kw)
    return OrcaObservation.model_validate(fields)


@pytest.fixture
def case() -> CaseManifest:
    return load_case(case_manifests()["orca6-dvb-tddft"]).manifest


@pytest.fixture
def stores(tmp_path: Path) -> tuple[Store, Store]:
    agent = Store(tmp_path / "agent", clock=LogicalClock())
    evaluator = Store(
        tmp_path / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",)
    )
    return agent, evaluator


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def run(stores, case, candidate, *, reference=None, policy="orca-reexecution@1", run_id="r1"):
    agent, evaluator = stores
    digest = agent.put_json(candidate.to_json(), labels=["synthetic"]).digest
    ref_digest = None
    if reference is not None:
        ref_digest = publish_case_reference(
            evaluator,
            case_id=case.case_id,
            observation=reference,
            reference_version="test-ref@1",
            provenance={"origin": "test-constructed"},
        )
    return evaluate_observation(
        agent,
        evaluator,
        run_id=run_id,
        observation_digest=digest,
        case=case,
        policy=policy,
        reference=ref_digest,
    )


# ------------------------------------------------------------------ policy


def test_tolerance_without_source_is_rejected():
    with pytest.raises(ValidationError):
        ComparisonPolicy(
            policy_id="x@1", kind="reexecution", tolerance="0.001", tolerance_units="au"
        )
    with pytest.raises(ValidationError):
        ComparisonPolicy(policy_id="x@1", kind="reexecution", tolerance="-1", tolerance_source="T")


def test_registered_reexecution_policy_has_no_tolerance():
    pol = POLICIES["orca-reexecution@1"]
    assert pol.kind == "reexecution" and pol.tolerance is None and pol.tolerance_source == "A6"


# ------------------------------------------------------------------ comparison


def test_unresolved_tolerance_reports_exact_differences(stores, case):
    reference = obs()
    shifted = [
        s.model_copy(update={"energy_au": _q(_add(s.energy_au.value, "0.000006"))})
        for s in reference.excited_states
    ]
    candidate = obs(excited_states=shifted, final_energy=_q("-100.000007562582", "Eh"))
    e = run(stores, case, candidate, reference=reference)

    assert e.assignments["reference_comparison"] == "unresolved"
    assert "reference_comparison" in e.unresolved
    assert e.metrics["reference_comparison.reason"] == "tolerance unresolved (A6)"
    assert e.metrics["diff.energy_au.m1.r1"] == "0.000006"
    assert e.metrics["diff.energy_au.m3.r5"] == "0.000006"
    assert e.metrics["diff.final_energy"] == "-0.000007562582"
    assert e.metrics["diff.final_energy.units"] == "Eh"
    assert e.reference_version == "test-ref@1"
    assert all(isinstance(v, str) for v in e.metrics.values())


def test_tolerance_policy_passes_and_fails(stores, case, monkeypatch):
    pol = ComparisonPolicy(
        policy_id="test-tolerance@1",
        kind="reexecution",
        quantities=("excited_states.energy_au",),
        tolerance="0.00001",
        tolerance_units="au",
        tolerance_source="TEST-ONLY",
    )
    monkeypatch.setitem(ev.POLICIES, pol.policy_id, pol)
    reference = obs()

    def shifted(delta: str) -> OrcaObservation:
        return obs(
            excited_states=[
                s.model_copy(update={"energy_au": _q(_add(s.energy_au.value, delta))})
                for s in reference.excited_states
            ]
        )

    ok = run(stores, case, shifted("0.00001"), reference=reference, policy=pol.policy_id)
    assert ok.assignments["reference_comparison"] == "pass"
    bad = run(stores, case, shifted("-0.00002"), reference=reference, policy=pol.policy_id)
    assert bad.assignments["reference_comparison"] == "fail"
    assert bad.metrics["diff.energy_au.m1.r1"] == "-0.000020"


def test_symmetry_disagreement_is_flagged(stores, case):
    reference = obs()
    states = list(reference.excited_states)
    states[1] = states[1].model_copy(update={"symmetry": "Au"})  # reference says Bg
    e = run(stores, case, obs(excited_states=states), reference=reference)
    assert e.assignments["reference_comparison"] == "fail"
    assert "m1.r2: symmetry Au vs reference Bg" in e.metrics["discrepancy.symmetry"]


def test_symmetry_missing_on_one_side_is_not_a_disagreement(stores, case):
    e = run(stores, case, obs(excited_states=_states("0.100000", sym=False)), reference=obs())
    assert "discrepancy.symmetry" not in e.metrics
    assert e.assignments["reference_comparison"] == "unresolved"  # tolerance only


def test_pairing_is_by_root_order_not_energy(stores, case):
    reference = obs()
    # Candidate swaps the energies of singlet roots 1 and 2; pairing keeps root order.
    states = list(reference.excited_states)
    e1, e2 = states[0].energy_au, states[1].energy_au
    states[0] = states[0].model_copy(update={"energy_au": e2})
    states[1] = states[1].model_copy(update={"energy_au": e1})
    e = run(stores, case, obs(excited_states=states), reference=reference)
    assert e.metrics["diff.energy_au.m1.r1"] == "0.010000"
    assert e.metrics["diff.energy_au.m1.r2"] == "-0.010000"


def test_count_mismatch_fails_and_lists_unpaired_states(stores, case):
    reference = obs()
    candidate = obs(excited_states=[s for s in reference.excited_states if s.root <= 4])
    e = run(stores, case, candidate, reference=reference)
    assert e.assignments["state_identification"] == "fail"
    assert "m1:expected=5,observed=4" in e.metrics["state_identification.reason"]
    assert e.assignments["reference_comparison"] == "fail"
    assert e.metrics["unpaired.reference"] == "m1.root5,m3.root5"
    assert e.metrics["unpaired.candidate"] == "-"


def test_units_mismatch_is_unresolved_not_pass(stores, case):
    reference = obs()
    candidate = obs(final_energy=_q("-100.000000000000", "kcal/mol"))
    e = run(stores, case, candidate, reference=reference)
    assert e.assignments["reference_comparison"] == "unresolved"
    assert "units differ" in e.metrics["reference_comparison.reason"]
    assert "diff.final_energy" not in e.metrics


# ------------------------------------------------------------------ single-observation checks


def test_well_formed_synthetic_observation(stores, case):
    e = run(stores, case, obs())
    # a synthetic observation is not a solver run, so execution never passes either.
    assert e.assignments["execution_status"] == "unresolved"
    assert e.assignments["state_identification"] == "pass"
    # synthetic never passes parse integrity, and the record says it is synthetic.
    assert e.assignments["parse_integrity"] == "unresolved"
    assert e.metrics["observation.synthetic"] == "true"
    assert e.metrics["case.review"] == "pending-human-review"
    assert e.output_refs["observation"].startswith("sha256:")
    assert e.reference_version == "none"
    assert "reference_comparison" not in e.assignments


def test_missing_termination_and_final_energy_are_unresolved(stores, case):
    candidate = obs(
        termination="absent",
        final_energy=None,
        problems=[
            Problem(field="termination", state="missing", detail="no termination line"),
            Problem(field="final_energy", state="missing", detail="no final energy line"),
        ],
    )
    e = run(stores, case, candidate, reference=obs())
    assert e.assignments["execution_status"] == "unresolved"
    assert e.assignments["parse_integrity"] == "unresolved"
    assert e.assignments["reference_comparison"] == "unresolved"
    assert "final_energy: candidate value missing" in e.metrics["reference_comparison.reason"]
    assert {"execution_status", "parse_integrity", "reference_comparison"} <= set(e.unresolved)


def test_error_termination_fails_execution(stores, case):
    e = run(stores, case, obs(termination="error"))
    assert e.assignments["execution_status"] == "fail"
    e = run(stores, case, obs(scf="not_converged"), run_id="r2")
    assert e.assignments["execution_status"] == "fail"


def test_error_problem_fails_parse_integrity(stores, case):
    candidate = obs(problems=[Problem(field="scf", state="error", detail="garbled block")])
    e = run(stores, case, candidate)
    assert e.assignments["parse_integrity"] == "fail"


def test_ambiguous_states_are_unresolved(stores, case):
    candidate = obs(excited_states_status="ambiguous")
    e = run(stores, case, candidate, reference=obs())
    assert e.assignments["state_identification"] == "unresolved"
    assert e.assignments["reference_comparison"] == "unresolved"


def test_states_without_multiplicity_are_unresolved(stores, case):
    states = [s.model_copy(update={"multiplicity": None}) for s in obs().excited_states]
    e = run(stores, case, obs(excited_states=states))
    assert e.assignments["state_identification"] == "unresolved"
    assert "multiplicity" in e.metrics["state_identification.reason"]


def test_absent_but_requested_states_are_unresolved(stores, case):
    e = run(stores, case, obs(excited_states_status="absent", excited_states=[]))
    assert e.assignments["state_identification"] == "unresolved"


# ------------------------------------------------------------------ storage


def test_reference_publish_to_agent_store_raises(stores, case):
    agent, _ = stores
    with pytest.raises(VisibilityError):
        publish_case_reference(
            agent,
            case_id=case.case_id,
            observation=obs(),
            reference_version="test-ref@1",
            provenance={},
        )


def test_evaluator_store_accepting_agent_artifacts_is_refused(tmp_path, case):
    agent = Store(tmp_path / "agent", clock=LogicalClock())
    loose = Store(
        tmp_path / "loose", clock=LogicalClock(), allowed_visibility=("agent", "evaluator")
    )
    digest = agent.put_json(obs().to_json()).digest
    with pytest.raises(CapabilityError):
        evaluate_observation(
            agent,
            loose,
            run_id="r1",
            observation_digest=digest,
            case=case,
            policy="orca-reexecution@1",
            reference=None,
        )


def test_reevaluation_creates_new_record_and_never_touches_agent_store(stores, case):
    agent, evaluator = stores
    digest = agent.put_json(obs().to_json(), labels=["synthetic"]).digest
    ref = publish_case_reference(
        evaluator,
        case_id=case.case_id,
        observation=obs(),
        reference_version="test-ref@1",
        provenance={"origin": "test-constructed"},
    )
    before = _tree(agent.root)
    kwargs = dict(run_id="r1", observation_digest=digest, case=case, policy="orca-reexecution@1")

    first = evaluate_observation(agent, evaluator, reference=ref, **kwargs)
    first_path = evaluator.root / "records" / "evaluation"
    first_bytes = _tree(first_path)
    second = evaluate_observation(agent, evaluator, reference=ref, **kwargs)

    assert first.evaluation_id != second.evaluation_id
    assert evaluator.has_record("evaluation", first.evaluation_id)
    assert evaluator.has_record("evaluation", second.evaluation_id)
    after = _tree(first_path)
    assert all(after[name] == data for name, data in first_bytes.items())
    assert len(after) == len(first_bytes) + 1
    assert _tree(agent.root) == before
    assert not agent.list_record_ids("evaluation")


def test_policy_must_compare_something():
    with pytest.raises(ValidationError):
        ComparisonPolicy(policy_id="x@1", kind="reexecution", quantities=())


def test_failed_execution_receipt_fails_execution_status(stores, case):
    agent, evaluator = stores
    digest = agent.put_json(obs(synthetic=False).to_json()).digest
    e = evaluate_observation(
        agent,
        evaluator,
        run_id="r1",
        observation_digest=digest,
        case=case,
        policy="orca-reexecution@1",
        reference=None,
        receipt_status="failed",
    )
    assert e.assignments["execution_status"] == "fail"
    assert e.metrics["execution.receipt_status"] == "failed"
