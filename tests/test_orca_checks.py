"""A66: advisory ladder checks the broker reads from each job's ORCA output.

Every output here is authentic ORCA 6.1.1 (`tests/fixtures/orca611-nevpt2`);
the job ids stand for the order an agent would have run them in.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from onoda.science.orca_checks import JobFacts, job_checks, job_facts

FIXTURES = Path(__file__).parent / "fixtures" / "orca611-nevpt2"


def facts(name: str) -> JobFacts:
    return job_facts((FIXTURES / f"{name}.out").read_bytes())


def run(*names: str) -> list[tuple[str, ...]]:
    """The check lines each job of a run would get, in order."""
    earlier: list[tuple[str, JobFacts]] = []
    out = []
    for i, name in enumerate(names, 1):
        f = facts(name)
        out.append(job_checks(f"j{i:04d}", f, earlier))
        earlier.append((f"j{i:04d}", f))
    return out


def test_facts_read_from_real_outputs():
    rhf = facts("ketene_a_rhf")
    assert rhf.hf_energy == "-151.73976945822011"
    assert rhf.casscf_ground is None and rhf.root_weights == ()
    cas = facts("ketene_b_valence_dz")
    assert cas.hf_energy is None  # orbitals read with MORead: no SCF in this job
    assert cas.casscf_ground == "-151.883281310"  # single root: its final CASSCF energy
    assert cas.frame == rhf.frame  # same basis line and geometry
    target = facts("ketene_f_target")
    assert target.casscf_ground == "-151.922335458"  # LOWEST ROOT, not the SA average
    assert target.active_irreps == (("A1", 5), ("B1", 3), ("B2", 2))  # A2 has none


def test_casscf_below_hf_from_an_earlier_job():
    rhf, cas = run("ketene_a_rhf", "ketene_b_valence_dz")
    assert rhf == ()  # no CASSCF, no checks
    assert cas[0].startswith("ok (criterion 1): lowest CASSCF root -151.883281310 Eh is below")
    assert "HF -151.73976945822011 Eh (job j0001, same basis and geometry)" in cas[0]


def test_casscf_not_below_hf_is_a_warning():
    rhf, cas = facts("ketene_a_rhf"), facts("ketene_b_valence_dz")
    higher = replace(rhf, hf_energy="-151.900000000")
    (line, *_) = job_checks("j0002", cas, [("j0001", higher)])
    assert line.startswith("WARNING (criterion 1)") and "-151.900000000" in line


def test_hf_in_another_basis_is_not_compared():
    _, _, cas = run("ketene_a_rhf", "ketene_b_valence_dz", "ketene_d_valence_avtz_sym")
    assert cas[0].startswith("not checked (criterion 1")


def test_unequal_state_average_weights_are_flagged():
    (lines,) = run("acetone_g_trah")
    (weights,) = [ln for ln in lines if "state averaging" in ln]
    assert weights.startswith("WARNING (state averaging)")
    assert "mult 1 A1 root 0: 0.500000; mult 1 B2 root 0: 0.250000" in weights


def test_equal_weights_pass_and_irreps_are_listed():
    (lines,) = run("ketene_f_target")
    assert "ok (state averaging): 2 roots weighted equally (0.500000 each)" in lines
    assert any(ln.endswith("A1 5, B1 3, B2 2") for ln in lines)


def test_without_symmetry_no_irreps_and_unconverged_casscf_is_not_compared():
    (nosym,) = run("ketene_c_valence_avtz_nosym")
    assert not any("criterion 3" in ln for ln in nosym)
    (failed,) = run("ethylene_nevpt2_np4_default_orbstep")  # CASSCF did not converge
    assert not any("criterion 1" in ln for ln in failed)
    assert any(ln.startswith("WARNING (state averaging)") for ln in failed)


def test_nevpt2_reordering_the_casscf_roots_is_flagged():
    (lines,) = run("acetone_g_trah")
    (order,) = [ln for ln in lines if "root order" in ln]
    assert order.startswith("WARNING (root order)")
    assert "mult 1 B2: root 1 4.614 eV, root 0 6.914 eV" in order
    for name in ("ketene_f_target", "ethylene_nevpt2_np4"):  # with and without symmetry
        (lines,) = run(name)
        assert "ok (root order): SC-NEVPT2 keeps the SA-CASSCF root order in every block" in lines
    (no_nevpt2,) = run("ketene_d_valence_avtz_sym")
    assert not any("root order" in ln for ln in no_nevpt2)


def test_the_input_echo_is_never_read():
    text = (FIXTURES / "ketene_a_rhf.out").read_bytes()
    echo = b"|  9> # LOWEST ROOT (ROOT 0, MULT 1) =   -999.000000000 Eh\n"
    assert job_facts(echo + text).casscf_ground is None
    assert job_facts(b"").root_weights == () and job_facts(b"").frame is None
