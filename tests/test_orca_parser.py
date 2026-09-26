"""ORCA parser: authentic vendored outputs, T18 mutations, and a cclib cross-check."""

from __future__ import annotations

import logging
import warnings

import pytest

from onoda.canonical import canonical_bytes, sha256_digest
from onoda.science.contract import OrcaObservation, case_manifests, load_case
from onoda.science.orca_parser import PARSER_ID, parse_orca_output

CASES = case_manifests()
HARTREE_CM = 219474.6313705  # CODATA; used only for tolerance checks in this test


def output(case_id: str) -> bytes:
    (data,) = load_case(CASES[case_id]).output_bytes.values()
    return data


def lines_of(data: bytes) -> list[str]:
    return data.decode().splitlines(keepends=True)


def line_no(data: bytes, needle: str) -> int:
    return next(i for i, line in enumerate(lines_of(data), 1) if needle in line)


def mutate(data: bytes, old: str, new: str) -> bytes:
    text = data.decode()
    assert text.count(old) >= 1, old
    return text.replace(old, new, 1).encode()


def drop_lines(data: bytes, needle: str) -> bytes:
    return "".join(ln for ln in lines_of(data) if needle not in ln).encode()


def fields(obs: OrcaObservation, state: str) -> set[str]:
    return {p.field for p in obs.problems if p.state == state}


def by_key(obs: OrcaObservation) -> dict[tuple, object]:
    return {(s.multiplicity, s.root): s for s in obs.excited_states}


def test_every_vendored_case_is_covered():
    orca = {c for c in CASES if c.startswith(("orca5-", "orca6-"))}
    assert orca == set(EXPECTED)


# case -> version, scf cycles, final energy (value, line), family, {(mult, root): (au, line)}
EXPECTED = {
    "orca6-dvb-tddft": (
        "6.0.0",
        8,
        ("-381.858419046289", 3290),
        "tddft",
        {(1, 1): ("0.196688", 3080), (1, 5): ("0.272503", 3099), (3, 6): ("0.114999", 3165)},
        10,
    ),
    "orca5-dvb-tddft": (
        "5.0.0",
        9,
        ("-381.858426608871", 4002),
        "tddft",
        {(1, 1): ("0.196682", 3757), (3, 1): ("0.115007", 3867), (3, 5): ("0.194703", 3898)},
        10,
    ),
    "orca6-dvb-eom-ccsd": (
        "6.0.1",
        11,
        ("-380.500397408116", 1631),
        "eom-ccsd",
        {(None, 1): ("0.201084", 1485), (None, 2): ("0.280220", 1494)},
        2,
    ),
    "orca6-dvb-adc2": (
        "6.0.1",
        11,
        ("-380.165181816960", 1525),
        "adc2",
        {(None, 1): ("0.204110", 1425), (None, 2): ("0.256694", 1431)},
        2,
    ),
    "orca6-dvb-steom-ccsd": (
        "6.0.1",
        11,
        ("-380.500397733623", 3294),
        "steom-ccsd",
        {(1, 1): ("0.172381", 3045), (1, 2): ("0.281090", 3061)},
        2,
    ),
    "orca6-water-mp2": ("6.0.1", 8, ("-74.999374598107", 730), None, {}, 0),
}


@pytest.mark.parametrize("case_id", sorted(EXPECTED))
def test_authentic_case(case_id):
    version, cycles, (energy, energy_line), family, spots, count = EXPECTED[case_id]
    data = output(case_id)
    obs = parse_orca_output(data)
    lines = data.decode().splitlines()

    assert obs.parser == PARSER_ID and obs.source == sha256_digest(data)
    assert obs.synthetic is False
    assert obs.problems == []
    assert obs.program_version == version
    assert obs.termination == "normal" and obs.scf == "converged" and obs.scf_cycles == cycles
    assert obs.final_energy.value == energy and obs.final_energy.units == "Eh"
    assert obs.final_energy.evidence.line == energy_line
    assert obs.final_energy.evidence.text == lines[energy_line - 1].rstrip()
    assert obs.method_family == family
    assert obs.excited_states_status == ("present" if count else "absent")
    assert len(obs.excited_states) == count
    states = by_key(obs)
    for key, (au, line) in spots.items():
        q = states[key].energy_au
        assert (q.value, q.units, q.evidence.line) == (au, "au", line)
        assert au in q.evidence.text and q.evidence.text == lines[line - 1].rstrip()
    for s in obs.excited_states:
        assert (s.energy_ev.units, s.energy_cm.units) == ("eV", "cm-1")
        assert s.energy_ev.value in s.energy_ev.evidence.text
    assert obs.warnings and all(w.text.startswith("WARNING") for w in obs.warnings)
    canonical_bytes(obs.to_json())  # no floats anywhere


def test_manifest_requests_match_parsed_roots():
    for case_id in EXPECTED:
        request = load_case(CASES[case_id]).manifest.request.roots_by_multiplicity
        obs = parse_orca_output(output(case_id))
        assert len(obs.excited_states) == sum(request.values()), case_id


def test_orca6_symmetry_and_orca5_section_multiplicity():
    six = by_key(parse_orca_output(output("orca6-dvb-tddft")))
    assert (six[(1, 3)].symmetry, six[(3, 7)].symmetry) == ("Ag", "Ag")
    five = parse_orca_output(output("orca5-dvb-tddft"))
    assert {s.symmetry for s in five.excited_states} == {None}
    assert sorted({s.multiplicity for s in five.excited_states}) == [1, 3]


def test_steom_reports_only_final_steom_roots():
    data = output("orca6-dvb-steom-ccsd")
    obs = parse_orca_output(data)
    lines = {s.energy_au.evidence.line for s in obs.excited_states}
    assert lines == {3045, 3061}
    cis = line_no(data, "IROOT=  1:  0.241150 au")  # CIS guess, before STEOM
    assert cis not in lines


# ------------------------------------------------------------------ T18 mutations


def assert_no_defaults(obs: OrcaObservation) -> None:
    assert obs.synthetic is True
    for name, value in (
        ("final_energy", obs.final_energy),
        ("program_version", obs.program_version),
    ):
        if value is None:
            assert name in {p.field for p in obs.problems}
    canonical_bytes(obs.to_json())


@pytest.mark.parametrize("case_id", ["orca6-dvb-tddft", "orca5-dvb-tddft", "orca6-dvb-eom-ccsd"])
@pytest.mark.parametrize("where", ["header", "mid-listing", "mid-line", "early"])
def test_truncation(case_id, where):
    data = output(case_id)
    first = "STATE  2:" if "tddft" in case_id else "IROOT=  2:"
    start = data.index(first.encode())
    header = data.index(b"EXCITED STATES" if "tddft" in case_id else b"RESULTS (RHS)")
    cut = {
        "header": data.index(b"\n", header) + 1,
        "mid-listing": start,
        "mid-line": start + 30,
        "early": len(data) // 10,
    }[where]
    obs = parse_orca_output(data[:cut], synthetic=True)
    assert_no_defaults(obs)
    assert obs.termination == "absent"
    assert "termination" in fields(obs, "missing")
    assert all(s.root == 1 for s in obs.excited_states)
    if where == "header":
        assert obs.excited_states == [] and obs.excited_states_status == "absent"
    if where == "mid-listing":
        assert [s.root for s in obs.excited_states] == [1]
    if where == "mid-line":
        assert "excited_states" in fields(obs, "error")
        assert obs.excited_states_status == "error"
    if where != "early":
        assert "excited_states" in fields(obs, "unresolved")
    else:
        assert obs.final_energy is None and obs.excited_states == []


def test_state_line_cut_inside_trailing_tokens_is_not_read():
    data = output("orca6-dvb-tddft")
    start = data.index(b"STATE  1:")
    end = data.index(b"Sym: Bu", start) + len(b"Sym: B")
    obs = parse_orca_output(data[:end], synthetic=True)
    assert (1, 1) not in by_key(obs)
    assert obs.excited_states_status == "error"
    (err,) = [p for p in obs.problems if p.state == "error"]
    assert err.evidence.line == 3080


def test_final_energy_line_cut_is_not_read():
    data = output("orca6-water-mp2")
    end = data.index(b"FINAL SINGLE POINT ENERGY") + len(b"FINAL SINGLE POINT ENERGY       -74.99")
    obs = parse_orca_output(data[:end], synthetic=True)
    assert obs.final_energy is None and "final_energy" in fields(obs, "error")


def test_missing_termination_line():
    obs = parse_orca_output(
        drop_lines(output("orca6-dvb-adc2"), "TERMINATED NORMALLY"), synthetic=True
    )
    assert_no_defaults(obs)
    assert obs.termination == "absent" and "termination" in fields(obs, "missing")
    assert "excited_states" in fields(obs, "unresolved")


def test_error_termination():
    data = mutate(
        output("orca6-water-mp2"),
        "****ORCA TERMINATED NORMALLY****",
        "ORCA finished by error termination in SCF",
    )
    obs = parse_orca_output(data, synthetic=True)
    assert obs.termination == "error"


def test_missing_scf_line():
    obs = parse_orca_output(drop_lines(output("orca6-water-mp2"), "SCF CONVERGED"), synthetic=True)
    assert obs.scf == "absent" and obs.scf_cycles is None and "scf" in fields(obs, "missing")


def test_scf_not_converged():
    data = mutate(output("orca6-water-mp2"), "SCF CONVERGED AFTER", "SCF NOT CONVERGED AFTER")
    obs = parse_orca_output(data, synthetic=True)
    assert (obs.scf, obs.scf_cycles) == ("not_converged", 8)


def test_missing_version_and_final_energy():
    data = drop_lines(drop_lines(output("orca6-water-mp2"), "Program Version"), "FINAL SINGLE")
    obs = parse_orca_output(data, synthetic=True)
    assert_no_defaults(obs)
    assert obs.program_version is None and obs.final_energy is None
    assert {"program_version", "final_energy"} <= fields(obs, "missing")


@pytest.mark.parametrize(
    ("case_id", "old", "new", "key"),
    [
        ("orca6-dvb-tddft", "0.196688 au", "0.19x688 au", (1, 1)),
        ("orca5-dvb-tddft", "0.115007 au", "0.115007au", (3, 1)),
        ("orca6-dvb-eom-ccsd", "7.625 eV", "7.62.5 eV", (None, 2)),
        ("orca6-dvb-adc2", "44797.1 cm", "4479e1 cm", (None, 1)),
        ("orca6-dvb-steom-ccsd", "0.172381 au", "nan au", (1, 1)),
    ],
)
def test_malformed_number(case_id, old, new, key):
    obs = parse_orca_output(mutate(output(case_id), old, new), synthetic=True)
    assert_no_defaults(obs)
    assert key not in by_key(obs)
    assert obs.excited_states_status == "error"
    (err,) = [p for p in obs.problems if p.state == "error"]
    assert new in err.evidence.text


@pytest.mark.parametrize(
    ("case_id", "old", "new"),
    [
        ("orca6-dvb-tddft", "5.352 eV", "5.352 nm"),
        ("orca5-dvb-tddft", "0.196682 au", "0.196682 Eh"),
        ("orca6-dvb-eom-ccsd", "44132.8 cm**-1", "44132.8 cm-1"),
    ],
)
def test_wrong_state_unit(case_id, old, new):
    obs = parse_orca_output(mutate(output(case_id), old, new), synthetic=True)
    assert obs.excited_states_status == "error"
    (err,) = [p for p in obs.problems if p.state == "error"]
    assert "unit" in err.detail and new in err.evidence.text
    assert all(new not in s.energy_au.evidence.text for s in obs.excited_states)


def test_final_energy_with_unexpected_unit_or_bad_number():
    data = output("orca6-water-mp2")
    for new in ("-74.999374598107 eV", "-74.99937459810x"):
        obs = parse_orca_output(mutate(data, "-74.999374598107", new), synthetic=True)
        assert obs.final_energy is None and "final_energy" in fields(obs, "error")


def test_identical_repeat_listing_dedupes():
    # the authentic EOM/ADC logs already print each root twice (RHS and LHS listings)
    data = output("orca6-dvb-eom-ccsd")
    assert data.count(b"IROOT=  1:  0.201084 au") == 2
    assert len(parse_orca_output(data).excited_states) == 2


@pytest.mark.parametrize(
    ("case_id", "line", "changed", "key"),
    [
        ("orca6-dvb-eom-ccsd", "IROOT=  1:  0.201084 au", "0.201085", (None, 1)),
        ("orca6-dvb-tddft", "STATE  3:  E=   0.228756", "0.228757", (1, 3)),
    ],
)
def test_conflicting_duplicate_listing_is_ambiguous(case_id, line, changed, key):
    data = output(case_id)
    lines = lines_of(data)
    n = line_no(data, line)
    original = lines[n - 1]
    old = original.split()[2] if case_id.endswith("eom-ccsd") else original.split()[3]
    lines.insert(n + 1, original.replace(old, changed))
    obs = parse_orca_output("".join(lines).encode(), synthetic=True)
    assert_no_defaults(obs)
    assert obs.excited_states_status == "ambiguous"
    assert key not in by_key(obs)
    (amb,) = [p for p in obs.problems if p.state == "ambiguous"]
    assert amb.evidence.line == n + 2 and str(n) in amb.detail


def test_unattributable_mixed_methods_are_ambiguous():
    # a CIS listing without a STEOM listing cannot be dismissed as a guess
    data = mutate(
        output("orca6-dvb-eom-ccsd"),
        "----------------------\nEOM-CCSD RESULTS (RHS)",
        "-----------\nCIS RESULTS\n-----------\n\nIROOT=  1:  0.241150 au     6.562 eV"
        "   52926.3 cm**-1\n\n----------------------\nEOM-CCSD RESULTS (RHS)",
    )
    obs = parse_orca_output(data, synthetic=True)
    assert obs.excited_states_status == "ambiguous" and obs.excited_states == []
    assert obs.method_family is None


def test_mult_conflicting_with_section_is_not_guessed():
    data = mutate(output("orca6-dvb-tddft"), "Sym: Bu Mult 1", "Sym: Bu Mult 3")
    obs = parse_orca_output(data, synthetic=True)
    assert (1, 1) not in by_key(obs) and (3, 1) not in by_key(obs)
    assert "excited_states" in fields(obs, "ambiguous")


# ------------------------------------------------------------------ cclib cross-check


def cclib_parse(case_id: str):
    cclib = pytest.importorskip("cclib")
    load = load_case(CASES[case_id])
    (rel,) = [p.path for p in load.manifest.outputs.values()]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return cclib, cclib.io.ccread(str(load.root / rel), loglevel=logging.ERROR)
        except Exception as exc:  # pragma: no cover - depends on cclib version
            pytest.skip(f"cclib {cclib.__version__} cannot parse {case_id}: {exc!r}")


@pytest.mark.parametrize("case_id", sorted(EXPECTED))
def test_cclib_excitation_cross_check(case_id):
    cclib, data = cclib_parse(case_id)
    obs = parse_orca_output(output(case_id))
    assert data.metadata["package_version"].startswith(obs.program_version)

    theirs = sorted(float(x) for x in getattr(data, "etenergies", []))
    ours = sorted(obs.excited_states, key=lambda s: float(s.energy_au.value))
    assert len(theirs) == len(ours)
    for cm, state in zip(theirs, ours, strict=True):
        # cclib reads either the printed cm-1 or the printed au value; allow
        # half a unit in the last printed place of both, plus the au->cm span.
        au_cm = float(state.energy_au.value) * HARTREE_CM
        assert abs(cm - au_cm) <= 0.5e-6 * HARTREE_CM + 0.06, (case_id, state.root)
        assert abs(cm - float(state.energy_cm.value)) <= 0.5e-6 * HARTREE_CM + 0.06


@pytest.mark.parametrize("case_id", sorted(EXPECTED))
def test_cclib_final_energy_cross_check(case_id):
    cclib, data = cclib_parse(case_id)
    obs = parse_orca_output(output(case_id))
    # cclib keeps the CC total in ccenergies and the MP2 total in mpenergies[-1][-1]
    if getattr(data, "ccenergies", None) is not None:
        total = float(data.ccenergies[-1])
    elif getattr(data, "mpenergies", None) is not None:
        total = float(data.mpenergies[-1][-1])
    else:
        pytest.skip(f"cclib exposes no total energy matching FINAL SINGLE POINT for {case_id}")
    hartree = cclib.parser.utils.convertor(total, "eV", "hartree")
    # printed to 1e-12 Eh; cclib's eV round trip costs ~1e-10 Eh of float precision
    assert abs(hartree - float(obs.final_energy.value)) < 1e-9


def test_numeric_garbage_in_a_unit_slot_is_an_error_not_a_skipped_row():
    data = mutate(
        output("orca6-dvb-steom-ccsd"),
        "IROOT=  1:  0.172381 au     4.691 eV",
        "IROOT=  1:  0.172381 1.0     4.691 eV",
    )
    obs = parse_orca_output(data, synthetic=True)
    assert obs.excited_states_status == "error"
    assert "excited_states" in fields(obs, "error")
    assert [s.root for s in obs.excited_states] == [2]


def test_corruption_in_the_discarded_steom_cis_guess_leaves_steom_roots_reported():
    data = mutate(
        output("orca6-dvb-steom-ccsd"),
        "IROOT=  1:  0.241150 au",
        "IROOT=  1:  0.241150 xx",
    )
    obs = parse_orca_output(data, synthetic=True)
    assert obs.excited_states_status == "present"
    assert [s.energy_au.value for s in obs.excited_states] == ["0.172381", "0.281090"]
    assert "excited_states" in fields(obs, "error")  # the corrupt line is still reported
