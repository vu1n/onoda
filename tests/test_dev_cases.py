"""Dev-only cases under cases-dev/ load and verify like the pinned cases."""

from pathlib import Path

from onoda.science.contract import case_manifests, load_case

DEV_ROOT = Path(__file__).resolve().parents[1] / "cases-dev"


def test_dev_cases_load_and_are_labelled_dev_only():
    manifests = case_manifests(DEV_ROOT)
    assert set(manifests) == {"orca5-adbna-tddft", "orca4-1ttt-tddft"}
    for path in manifests.values():
        case = load_case(path)  # checks every pinned sha256
        m = case.manifest
        assert "dev-only" in m.labels
        assert m.provenance.executed_here is False
        assert (path.parent / m.provenance.license_file).is_file()
