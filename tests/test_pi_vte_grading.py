"""VTE bundle staging and identity grading (VTE-006, A57/A58).

Grading runs on real ORCA 6.1.1 outputs (`tests/fixtures/orca611-nevpt2/`)
against the dev QUESTDB cases (`cases-dev/questdb/`): ketene's 1B1(pi,3s) is
the only singlet B1 row, and acetone has two singlet B2 rows, so its
`acetone_g_trah` job (a different CASSCF solution from QUESTDB's; see the
fixture README) exercises character disambiguation and the energy-order rule.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from onoda.adapters.pi.vte import (
    VTE_EVALUATOR,
    VTE_EVALUATOR_VERSION,
    VteJob,
    evaluate_vte_claims,
    stage_vte_bundle,
)
from onoda.adapters.pi.vte_contract import ClaimedVte, VteClaim, VteTaskBundle
from onoda.canonical import sha256_digest
from onoda.clock import LogicalClock
from onoda.errors import CapabilityError, IntegrityError, OnodaError
from onoda.science.orca_parser import PARSER_ID
from onoda.science.vte import load_vte_case, load_vte_references, vte_case_manifests
from onoda.store import Store

REPO = Path(__file__).resolve().parent.parent
CASES = vte_case_manifests(REPO / "cases-dev" / "questdb")
FIXTURES = REPO / "tests" / "fixtures" / "orca611-nevpt2"


def _case(name: str):
    return load_vte_case(CASES[f"questdb-{name}"])


def _out(name: str) -> bytes:
    return (FIXTURES / f"{name}.out").read_bytes()


def _state(spin: int, irrep: str, energy: str, job_id: str, character: str = "") -> ClaimedVte:
    return ClaimedVte.model_validate(
        {
            "spin": spin,
            "irrep": irrep,
            "character": character,
            "energy": {"text": energy, "unit": "eV"},
            "job_id": job_id,
        }
    )


def _claim(case, *states: ClaimedVte) -> VteClaim:
    return VteClaim(case_id=case.manifest.case_id, states=states)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def evaluator_store(tmp_path):
    return Store(tmp_path / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",))


def _grade(store, case, claim, jobs, **kw):
    return evaluate_vte_claims(
        store,
        run_id="r1",
        case=case,
        references=load_vte_references(case),
        claim=claim,
        jobs=jobs,
        **kw,
    )


# ------------------------------------------------------------------ stage_vte_bundle


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_stage_writes_structure_and_targets_and_no_reference(tmp_path, case_id):
    case = load_vte_case(CASES[case_id])
    refs = load_vte_references(case)
    dest = tmp_path / "bundle"
    bundle = stage_vte_bundle(case, dest, instruction="Compute the target states.")

    m = case.manifest
    name = Path(m.structure.path).name
    tree = _tree(dest)
    assert sorted(tree) == ["structure/" + name, "targets.json", "task.json"]
    assert sha256_digest(tree["structure/" + name]) == m.structure.sha256
    assert VteTaskBundle.model_validate_json(tree["task.json"]) == bundle
    assert bundle.targets == m.targets and bundle.structure_sha256 == m.structure.sha256
    assert json.loads(tree["targets.json"])["targets"] == [
        t.model_dump(mode="json") for t in m.targets
    ]

    # Nothing from the evaluator-only references file reaches the bundle.
    staged = b"\n".join(tree.values())
    ref_bytes = (case.root / m.references.path).read_bytes()
    assert ref_bytes not in staged
    assert m.references.sha256.encode() not in staged
    assert refs.method.encode() not in staged and m.method.encode() not in staged
    for row in refs.rows:
        assert row.label.encode() not in staged
        assert f"({row.character})".encode() not in staged
        assert row.energy_ev.encode() not in tree["task.json"] + tree["targets.json"]


def test_stage_writes_docs_and_ladder(tmp_path):
    case = _case("ketene")
    bundle = stage_vte_bundle(
        case,
        tmp_path / "b",
        instruction="Compute the target states.",
        docs={"orca-notes.md": b"# notes\n"},
        ladder=b"# ladder\n",
        max_executions=8,
    )
    assert (tmp_path / "b" / "docs" / "orca-notes.md").read_bytes() == b"# notes\n"
    assert (tmp_path / "b" / "ladder.md").read_bytes() == b"# ladder\n"
    assert bundle.docs == ("orca-notes.md",)
    assert bundle.ladder_sha256 == sha256_digest(b"# ladder\n")
    assert bundle.max_executions == 8 and not bundle.synthetic_ladder


@pytest.mark.parametrize(
    "kwargs",
    [
        {"instruction": "@task.json"},
        {"instruction": "go", "docs": {"../x.md": b""}},
        {"instruction": "go", "docs": {"notes.txt": b""}},
        {"instruction": "go", "synthetic_ladder": True},
    ],
)
def test_stage_refusals_write_nothing(tmp_path, kwargs):
    with pytest.raises((OnodaError, ValueError)):
        stage_vte_bundle(_case("ketene"), tmp_path / "b", **kwargs)
    assert not (tmp_path / "b").exists()


def test_stage_refuses_nonempty_dest(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("x")
    with pytest.raises(OnodaError, match="not empty"):
        stage_vte_bundle(_case("ketene"), tmp_path / "b", instruction="go")


def test_stage_refuses_drifted_structure_bytes(tmp_path):
    case = _case("ketene")
    drifted = case.model_copy(update={"structure_bytes": case.structure_bytes + b"\n"})
    with pytest.raises(IntegrityError):
        stage_vte_bundle(drifted, tmp_path / "b", instruction="go")


# ------------------------------------------------------------------ value verdicts


KETENE_JOBS = {"j0001": VteJob(_out("ketene_f_target"), executed_here=True, cache_hit=False)}


def test_supported_claim_with_its_character_is_identified(evaluator_store):
    case = _case("ketene")
    claim = _claim(case, _state(1, "B1", "6.069", "j0001", "pi,3s"))
    e = _grade(evaluator_store, case, claim, KETENE_JOBS, claim_ref="sha256:" + "0" * 64)

    assert e.evaluator == VTE_EVALUATOR and e.evaluator_version == VTE_EVALUATOR_VERSION
    assert e.assignments["claim.01"] == "supported"
    assert e.assignments["claim.01.identity"] == "identified"
    assert e.metrics["claim.01.reference"] == "ref.1B1(pi,3s)"
    assert e.assignments["ref.1B1(pi,3s)"] == "identified"
    assert e.metrics["ref.1B1(pi,3s).error_ev"] == "0.002"  # 6.069 - QUESTDB 6.067
    assert e.metrics["mae_ev"] == "0.002000"
    assert e.metrics["ref.1B1(pi,3s).accuracy"] == "within"
    assert e.metrics["accurate"] == "1" and e.metrics["tolerance_ev"] == "0.3"
    assert e.metrics["tolerance_source"].startswith("doc://onoda/vte-energy-tolerance")
    assert e.metrics["coverage"] == "1" and e.metrics["coverage.total"] == "7"
    assert e.metrics["count.reference.missing"] == "6"
    assert e.metrics["false_success_claims"] == "0"
    assert e.metrics["case.review"] == "pending-human-review"
    assert e.metrics["claim.01.irrep_printed"] == "true"
    assert e.metrics["parser"] == PARSER_ID
    assert e.output_refs["job.j0001"] == sha256_digest(_out("ketene_f_target"))
    assert e.reference_version == f"{case.manifest.references.sha256}:{PARSER_ID}"
    assert evaluator_store.has_record("evaluation", e.evaluation_id)


@pytest.mark.parametrize("character", ["", "pi,pi*"])
def test_the_only_row_of_an_irrep_still_needs_its_character(evaluator_store, character):
    # qv5 (2026-10-05): a bare or wrong label on the lone row of its irrep was
    # credited with states several eV from the target.
    case = _case("ketene")
    claim = _claim(case, _state(1, "B1", "6.069", "j0001", character))
    e = _grade(evaluator_store, case, claim, KETENE_JOBS)
    assert e.assignments["claim.01"] == "supported"
    assert e.assignments["claim.01.identity"] == "unidentified"
    assert e.metrics["claim.01.identity_reason"] == "character"
    assert e.assignments["ref.1B1(pi,3s)"] == "unidentified"
    assert e.metrics["coverage"] == "0" and e.metrics["accurate"] == "0"


def test_cache_hit_supports_like_an_execution(evaluator_store):
    case = _case("ketene")
    jobs = {"j0002": (_out("ketene_f_target"), False, True)}  # plain tuples are accepted
    e = _grade(evaluator_store, case, _claim(case, _state(1, "B1", "6.069", "j0002")), jobs)
    assert e.assignments["claim.01"] == "supported"


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (_state(1, "B1", "6.070", "j0001"), "value"),
        (_state(1, "B1", "5.217", "j0001"), "casscf_value"),  # the CASSCF, not the NEVPT2 value
        (_state(3, "B1", "6.069", "j0001"), "no_state"),  # the job computed no triplet
        (_state(1, "A2", "6.069", "j0001"), "no_state"),  # printed under B1, claimed as A2
    ],
)
def test_contradicted_claims_are_false_successes(evaluator_store, state, reason):
    case = _case("ketene")
    e = _grade(evaluator_store, case, _claim(case, state), KETENE_JOBS)
    assert e.assignments["claim.01"] == "contradicted"
    assert e.metrics["claim.01.reason"] == reason
    assert "claim.01.identity" not in e.assignments
    assert e.metrics["false_success_claims"] == "1"
    assert e.metrics["coverage"] == "0" and e.metrics["mae_ev"] == "unresolved"


def test_decimal_equality_not_text_equality(evaluator_store):
    case = _case("ketene")
    e = _grade(evaluator_store, case, _claim(case, _state(1, "B1", "6.0690", "j0001")), KETENE_JOBS)
    assert e.assignments["claim.01"] == "supported"


@pytest.mark.parametrize(
    ("jobs", "reason"),
    [
        ({}, "job_not_in_run"),
        ({"j0001": VteJob(_out("ketene_f_target"), False, False)}, "job_not_executed"),
        (
            {"j0001": VteJob(_out("ethylene_nevpt2_np4_default_orbstep"), True, False)},
            "job_problem",
        ),
    ],
)
def test_unsupported_claims(evaluator_store, jobs, reason):
    case = _case("ketene")
    e = _grade(evaluator_store, case, _claim(case, _state(1, "B1", "6.069", "j0001")), jobs)
    assert e.assignments["claim.01"] == "unsupported"
    assert e.metrics["claim.01.reason"] == reason
    assert e.assignments["ref.1B1(pi,3s)"] == "missing"
    assert e.metrics["false_success_claims"] == "1"


def test_listing_not_from_a_singlet_ground_state_blocks_the_job(evaluator_store):
    # The parser keeps these rows but flags them; the grader fails closed on the job.
    marker = b"LOWEST ROOT (ROOT 0, MULT 1, IRREP  A1) =   -152.306748571"
    out = _out("ketene_f_target")
    assert out.count(marker) == 1
    jobs = {"j0001": VteJob(out.replace(marker, marker.replace(b"MULT 1", b"MULT 3")), True, False)}
    case = _case("ketene")
    e = _grade(evaluator_store, case, _claim(case, _state(1, "B1", "6.069", "j0001")), jobs)
    assert e.assignments["claim.01"] == "unsupported"
    assert e.metrics["claim.01.reason"] == "job_problem"


# ------------------------------------------------------------------ identity


ACETONE_JOBS = {"j0003": VteJob(_out("acetone_g_trah"), executed_here=True, cache_hit=False)}


def test_energy_order_alone_never_identifies(evaluator_store):
    # Two supported singlet B2 values, listed in the reference's energy order,
    # but with no character: QUESTDB has two 1B2 rows, so neither is identified.
    case = _case("acetone")
    claim = _claim(
        case, _state(1, "B2", "4.614", "j0003"), _state(1, "B2", "6.914", "j0003", "n,3d")
    )
    e = _grade(evaluator_store, case, claim, ACETONE_JOBS)
    assert [e.assignments[f"claim.0{i}"] for i in (1, 2)] == ["supported", "supported"]
    assert [e.assignments[f"claim.0{i}.identity"] for i in (1, 2)] == ["unidentified"] * 2
    assert e.metrics["claim.01.identity_reason"] == "character"
    assert e.assignments["ref.1B2(n,3s)"] == "unidentified"
    assert e.assignments["ref.1B2(n,3pz)"] == "unidentified"
    assert e.assignments["ref.1A2(n,pi*)"] == "missing"
    assert e.metrics["coverage"] == "0" and e.metrics["mae_ev"] == "unresolved"
    assert e.metrics["false_success_claims"] == "0"


def test_character_disambiguates_among_rows_of_one_irrep(evaluator_store):
    case = _case("acetone")
    claim = _claim(
        case,
        _state(1, "B2", "6.914", "j0003", "n,3s"),
        _state(1, "B2", "4.614", "j0003", "N, 3pz"),  # normalized: spaces and case
    )
    e = _grade(evaluator_store, case, claim, ACETONE_JOBS)
    assert e.metrics["claim.01.reference"] == "ref.1B2(n,3s)"
    assert e.metrics["claim.02.reference"] == "ref.1B2(n,3pz)"
    assert e.metrics["ref.1B2(n,3s).error_ev"] == "0.217"  # 6.914 - 6.697
    assert e.metrics["ref.1B2(n,3pz).error_ev"] == "-3.186"  # 4.614 - 7.800
    assert e.metrics["ref.1B2(n,3s).accuracy"] == "within"
    assert e.metrics["ref.1B2(n,3pz).accuracy"] == "outside"  # identified, but the wrong state
    assert e.metrics["coverage"] == "2" and e.metrics["accurate"] == "1"
    assert e.metrics["abs_error_sum_ev"] == "3.403"
    assert e.metrics["mae_ev"] == "1.701500"
    assert e.metrics["coverage"] == "2"
    assert Decimal(e.metrics["mae_ev"]) == Decimal("3.403") / 2


def test_two_claims_on_one_row_identify_neither(evaluator_store):
    case = _case("ketene")
    s = _state(1, "B1", "6.069", "j0001", "pi,3s")
    e = _grade(evaluator_store, case, _claim(case, s, s), KETENE_JOBS)
    assert e.assignments["claim.01.identity"] == "unidentified"
    assert e.metrics["claim.02.identity_reason"] == "conflict"
    assert e.assignments["ref.1B1(pi,3s)"] == "unidentified"


def _without_irreps(out: bytes) -> bytes:
    """The same output as ORCA prints it without symmetry: no IRREP column or label."""
    text = out.decode()
    text = text.replace("MULT IRREP", "MULT")
    text = re.sub(r"(MULT \d+), IRREP\s+\S+\)", r"\1)", text)
    text = re.sub(r"(?m)^(\s*\d+:\s+\d+\s+\d+)\s+[A-Z][a-z0-9]*'{0,2}(?=\s)", r"\1", text)
    return text.encode()


def test_a_job_without_printed_irreps_supports_but_never_identifies(evaluator_store):
    """A71: the irrep is the agent's own label when the output prints none."""
    case = _case("ketene")
    jobs = {"j0001": VteJob(_without_irreps(_out("ketene_f_target")), True, False)}
    e = _grade(evaluator_store, case, _claim(case, _state(1, "B1", "6.069", "j0001")), jobs)
    assert e.assignments["claim.01"] == "supported"
    assert e.metrics["claim.01.irrep_printed"] == "false"
    assert e.assignments["claim.01.identity"] == "unidentified"
    assert e.metrics["claim.01.identity_reason"] == "irrep_not_printed"
    assert e.assignments["ref.1B1(pi,3s)"] == "unidentified"
    assert e.metrics["coverage"] == "0" and e.metrics["false_success_claims"] == "0"


def test_supported_state_with_no_reference_row_is_unidentified(evaluator_store):
    # Acetone's job prints B2 states; ketene has no singlet B2 reference row.
    case = _case("ketene")
    e = _grade(evaluator_store, case, _claim(case, _state(1, "B2", "6.914", "j0003")), ACETONE_JOBS)
    assert e.assignments["claim.01"] == "supported"
    assert e.metrics["claim.01.identity_reason"] == "no_reference"


def test_no_claim_leaves_every_reference_missing(evaluator_store):
    case = _case("ketene")
    e = _grade(evaluator_store, case, None, KETENE_JOBS)
    assert {v for k, v in e.assignments.items() if k.startswith("ref.")} == {"missing"}
    assert e.metrics["claim.present"] == "false" and e.metrics["coverage"] == "0"


# ------------------------------------------------------------------ boundaries


def test_refuses_an_agent_visible_store(tmp_path):
    agent = Store(tmp_path / "agent", clock=LogicalClock())
    with pytest.raises(CapabilityError):
        _grade(agent, _case("ketene"), None, {})
    assert not agent.list_record_ids("evaluation")


def test_refuses_mismatched_claim_references_or_job_flags(evaluator_store):
    ketene, acetone = _case("ketene"), _case("acetone")
    with pytest.raises(IntegrityError):
        _grade(evaluator_store, ketene, _claim(acetone), {})
    with pytest.raises(IntegrityError):
        evaluate_vte_claims(
            evaluator_store,
            run_id="r1",
            case=ketene,
            references=load_vte_references(acetone),
            claim=None,
            jobs={},
        )
    with pytest.raises(IntegrityError, match="cache hit"):
        _grade(evaluator_store, ketene, None, {"j0001": (b"", True, True)})


def test_reevaluation_writes_a_new_record(evaluator_store):
    case = _case("ketene")
    first = _grade(evaluator_store, case, None, {})
    second = _grade(evaluator_store, case, None, {})
    assert first.evaluation_id != second.evaluation_id
    assert evaluator_store.has_record("evaluation", first.evaluation_id)
