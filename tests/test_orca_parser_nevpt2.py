"""ORCA parser on real ORCA 6.1.1 SA-CASSCF / SC-NEVPT2 output, plus mutations.

Expected values are transcribed from the fixture bytes and the README's
known results (`tests/fixtures/orca611-nevpt2/README.md`). Mutated outputs
are parsed with `synthetic=True`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from onoda.canonical import canonical_bytes, sha256_digest
from onoda.science.contract import OrcaObservation
from onoda.science.orca_parser import PARSER_ID, parse_orca_output

FIXTURES = Path(__file__).parent / "fixtures" / "orca611-nevpt2"
CONVERGED_LINE = "---- THE CAS-SCF GRADIENT HAS CONVERGED ----"


def output(job: str) -> bytes:
    return (FIXTURES / f"{job}.out").read_bytes()


def mutate(data: bytes, old: str, new: str, count: int = 1) -> bytes:
    text = data.decode()
    assert text.count(old) >= count, old
    return text.replace(old, new, count).encode()


def drop_lines(data: bytes, needle: str) -> bytes:
    lines = data.decode().splitlines(keepends=True)
    assert any(needle in ln for ln in lines), needle
    return "".join(ln for ln in lines if needle not in ln).encode()


def problems(obs: OrcaObservation, field: str) -> list[tuple[str, str]]:
    return [(p.state, p.detail) for p in obs.problems if p.field == field]


def table(obs: OrcaObservation) -> dict[tuple, tuple[str, str, str, int]]:
    """(method, mult, irrep, root) -> (au, eV, cm-1, line)."""
    out = {}
    for s in obs.mr_states:
        key = (s.method, s.multiplicity, s.symmetry, s.root)
        assert key not in out
        out[key] = (
            s.energy_au.value,
            s.energy_ev.value,
            s.energy_cm.value,
            s.energy_au.evidence.line,
        )
    return out


# job -> {(method, mult, irrep, root): (au, eV, cm-1, line)}
EXPECTED = {
    "ketene_f_target": {
        ("CASSCF", 1, "B1", 0): ("0.191732", "5.217", "42080.4", 1733),
        ("SC-NEVPT2", 1, "B1", 0): ("0.223022", "6.069", "48947.8", 4264),
    },
    "acetone_g_trah": {
        ("CASSCF", 1, "B2", 0): ("0.243362", "6.622", "53411.8", 1385),
        ("CASSCF", 1, "B2", 1): ("0.343589", "9.350", "75409.1", 1386),
        # NEVPT2 prints root 1 first (lower energy); identity is the root, not the order.
        ("SC-NEVPT2", 1, "B2", 1): ("0.169552", "4.614", "37212.3", 8339),
        ("SC-NEVPT2", 1, "B2", 0): ("0.254092", "6.914", "55766.8", 8340),
    },
    "formaldehyde_nevpt2_np4": {
        ("CASSCF", 3, None, 0): ("0.124567", "3.390", "27339.2", 1346),
        ("CASSCF", 1, None, 1): ("0.138162", "3.760", "30323.0", 1347),
        ("CASSCF", 3, None, 1): ("0.190533", "5.185", "41817.2", 1348),
        ("CASSCF", 1, None, 2): ("0.263617", "7.173", "57857.3", 1349),
        ("SC-NEVPT2", 3, None, 0): ("0.136458", "3.713", "29949.1", 3424),
        ("SC-NEVPT2", 1, None, 1): ("0.150734", "4.102", "33082.3", 3425),
        ("SC-NEVPT2", 3, None, 1): ("0.230008", "6.259", "50481.0", 3426),
        ("SC-NEVPT2", 1, None, 2): ("0.269521", "7.334", "59153.0", 3427),
    },
    "ethylene_nevpt2_np4": {
        ("CASSCF", 3, None, 0): ("0.150788", "4.103", "33094.1", 1073),
        ("CASSCF", 1, None, 1): ("0.352799", "9.600", "77430.3", 1074),
        ("CASSCF", 1, None, 2): ("0.536804", "14.607", "117815.0", 1075),
        ("SC-NEVPT2", 3, None, 0): ("0.176228", "4.795", "38677.7", 3981),
        ("SC-NEVPT2", 1, None, 1): ("0.272379", "7.412", "59780.3", 3982),
        ("SC-NEVPT2", 1, None, 2): ("0.469808", "12.784", "103111.0", 3983),
    },
    "formaldehyde_guess": {
        ("CASSCF", 3, None, 0): ("0.127307", "3.464", "27940.7", 975),
        ("CASSCF", 1, None, 1): ("0.140928", "3.835", "30930.2", 976),
        ("CASSCF", 3, None, 1): ("0.193545", "5.267", "42478.1", 977),
        ("CASSCF", 1, None, 2): ("0.329404", "8.964", "72295.8", 978),
    },
    "ethylene_guess": {
        ("CASSCF", 3, None, 0): ("0.155735", "4.238", "34180.0", 829),
        ("CASSCF", 1, None, 1): ("0.381311", "10.376", "83688.0", 830),
        ("CASSCF", 1, None, 2): ("0.563418", "15.331", "123656.0", 831),
    },
    # Single-root CASSCF: converged, no transition listing.
    "ketene_b_valence_dz": {},
    "ketene_c_valence_avtz_nosym": {},
    "ketene_d_valence_avtz_sym": {},
    "acetone_b_valence_dz": {},
    "acetone_c_valence_avtz_nosym": {},
}


def test_every_fixture_is_covered():
    jobs = {p.stem for p in FIXTURES.glob("*.out")}
    assert jobs == set(EXPECTED) | {
        "ketene_a_rhf",
        "acetone_a_rhf",
        "ethylene_nevpt2_np4_default_orbstep",
    }


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_real_casscf_job(job):
    data = output(job)
    obs = parse_orca_output(data)
    lines = data.decode().splitlines()

    assert obs.parser == PARSER_ID == "orca-parser@3"
    assert obs.source == sha256_digest(data) and obs.synthetic is False
    assert obs.program_version == "6.1.1" and obs.termination == "normal"
    assert obs.casscf_converged is True
    assert problems(obs, "mr_states") == [] and problems(obs, "casscf_converged") == []
    # CASSCF jobs print no SCF convergence line; that stays an explicit problem.
    assert [p.field for p in obs.problems] == ["scf"]
    assert obs.excited_states == [] and obs.excited_states_status == "absent"
    assert table(obs) == EXPECTED[job]
    for s in obs.mr_states:
        units = (s.energy_au.units, s.energy_ev.units, s.energy_cm.units)
        assert units == ("au", "eV", "cm-1")
        for q in (s.energy_au, s.energy_ev, s.energy_cm):
            assert q.evidence.artifact == obs.source
            assert q.evidence.text == lines[q.evidence.line - 1].rstrip()
            assert q.value in q.evidence.text.split()
    canonical_bytes(obs.to_json())  # no floats anywhere


def test_readme_known_results():
    ketene = table(parse_orca_output(output("ketene_f_target")))
    assert ketene[("CASSCF", 1, "B1", 0)][1] == "5.217"
    assert ketene[("SC-NEVPT2", 1, "B1", 0)][1] == "6.069"
    acetone = parse_orca_output(output("acetone_g_trah"))
    nevpt2 = [s for s in acetone.mr_states if s.method == "SC-NEVPT2"]
    assert [(s.root, s.energy_ev.value) for s in nevpt2] == [(1, "4.614"), (0, "6.914")]


@pytest.mark.parametrize("job", ["ketene_a_rhf", "acetone_a_rhf"])
def test_rhf_job_has_no_casscf(job):
    obs = parse_orca_output(output(job))
    assert obs.casscf_converged is None and obs.mr_states == []
    assert obs.problems == []


def test_nonconverged_casscf_is_an_explicit_problem():
    data = output("ethylene_nevpt2_np4_default_orbstep")
    obs = parse_orca_output(data)
    assert obs.termination == "error"
    assert obs.casscf_converged is False and obs.mr_states == []
    (failure,) = [p for p in obs.problems if p.field == "casscf_converged"]
    assert (failure.state, failure.detail) == ("error", "CASSCF did not converge")
    assert "Convergence Failure" in failure.evidence.text
    assert problems(obs, "excited_states") == []  # "CASSCF RESULTS" is not a TD listing


# ------------------------------------------------------------------ mutations


def test_normal_termination_with_failed_casscf_is_a_problem():
    data = output("ethylene_nevpt2_np4_default_orbstep")
    data = drop_lines(drop_lines(data, "ORCA finished by error termination"), "aborting the run")
    data += b"                             ****ORCA TERMINATED NORMALLY****\n"
    obs = parse_orca_output(data, synthetic=True)
    assert obs.termination == "normal" and obs.casscf_converged is False
    assert problems(obs, "casscf_converged") == [
        ("error", "CASSCF did not converge although ORCA printed normal termination")
    ]


def test_listings_after_a_failed_casscf_are_not_reported():
    data = mutate(
        output("ketene_f_target"),
        CONVERGED_LINE,
        "ERROR (ORCA_CASSCF): Convergence Failure. Retry with modified convergence settings!",
    )
    obs = parse_orca_output(data, synthetic=True)
    assert obs.casscf_converged is False and obs.mr_states == []
    assert ("error", "CASSCF did not converge although ORCA printed normal termination") in (
        problems(obs, "casscf_converged")
    )
    assert [s for s, _ in problems(obs, "mr_states")] == ["unresolved"]


def test_missing_convergence_line_is_not_a_default():
    obs = parse_orca_output(drop_lines(output("ketene_f_target"), CONVERGED_LINE), synthetic=True)
    assert obs.casscf_converged is None and obs.mr_states == []
    assert [s for s, _ in problems(obs, "casscf_converged")] == ["missing"]
    assert [s for s, _ in problems(obs, "mr_states")] == ["unresolved"]


def test_mixed_convergence_verdicts_are_ambiguous():
    data = output("ketene_f_target")
    text = data.decode()
    at = text.index(CONVERGED_LINE)
    data = (text[:at] + "ERROR (ORCA_CASSCF): Convergence Failure.\n" + text[at:]).encode()
    obs = parse_orca_output(data, synthetic=True)
    assert obs.casscf_converged is None and obs.mr_states == []
    assert [s for s, _ in problems(obs, "casscf_converged")] == ["ambiguous"]


def test_malformed_value_drops_only_that_state():
    data = mutate(output("ketene_f_target"), "0.223022     6.069", "0.223022     6.0x9")
    obs = parse_orca_output(data, synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert problems(obs, "mr_states") == [("error", "malformed number '6.0x9'")]


def test_missing_pt2_method_drops_the_nevpt2_listing():
    obs = parse_orca_output(drop_lines(output("ketene_f_target"), "(PT2 = "), synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert problems(obs, "mr_states") == [("error", "NEVPT2 listing but no '(PT2 = ...)' line")]


def test_pt2_method_is_read_not_assumed():
    data = mutate(output("ketene_f_target"), "(PT2 = SC-NEVPT2)", "(PT2 = FIC-NEVPT2)")
    obs = parse_orca_output(data, synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0), ("FIC-NEVPT2", 1, "B1", 0)}


def test_unexpected_column_header_drops_the_listing():
    data = mutate(
        output("ketene_f_target"),
        "STATE ROOT MULT IRREP  DE/a.u.     DE/eV    DE/cm**-1",
        "STATE ROOT MULT IRREP  DE/a.u.     DE/kcal  DE/cm**-1",
    )
    obs = parse_orca_output(data, synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert problems(obs, "mr_states") == [("error", "unexpected column header")]


def test_missing_lowest_root_drops_the_listing():
    data = drop_lines(output("formaldehyde_nevpt2_np4"), "LOWEST ROOT (ROOT 0, MULT 1)")
    obs = parse_orca_output(data, synthetic=True)
    assert {k[0] for k in table(obs)} == {"CASSCF"}
    assert problems(obs, "mr_states") == [("error", "no LOWEST ROOT line before the listing")]


def test_non_singlet_lowest_root_is_unresolved():
    data = mutate(output("formaldehyde_nevpt2_np4"), "(ROOT 0, MULT 1)", "(ROOT 0, MULT 3)")
    obs = parse_orca_output(data, synthetic=True)
    assert [s for s, _ in problems(obs, "mr_states")] == ["unresolved"]


def test_row_with_wrong_width_drops_the_listing():
    data = mutate(
        output("ketene_f_target"), "   1:   0    1   B1    0.223022", "   1:   0    1    0.223022"
    )
    obs = parse_orca_output(data, synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert problems(obs, "mr_states") == [("error", "row does not have 6 columns")]


def nevpt2_block(data: bytes) -> str:
    text = data.decode()
    start = text.index(" NEVPT2 TRANSITION ENERGIES")
    start = text.rindex("\n", 0, start - 1)  # keep the dashed line above the header
    return text[start : text.index(" NEVPT2 CORRECTION TO THE TRANSITION ENERGY")]


def test_identical_reprint_is_deduplicated():
    data = output("ketene_f_target")
    block = nevpt2_block(data)
    data = mutate(data, block, block + block)
    obs = parse_orca_output(data, synthetic=True)
    assert table(obs).keys() == EXPECTED["ketene_f_target"].keys()
    assert problems(obs, "mr_states") == []


def test_conflicting_reprint_drops_the_state():
    data = output("ketene_f_target")
    block = nevpt2_block(data)
    data = mutate(data, block, block + block.replace("6.069", "6.070"))
    obs = parse_orca_output(data, synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert [s for s, _ in problems(obs, "mr_states")] == ["ambiguous"]


def test_cut_row_is_not_read():
    data = output("ketene_f_target")
    end = data.index(b"48947.8") + len(b"48947.8")  # file ends mid-listing, no newline
    obs = parse_orca_output(data[:end], synthetic=True)
    assert obs.termination == "absent"
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert ("error", "line is cut off (no trailing newline); not read") in problems(
        obs, "mr_states"
    )
    assert ("unresolved", "no normal termination; listing may be incomplete") in problems(
        obs, "mr_states"
    )
    assert problems(obs, "excited_states") == []


def test_listing_cut_before_its_columns():
    data = output("ketene_f_target")
    end = data.index(b"LOWEST ROOT (ROOT 0, MULT 1, IRREP  A1) =   -152.306748571")
    obs = parse_orca_output(data[:end], synthetic=True)
    assert ("error", "listing ends before its column header") in problems(obs, "mr_states")
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}


def test_parser_at_2_records_still_load():
    obs = parse_orca_output(output("ketene_f_target")).to_json()
    for name in ("casscf_converged", "mr_states"):
        del obs[name]
    old = OrcaObservation.model_validate({**obs, "parser": "orca-parser@2"})
    assert old.casscf_converged is None and old.mr_states == []


# ------------------------------------------------------------------ input echo


def echo_comment(data: bytes, text: str) -> bytes:
    """Add `text` to ORCA's input echo as a comment line, as an agent's input could."""
    return mutate(data, "| 21> *\n", f"| 21> *\n| 22> # {text}\n")


def test_an_echoed_convergence_comment_is_not_a_convergence_line():
    data = drop_lines(output("ketene_f_target"), CONVERGED_LINE)
    data = echo_comment(data, CONVERGED_LINE)
    obs = parse_orca_output(data, synthetic=True)
    assert obs.casscf_converged is None and obs.mr_states == []
    assert [s for s, _ in problems(obs, "casscf_converged")] == ["missing"]


def test_an_echoed_failure_comment_does_not_fail_a_converged_casscf():
    data = echo_comment(output("ketene_f_target"), "CASSCF NOT CONVERGED")
    obs = parse_orca_output(data, synthetic=True)
    assert obs.casscf_converged is True
    assert problems(obs, "casscf_converged") == []


def test_an_echoed_pt2_label_is_not_the_pt2_method():
    data = drop_lines(output("ketene_f_target"), "(PT2 = ")
    data = echo_comment(data, "(PT2 = FIC-NEVPT2)")
    obs = parse_orca_output(data, synthetic=True)
    assert set(table(obs)) == {("CASSCF", 1, "B1", 0)}
    assert problems(obs, "mr_states") == [("error", "NEVPT2 listing but no '(PT2 = ...)' line")]
