"""Dev-only cases under cases-dev/ load and verify like the pinned cases."""

from pathlib import Path

from onoda.science.contract import case_manifests, load_case

DEV_ROOT = Path(__file__).resolve().parents[1] / "cases-dev"


def test_dev_cases_load_and_are_labelled_dev_only():
    manifests = case_manifests(DEV_ROOT)
    assert set(manifests) == {
        "orca5-adbna-tddft",
        "orca4-1ttt-tddft",
        "orca42-dvb-gopt",
        "orca60-chf2-opt-epr",
        "orca42-dvb-ir",
        "orca42-dvb-nmr",
        "orca42-dvb-sp-un",
        "orca42-water-ccsd-t",
        "orca42-water-dlpno-ccsd",
        "orca60-be-casscf",
        "orca50-dvb-soc",
        "orca61-zindo-cis",
    }
    for path in manifests.values():
        case = load_case(path)  # checks every pinned sha256
        m = case.manifest
        assert "dev-only" in m.labels
        assert m.provenance.executed_here is False
        assert (path.parent / m.provenance.license_file).is_file()
