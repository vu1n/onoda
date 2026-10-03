"""The eight QUESTDB VTE cases under `cases-dev/questdb/` (VTE-007).

Offline: loads every case with `load_vte_case`, which verifies the sha256 pins,
and compares the committed `references.json` rows with the QUESTDB JSON
extracts vendored in `experiments/questdb-phase0/questdb-raw/` (the same
commit the cases were built from).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onoda.science.vte import (
    VTE_MANIFEST_SUFFIX,
    load_vte_case,
    load_vte_references,
    parse_state_label,
    vte_case_manifests,
)

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "cases-dev" / "questdb"
RAW = ROOT / "experiments" / "questdb-phase0" / "questdb-raw" / "data" / "cas" / "json"
MOLECULES = (
    "acetaldehyde",
    "formamide",
    "ketene",
    "diazomethane",
    "acetone",
    "thioacetone",
    "methylenecyclopropene",
    "cyclopropenone",
)
TOTAL_TRANSITIONS = 53


def _manifests() -> dict[str, Path]:
    return vte_case_manifests(CASES)


def test_eight_cases_load_and_pins_verify():
    manifests = _manifests()
    assert sorted(manifests) == sorted(f"questdb-{m}" for m in MOLECULES)
    for case_id, path in manifests.items():
        case = load_vte_case(path)  # raises IntegrityError on any pin mismatch
        assert case.manifest.case_id == case_id
        assert case.manifest.split == "development"
        assert case.manifest.provenance.executed_here is False
        assert case.manifest.provenance.license == "CC BY-SA 4.0"
        assert case.structure_bytes.splitlines()[0].strip().isdigit()


def test_reference_rows_match_the_questdb_json():
    total = 0
    for molecule in MOLECULES:
        case = load_vte_case(CASES / molecule / f"{molecule}{VTE_MANIFEST_SUFFIX}")
        refs = load_vte_references(case)
        raw = json.loads((RAW / f"{molecule}_mrpt.json").read_text())
        expected = [r for r in raw["data"] if parse_state_label(r["State"]) is not None]
        assert [r.label for r in refs.rows] == [r["State"] for r in expected]
        assert [r.energy_ev for r in refs.rows] == [r["SC-NEVPT2"] for r in expected]
        assert [r.active_space for r in refs.rows] == [r[raw["headers"][1]] for r in expected]
        assert [r.state_average for r in refs.rows] == [r[raw["headers"][2]] for r in expected]
        assert all(not r.label.endswith("[F]") and "[F]" not in r.label for r in refs.rows)
        total += len(refs.rows)
    assert total == TOTAL_TRANSITIONS


def test_fluorescence_rows_are_dropped():
    dropped = 0
    for molecule in MOLECULES:
        raw = json.loads((RAW / f"{molecule}_mrpt.json").read_text())
        dropped += sum(1 for r in raw["data"] if "[F]" in r["State"])
    assert dropped == 2  # ketene and diazomethane


def test_targets_are_the_reference_counts_per_spin_and_irrep():
    for molecule in MOLECULES:
        case = load_vte_case(CASES / molecule / f"{molecule}{VTE_MANIFEST_SUFFIX}")
        refs = load_vte_references(case)
        counts: dict[tuple[int, str], int] = {}
        for r in refs.rows:
            counts[(r.spin, r.irrep)] = counts.get((r.spin, r.irrep), 0) + 1
        assert {(t.spin, t.irrep): t.count for t in case.manifest.targets} == counts


def test_no_reference_content_in_any_manifest():
    for path in _manifests().values():
        text = path.read_text()
        case = load_vte_case(path)
        refs = load_vte_references(case)
        for row in refs.rows:
            assert row.energy_ev not in text, (path, row.label)
            assert row.label not in text, (path, row.label)
            assert row.character not in text, (path, row.label)
        assert "energy_ev" not in text and "state_average" not in text


def test_credit_readme_and_ladder_are_labelled():
    readme = (CASES / "README.md").read_text()
    assert "CC BY-SA 4.0" in readme
    assert "460482bcc6231dab41fe32188dc43959a7c2bdaa" in readme
    ladder = (ROOT / "experiments" / "questdb" / "ladder-from-paper.md").read_text()
    assert "Reconstruction" in ladder or "reconstruction" in ladder
    for rule in "ABCDEFGH":
        assert f"**{rule}." in ladder


def test_builder_is_deterministic(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "questdb_cases", ROOT / "scripts" / "questdb_cases.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    questdb = tmp_path / "q"
    (questdb / "data" / "cas" / "json").mkdir(parents=True)
    (questdb / "geometries" / "xyz").mkdir(parents=True)
    src = ROOT / "experiments" / "questdb-phase0" / "questdb-raw"
    for name in ("ketene_mrpt.json",):
        (questdb / "data" / "cas" / "json" / name).write_bytes(
            (src / "data" / "cas" / "json" / name).read_bytes()
        )
    xyz = src / "geometries" / "xyz" / "ketene_1.xyz"
    if not xyz.exists():
        pytest.skip("raw ketene geometry not vendored")
    (questdb / "geometries" / "xyz" / "ketene_1.xyz").write_bytes(xyz.read_bytes())
    out = tmp_path / "out"
    mod.build_case(questdb, mod.QUESTDB_COMMIT, "ketene", out)
    for name in ("ketene.vte.yaml", "references.json", "ketene.xyz"):
        assert (out / "ketene" / name).read_bytes() == (CASES / "ketene" / name).read_bytes()
