"""Task bundle staging and claim grading (M3-005)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from onoda.adapters.pi.contract import Claim, ClaimedQuantity, ClaimedState, TaskBundle
from onoda.adapters.pi.task import (
    CLAIM_EVALUATOR,
    CLAIM_EVALUATOR_VERSION,
    evaluate_claims,
    stage_bundle,
)
from onoda.canonical import sha256_digest
from onoda.clock import LogicalClock
from onoda.errors import OnodaError
from onoda.science.contract import case_manifests, load_case
from onoda.science.orca_parser import PARSER_ID, parse_orca_output
from onoda.store import Store

CASES = case_manifests()


def _load(case_id: str):
    return load_case(CASES[case_id])


def _output_bytes(case) -> bytes:
    ((_, data),) = case.output_bytes.items()
    return data


def _claim_state(state, **overrides) -> ClaimedState:
    fields = dict(
        root=state.root,
        multiplicity=state.multiplicity,
        energy=ClaimedQuantity(text=state.energy_au.value, unit=state.energy_au.units),
    )
    fields.update(overrides)
    return ClaimedState(**fields)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


# ------------------------------------------------------------------ stage_bundle


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_stage_bundle_hashes_match_manifest_and_no_reference(tmp_path, case_id):
    case = _load(case_id)
    dest = tmp_path / case_id
    bundle = stage_bundle(case, dest, instruction="report every requested state")

    assert bundle.case_id == case.manifest.case_id
    assert bundle.input_sha256 == case.manifest.input.sha256
    ((_, pinned),) = case.manifest.outputs.items()
    assert bundle.output_sha256 == pinned.sha256
    assert sha256_digest((dest / "input" / bundle.input_name).read_bytes()) == bundle.input_sha256
    assert (
        sha256_digest((dest / "output" / bundle.output_name).read_bytes()) == bundle.output_sha256
    )
    assert bundle.output_provenance == "acquired"
    assert bundle.docs == ()
    assert bundle.ladder_sha256 is None

    # No evaluator-visibility data (reference observation, evaluator metrics) is staged.
    written = _tree(dest)
    assert set(written) == {
        "task.json",
        f"input/{bundle.input_name}",
        f"output/{bundle.output_name}",
    }
    assert b"reference" not in written["task.json"]


def test_stage_bundle_refuses_nonempty_dest(tmp_path):
    case = _load("orca6-dvb-tddft")
    dest = tmp_path / "bundle"
    dest.mkdir()
    (dest / "stray").write_text("x")
    with pytest.raises(OnodaError):
        stage_bundle(case, dest, instruction="go")


def test_stage_bundle_writes_docs_and_ladder(tmp_path):
    case = _load("orca6-dvb-tddft")
    dest = tmp_path / "bundle"
    docs = {"method.md": b"# method notes", "policy.md": b"# policy"}
    ladder = b"# advisory ladder\n1. read input\n2. read output\n"
    bundle = stage_bundle(
        case, dest, instruction="go", docs=docs, ladder=ladder, synthetic_ladder=True
    )

    assert bundle.docs == ("method.md", "policy.md")
    assert bundle.synthetic_ladder is True
    assert bundle.ladder_sha256 == sha256_digest(ladder)
    assert (dest / "ladder.md").read_bytes() == ladder
    for name, content in docs.items():
        assert (dest / "docs" / name).read_bytes() == content


def test_stage_bundle_synthetic_ladder_without_bytes_is_rejected(tmp_path):
    case = _load("orca6-dvb-tddft")
    with pytest.raises(ValueError):
        stage_bundle(case, tmp_path / "bundle", instruction="go", synthetic_ladder=True)


def test_stage_bundle_rejects_non_markdown_docs(tmp_path):
    case = _load("orca6-dvb-tddft")
    with pytest.raises(OnodaError):
        stage_bundle(case, tmp_path / "bundle", instruction="go", docs={"notes.txt": b"x"})


@pytest.mark.parametrize(
    "name", ["/tmp/escaped.md", "../outside.md", "../../outside.md", "sub/dir.md", ".hidden.md"]
)
def test_stage_bundle_refuses_doc_names_that_are_paths(tmp_path, name):
    case = _load("orca6-dvb-tddft")
    dest = tmp_path / "a" / "bundle"
    with pytest.raises(OnodaError, match="plain"):
        stage_bundle(case, dest, instruction="go", docs={name: b"x"})
    assert not dest.exists()  # refused before anything was written
    assert not (tmp_path / "a" / "outside.md").exists()
    assert not (tmp_path / "outside.md").exists()


@pytest.mark.parametrize("instruction", ["@/etc/passwd", "-p", "--help"])
def test_stage_bundle_refuses_instruction_pi_would_misread(tmp_path, instruction):
    case = _load("orca6-dvb-tddft")
    with pytest.raises(OnodaError, match="instruction"):
        stage_bundle(case, tmp_path / "bundle", instruction=instruction)
    with pytest.raises(ValueError):  # the contract refuses it too
        TaskBundle.model_validate(
            {
                **stage_bundle(case, tmp_path / "ok", instruction="go").model_dump(by_alias=True),
                "instruction": instruction,
            }
        )


# ------------------------------------------------------------------ evaluate_claims


@pytest.fixture
def evaluator_store(tmp_path) -> Store:
    return Store(tmp_path / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",))


def test_supported_claim_matches_every_requested_state(evaluator_store):
    case = _load("orca6-dvb-tddft")
    obs = parse_orca_output(_output_bytes(case))
    claim = Claim(
        case_id=case.manifest.case_id,
        final_energy=ClaimedQuantity(text=obs.final_energy.value, unit=obs.final_energy.units),
        states=tuple(_claim_state(s) for s in obs.excited_states),
    )
    e = evaluate_claims(
        evaluator_store, run_id="r1", case=case, claim=claim, claim_ref="sha256:" + "0" * 64
    )

    assert e.evaluator == CLAIM_EVALUATOR
    assert e.evaluator_version == CLAIM_EVALUATOR_VERSION
    assert e.reference_version == f"{sha256_digest(_output_bytes(case))}:{PARSER_ID}"
    assert set(e.assignments.values()) == {"supported"}
    assert e.metrics["count.supported"] == str(len(obs.excited_states) + 1)
    assert e.metrics["false_success_claims"] == "0"
    assert e.unresolved == []
    assert e.output_refs["claim"] == "sha256:" + "0" * 64
    assert evaluator_store.has_record("evaluation", e.evaluation_id)


def test_contradicted_energy_value(evaluator_store):
    case = _load("orca6-dvb-tddft")
    obs = parse_orca_output(_output_bytes(case))
    target = obs.excited_states[0]
    perturbed_text = str(Decimal(target.energy_au.value) + Decimal("0.5"))
    states = [_claim_state(s) for s in obs.excited_states]
    states[0] = _claim_state(
        target, energy=ClaimedQuantity(text=perturbed_text, unit=target.energy_au.units)
    )
    claim = Claim(case_id=case.manifest.case_id, states=tuple(states))
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=claim, claim_ref=None)

    tag = f"m{target.multiplicity}.r{target.root}"
    assert e.assignments[tag] == "contradicted"
    assert e.metrics["count.contradicted"] == "1"
    assert e.metrics["false_success_claims"] == "1"


def test_contradicted_unit_mismatch(evaluator_store):
    case = _load("orca6-dvb-tddft")
    obs = parse_orca_output(_output_bytes(case))
    target = obs.excited_states[0]
    states = [_claim_state(s) for s in obs.excited_states]
    states[0] = _claim_state(target, energy=ClaimedQuantity(text=target.energy_au.value, unit="eV"))
    claim = Claim(case_id=case.manifest.case_id, states=tuple(states))
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=claim, claim_ref=None)

    tag = f"m{target.multiplicity}.r{target.root}"
    assert e.assignments[tag] == "contradicted"
    assert e.metrics[f"state.{tag}.reason"] == "unit"


def test_fabricated_root_is_unsupported(evaluator_store):
    case = _load("orca6-dvb-tddft")
    obs = parse_orca_output(_output_bytes(case))
    fabricated = ClaimedState(
        root=99, multiplicity=1, energy=ClaimedQuantity(text="0.999999", unit="au")
    )
    claim = Claim(
        case_id=case.manifest.case_id,
        states=(*[_claim_state(s) for s in obs.excited_states], fabricated),
    )
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=claim, claim_ref=None)

    assert e.assignments["m1.r99"] == "unsupported"
    assert e.metrics["count.unsupported"] == "1"
    assert e.metrics["false_success_claims"] == "1"


def test_omitted_states_are_missing(evaluator_store):
    case = _load("orca6-dvb-tddft")
    obs = parse_orca_output(_output_bytes(case))
    kept = [s for s in obs.excited_states if not (s.multiplicity == 1 and s.root == 1)]
    claim = Claim(
        case_id=case.manifest.case_id,
        final_energy=ClaimedQuantity(text=obs.final_energy.value, unit=obs.final_energy.units),
        states=tuple(_claim_state(s) for s in kept),
    )
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=claim, claim_ref=None)

    assert e.assignments["m1.r1"] == "missing"
    assert e.metrics["count.missing"] == "1"
    assert e.assignments["final_energy"] == "supported"


def test_no_claim_makes_every_requested_state_and_final_energy_missing(evaluator_store):
    case = _load("orca6-dvb-tddft")
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=None, claim_ref=None)

    expected = sum(case.manifest.request.roots_by_multiplicity.values()) + 1  # + final_energy
    assert e.metrics["count.missing"] == str(expected)
    assert set(e.assignments.values()) == {"missing"}
    assert e.metrics["claim.present"] == "false"


def test_unknown_multiplicity_is_unresolved_not_guessed(evaluator_store):
    # ADC(2)/EOM-CCSD output never prints multiplicity (A28); a claim naming one
    # cannot be paired with confidence, and an omitted request is unresolved too.
    case = _load("orca6-dvb-adc2")
    obs = parse_orca_output(_output_bytes(case))
    assert all(s.multiplicity is None for s in obs.excited_states)

    claimed = _claim_state(obs.excited_states[0], multiplicity=1)
    claim = Claim(case_id=case.manifest.case_id, states=(claimed,))
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=claim, claim_ref=None)

    assert e.assignments["m1.r1"] == "unresolved"
    assert e.assignments["m1.r2"] == "unresolved"  # requested, omitted, and unattributable
    assert "m1.r1" in e.unresolved and "m1.r2" in e.unresolved
    assert e.metrics["count.unresolved"] == "2"


def test_final_energy_matches_parsed_final_single_point_energy(evaluator_store):
    case = _load("orca6-water-mp2")  # no excited states requested; only final_energy
    obs = parse_orca_output(_output_bytes(case))
    claim = Claim(
        case_id=case.manifest.case_id,
        final_energy=ClaimedQuantity(text=obs.final_energy.value, unit=obs.final_energy.units),
    )
    e = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=claim, claim_ref=None)
    assert e.assignments == {"final_energy": "supported"}

    contradicted = evaluate_claims(
        evaluator_store,
        run_id="r2",
        case=case,
        claim=Claim(
            case_id=case.manifest.case_id,
            final_energy=ClaimedQuantity(text="-1.000000", unit=obs.final_energy.units),
        ),
        claim_ref=None,
    )
    assert contradicted.assignments == {"final_energy": "contradicted"}


def test_evaluation_written_only_to_evaluator_store(tmp_path):
    agent_only = Store(tmp_path / "agent", clock=LogicalClock())
    evaluator = Store(
        tmp_path / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",)
    )
    case = _load("orca6-dvb-tddft")

    e = evaluate_claims(evaluator, run_id="r1", case=case, claim=None, claim_ref=None)

    assert evaluator.has_record("evaluation", e.evaluation_id)
    assert not agent_only.list_record_ids("evaluation")


def test_reevaluation_creates_a_new_record(evaluator_store):
    case = _load("orca6-dvb-tddft")
    first = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=None, claim_ref=None)
    second = evaluate_claims(evaluator_store, run_id="r1", case=case, claim=None, claim_ref=None)
    assert first.evaluation_id != second.evaluation_id
    assert evaluator_store.has_record("evaluation", first.evaluation_id)
    assert evaluator_store.has_record("evaluation", second.evaluation_id)
