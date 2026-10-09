"""Final CI observations: authentic outputs and explicitly mutated boundary cases."""

from pathlib import Path

import pytest

from onoda.science.orca_states import MAX_CONFIGS, MAX_ROOTS, casscf_state_summary
from onoda.science.orca_summary import job_summary

FIXTURES = Path(__file__).parent / "fixtures" / "orca611-nevpt2"


def log(name="ketene_f_target") -> bytes:
    return (FIXTURES / f"{name}.out").read_bytes()


def test_final_ci_uses_final_mo_order_not_initial_occupations_or_energy_order():
    lines = casscf_state_summary(log(), converged=True)
    assert "INITIAL CI" not in "\n".join(lines)
    assert "- digit 1: MO 6, 6-A1, occupation 0.0114, E=1.172274 Eh (line 1513)" in lines
    assert "- digit 10: MO 15, 10-A1, occupation 1.9852, E=-0.981849 Eh (line 1522)" in lines
    assert "- block 0, multiplicity 1, A1, NROOTS=1 (line 1694)" in lines
    assert "- block 1, multiplicity 1, B1, NROOTS=1 (line 1710)" in lines
    assert "    0.92991 [  2500]: 0002022022 (line 1698)" in lines
    assert "    0.91780 [  2363]: 0001022122 (line 1714)" in lines
    assert sum("[" in line for line in lines) == 2 * MAX_CONFIGS
    assert "    ... 6 more printed configurations" in lines
    assert "    ... 7 more printed configurations" in lines
    assert any("AO compositions: line 1784" in line for line in lines)
    # Every emitted value is traceable to the original output, not a reference.
    original = log().decode().splitlines()
    for line in lines:
        if "[" in line or line.startswith("  ROOT"):
            text, citation = line.strip().rsplit(" (line ", 1)
            assert original[int(citation[:-1]) - 1].strip() == text


def test_multiple_roots_and_multiplicities_without_symmetry():
    lines = casscf_state_summary(log("ethylene_nevpt2_np4"), converged=True)
    assert any("block 0, multiplicity 3, no irrep printed, NROOTS=1" in line for line in lines)
    assert any("block 1, multiplicity 1, no irrep printed, NROOTS=3" in line for line in lines)
    assert sum(line.startswith("  ROOT") for line in lines) == 4
    assert sum(line.startswith("- digit") for line in lines) == 2
    assert any("MO 7, no irrep printed, occupation 1.0000" in line for line in lines)
    assert any("0.96324 [     2]: 02" in line for line in lines)


def test_failed_or_truncated_casscf_never_claims_final_ci():
    failed = job_summary(log("ethylene_nevpt2_np4_default_orbstep"))
    assert "Converged CAS-SCF states: unavailable (CASSCF convergence not established)" in failed
    assert not any(line.startswith("  ROOT") for line in failed)
    truncated = log().split(b"CAS-SCF STATES FOR BLOCK", 1)[0]
    assert not any(line.startswith("  ROOT") for line in job_summary(truncated))
    incomplete_root = log().split(b"      0.92991", 1)[0]
    lines = casscf_state_summary(incomplete_root, converged=True)
    assert any("CI listing incomplete" in line for line in lines)
    assert not any(line.startswith("  ROOT") for line in lines)


def test_initial_only_missing_and_multiple_final_sections_are_explicit():
    initial = log().split(b"CASSCF RESULTS", 1)[0]
    assert "expected one final results section" in casscf_state_summary(initial, converged=True)[0]
    duplicate = casscf_state_summary(log() * 2, converged=True)
    assert "expected one final results section" in duplicate[0]
    missing = log().replace(b"CAS-SCF STATES FOR BLOCK", b"UNRECOGNIZED STATE BLOCK")
    assert "no final CI blocks printed" in casscf_state_summary(missing, converged=True)[0]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"Active         6 -   15 (  10 orbitals)", b"Active range missing"),
        (b"Active         6 -   15 (  10 orbitals)", b"Active         6 -   15 (  11 orbitals)"),
        (b"   6   0.0114", b"   6   BROKEN"),
        (b"ORBITAL ENERGIES", b"UNKNOWN ORBITAL LIST"),
    ],
)
def test_missing_or_inconsistent_map_never_guesses_from_occupations(old, new):
    assert old in log()
    lines = casscf_state_summary(log().replace(old, new), converged=True)
    assert any("Active-MO mapping unavailable" in line for line in lines)
    assert not any(line.startswith("- digit") for line in lines)


def test_rounded_zero_and_two_are_still_active_and_echo_cannot_supply_evidence():
    mutated = (
        log()
        .replace(b"   6   0.0114", b"   6   0.0000")
        .replace(b"  15   1.9852", b"  15   2.0000")
    )
    # A complete fake result in an input echo must not create a second section.
    echo = b"\n".join(b"| 123> " + line for line in log().splitlines()) + b"\n"
    lines = casscf_state_summary(echo + mutated, converged=True)
    assert any("MO 6, 6-A1, occupation 0.0000" in line for line in lines)
    assert any("MO 15, 10-A1, occupation 2.0000" in line for line in lines)
    assert sum(line.startswith("- digit") for line in lines) == 10
    assert sum(line.startswith("  ROOT") for line in lines) == 2
    echo_only = casscf_state_summary(echo, converged=True)
    assert "expected one final results section" in echo_only[0]


def test_configuration_width_mismatch_is_not_decoded_using_the_map():
    lines = casscf_state_summary(log().replace(b"0002022022", b"000202202"), converged=True)
    assert any("configuration width differs" in line for line in lines)
    assert not any("0.92991" in line for line in lines)


def test_unsupported_root_suffix_is_not_republished_as_state_evidence():
    mutated = log().replace(b"-151.9223354580 Eh", b"-151.9223354580 Eh unexpected-text")
    lines = casscf_state_summary(mutated, converged=True)
    assert any("CI listing incomplete or unsupported" in line for line in lines)
    assert not any("unexpected-text" in line for line in lines)


def test_block_and_active_orbital_limits_are_explicit():
    text = "CASSCF RESULTS\n" + "".join(
        f"CAS-SCF STATES FOR BLOCK {i} MULT= 1 NROOTS= 999999999\n" for i in range(MAX_ROOTS + 1)
    )
    lines = casscf_state_summary(text.encode(), converged=True)
    assert sum(line.startswith("- block") for line in lines) == MAX_ROOTS
    assert "... 1 more CI blocks omitted" in lines
    mutated = log().replace(
        b"Active         6 -   15 (  10 orbitals)", b"Active         6 -   55 (  50 orbitals)"
    )
    lines = casscf_state_summary(mutated, converged=True)
    assert any("50 orbitals exceeds summary limit" in line for line in lines)
    assert not any(line.startswith("- digit") for line in lines)


def test_large_root_listing_is_bounded_and_reports_omissions():
    # Synthetic summary-only input, deliberately not passed off as an ORCA run.
    count = MAX_ROOTS + 2
    text = f"CASSCF RESULTS\nCAS-SCF STATES FOR BLOCK 0 MULT= 1 NROOTS= {count}\n" + "".join(
        f"ROOT {i}: E= -1.0 Eh\n 1.0 [ 0]: 20\n" for i in range(count)
    )
    lines = casscf_state_summary(text.encode(), converged=True)
    assert sum(line.startswith("  ROOT") for line in lines) == MAX_ROOTS
    assert "... 2 more CI roots; inspect the cited output blocks" in lines


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.out")), ids=lambda p: p.stem)
def test_authentic_fixture_summaries_are_bounded(path):
    lines = job_summary(path.read_bytes())
    assert len("\n".join(lines)) < 16000
