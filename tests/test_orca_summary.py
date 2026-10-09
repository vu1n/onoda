"""A74: the parser summary each broker reply carries, on authentic ORCA 6.1.1 outputs."""

from __future__ import annotations

from pathlib import Path

from onoda.science.orca_summary import job_summary

FIXTURES = Path(__file__).parent / "fixtures" / "orca611-nevpt2"


def summary(name: str) -> tuple[str, ...]:
    return job_summary((FIXTURES / f"{name}.out").read_bytes())


def test_a_symmetric_nevpt2_job_lists_its_states_with_line_numbers():
    lines = summary("ketene_f_target")
    assert lines[:8] == (
        "termination: normal",
        "final single point energy: -152.195237381698 Eh (line 4465)",
        "symmetry: on, point group C2v",
        "CASSCF: converged",
        "CASSCF transition energies (line numbers in this output):",
        "- singlet B1 root 0: 5.217 eV (line 1733)",
        "SC-NEVPT2 transition energies (line numbers in this output):",
        "- singlet B1 root 0: 6.069 eV (line 4264)",
    )
    out = (FIXTURES / "ketene_f_target.out").read_text().splitlines()
    assert "6.069" in out[4264 - 1]  # the line number points at the printed value
    assert any("Converged CAS-SCF states (final CI" in line for line in lines)


def test_without_symmetry_the_states_say_no_irrep_was_printed():
    lines = summary("ethylene_nevpt2_np4")
    assert "symmetry: not used (no UseSym ON printed), so states print no irrep" in lines
    assert "- triplet no irrep printed root 0: 4.795 eV (line 3981)" in lines


def test_an_hf_job_has_no_casscf_lines():
    lines = summary("acetone_a_rhf")
    assert lines[0] == "termination: normal"
    assert not any("CASSCF" in ln for ln in lines)


def test_a_truncated_output_says_so():
    log = (FIXTURES / "ketene_f_target.out").read_bytes()
    lines = job_summary(log[: len(log) // 2])
    assert lines[0] == "termination: absent"
    assert any(ln.startswith("problem (termination): ") for ln in lines)
