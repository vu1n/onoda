"""Mutation audit of the VTE grader on real ORCA 6.1.1 outputs (`tests/fixtures/orca611-nevpt2/`).

Every known-wrong variant of a supported claim must leave no identified row
within tolerance; `label_swap` is measured only (the grader takes the
character from the agent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from onoda.adapters.pi.vte import VteJob
from onoda.adapters.pi.vte_audit import AuditReport, audit_claim
from onoda.adapters.pi.vte_contract import ClaimedVte, VteClaim
from onoda.clock import LogicalClock
from onoda.science.vte import load_vte_case, load_vte_references, vte_case_manifests
from onoda.store import Store

REPO = Path(__file__).resolve().parent.parent
CASES = vte_case_manifests(REPO / "cases-dev" / "questdb")
FIXTURES = REPO / "tests" / "fixtures" / "orca611-nevpt2"


def _job(name: str) -> VteJob:
    return VteJob((FIXTURES / f"{name}.out").read_bytes(), executed_here=True, cache_hit=False)


def _state(irrep: str, energy: str, job_id: str, character: str) -> ClaimedVte:
    return ClaimedVte.model_validate(
        {
            "spin": 1,
            "irrep": irrep,
            "character": character,
            "energy": {"text": energy, "unit": "eV"},
            "job_id": job_id,
        }
    )


@pytest.fixture
def scratch(tmp_path):
    return Store(tmp_path / "scratch", clock=LogicalClock(), allowed_visibility=("evaluator",))


def _audit(report, scratch, name, jobs, *states):
    case = load_vte_case(CASES[f"questdb-{name}"])
    audit_claim(
        report,
        scratch,
        run_id=name,
        case=case,
        references=load_vte_references(case),
        claim=VteClaim(case_id=case.manifest.case_id, states=states),
        jobs=jobs,
    )


def test_no_known_wrong_variant_of_a_real_claim_is_credited(scratch):
    report = AuditReport()
    ketene = {"j0001": _job("ketene_a_rhf"), "j0005": _job("ketene_f_target")}
    _audit(report, scratch, "ketene", ketene, _state("B1", "6.069", "j0005", "pi,3s"))
    acetone = {"j0002": _job("acetone_b_valence_dz"), "j0007": _job("acetone_g_trah")}
    _audit(
        report,
        scratch,
        "acetone",
        acetone,
        _state("B2", "6.914", "j0007", "n,3s"),
        _state("B2", "4.614", "j0007", "n,3pz"),  # identified but outside tolerance
        _state("B2", "9.999", "j0007", "n,3s"),  # unsupported: no variants built from it
    )
    d = report.as_dict()
    assert d["states"] == 3 and d["baseline_accurate"] == 2
    assert d["false_accepts"] == 0, d["examples"]
    expected = {
        "energy_nudged", "other_spin", "other_irrep", "casscf_value", "other_job",
        "job_not_in_run", "job_not_executed", "casscf_not_converged", "no_character",
        "duplicate", "label_swap",
    }  # fmt: skip
    assert set(d["variants"]) == expected
    assert d["variants"]["casscf_value"]["graded"] == 3
    assert d["variants"]["label_swap"] == {"graded": 2, "accepted": 0}
