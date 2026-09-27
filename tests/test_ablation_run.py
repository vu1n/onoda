"""M3b end to end: the default arms on the real pinned Pi, offline (scripted, synthetic)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import requires_pi

from onoda.ablation.contract import DEFAULT_ARMS, REPORT_SCHEMA
from onoda.ablation.run import run_ablation, run_id_for
from onoda.cli import main
from onoda.clock import LogicalClock
from onoda.errors import OnodaError
from onoda.science.contract import CASES_ROOT
from onoda.store import Store

FIXTURES = Path(__file__).parent / "ablation_fixtures"
CASE_ID = "orca6-water-mp2"


def stores(tmp_path: Path) -> tuple[Store, Store]:
    return (
        Store(tmp_path / "agent", clock=LogicalClock()),
        Store(tmp_path / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",)),
    )


def spec_copy(tmp_path: Path, **changes) -> Path:
    """The fixture spec, copied beside its files with `changes` applied (None deletes a key)."""
    dest = tmp_path / "spec"
    shutil.copytree(FIXTURES, dest)
    spec = yaml.safe_load((dest / "spec.yaml").read_text())
    for k, v in changes.items():
        if v is None:
            spec.pop(k, None)
        else:
            spec[k] = v
    (dest / "spec.yaml").write_text(yaml.safe_dump(spec))
    return dest / "spec.yaml"


@requires_pi
def test_default_arms_run_compare_and_report(tmp_path):
    store, evaluator = stores(tmp_path)
    digest, report = run_ablation(
        FIXTURES / "spec.yaml", store=store, evaluator_store=evaluator, work_dir=tmp_path / "w"
    )
    assert report["schema"] == REPORT_SCHEMA
    assert report["evidence"] == "synthetic" and "not a mechanism verdict" in report["caveat"]
    assert [r["arm"] for r in report["runs"]] == list(DEFAULT_ARMS)
    assert all(r["status"] == "settled" and r["synthetic"] for r in report["runs"])

    # Each single-mechanism arm activates its own mechanism and only that one
    # (S2c's compaction does not trigger on a run this short); S3 activates all three.
    act = {a: p["activation_by_model"]["scripted"] for a, p in report["arms"].items()}
    assert act["S1"] == dict.fromkeys(act["S1"], "0/1")
    assert act["S2a"]["observationPack"] == "1/1"
    assert act["S2b"]["evidencePreservingReducer"] == "1/1"
    assert act["S2d"]["actionFusion"] == "1/1"
    assert all(act["S3"][m] == "1/1" for m in ("observationPack", "evidencePreservingReducer"))
    assert act["S3"]["actionFusion"] == "1/1"
    assert report["arms"]["S1"]["mechanism_config"] == ["none"]
    assert len(set(report["arms"]["S3"]["mechanism_config"]) - {"none"}) == 1

    # The same claim in every arm: capability is identical, and the one metric the
    # fixture tolerances leave undeclared keeps every capability gate unresolved (A39).
    comps = {c["arm"]: c for c in report["comparisons"]}
    assert set(comps) == set(DEFAULT_ARMS) - {"S1"}
    for c in comps.values():
        assert c["capability_gate"] == "unresolved"
        unresolved = [m["metric"] for m in c["capability"] if m["result"] == "unresolved"]
        assert unresolved == ["count.unresolved"]
        assert c["verdict"] != "eligible"
    # ObservationPack archives the 43 KB output: measured payload bytes drop.
    bytes_ = {a: int(p["efficiency"]["context_bytes.total"]) for a, p in report["arms"].items()}
    assert bytes_["S2a"] < bytes_["S1"] and bytes_["S3"] < bytes_["S1"]
    assert comps["S2a"]["efficiency_gate"] == "pass"
    assert report["non_dominated"] == []  # nothing is eligible

    term = report["interaction"]["context_bytes.total"]
    assert int(term["full_delta"]) == bytes_["S3"] - bytes_["S1"]
    assert int(term["sum_single_deltas"]) == sum(
        bytes_[a] - bytes_["S1"] for a in ("S2a", "S2b", "S2c", "S2d")
    )
    assert int(term["interaction"]) == int(term["full_delta"]) - int(term["sum_single_deltas"])

    # The report and every evaluation live in the evaluator store only.
    desc = evaluator.descriptor(digest)
    assert desc.visibility == "evaluator" and {"ablation-report", "synthetic"} <= set(desc.labels)
    assert not store.has_object(digest)
    assert evaluator.get_json(digest)["runs"] == report["runs"]
    for arm in DEFAULT_ARMS:
        assert store.has_record("run", run_id_for("fixture", arm, CASE_ID))


@requires_pi
def test_an_ablation_id_is_never_rerun(tmp_path):
    store, evaluator = stores(tmp_path)
    spec = spec_copy(tmp_path, arms=["S1", "S2a"])
    run_ablation(spec, store=store, evaluator_store=evaluator, work_dir=tmp_path / "w1")
    with pytest.raises(OnodaError, match="already has runs"):
        run_ablation(spec, store=store, evaluator_store=evaluator, work_dir=tmp_path / "w2")


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"arms": ["S1", "S1r"]}, "RLM"),
        ({"baseline": "S0"}, "baseline"),
        ({"ladder": None}, "ladder"),
        ({"efficiency_gate": ["wall_clock"]}, "unknown efficiency metric"),
        ({"cases": [{"case_id": "no-such-case", "script": "water.script.json"}]}, "unknown case"),
        ({"cases": [{"case_id": CASE_ID, "script": "missing.json"}]}, "cannot read script"),
        ({"cases": [{"case_id": CASE_ID, "script": "ladder.md"}]}, "invalid script"),
        ({"ladder": "missing.md"}, "cannot read ladder"),
    ],
)
def test_bad_specs_are_refused_before_any_run(tmp_path, changes, match):
    store, evaluator = stores(tmp_path)
    with pytest.raises(OnodaError, match=match):
        run_ablation(
            spec_copy(tmp_path, **changes),
            store=store,
            evaluator_store=evaluator,
            work_dir=tmp_path / "w",
        )
    assert store.list_record_ids("run") == []


def test_false_success_tolerance_cannot_be_loosened(tmp_path):
    spec = spec_copy(tmp_path)
    tol = spec.parent / "tolerances.yaml"
    data = yaml.safe_load(tol.read_text())
    data["capability"]["false_success_claims"] = "1"
    tol.write_text(yaml.safe_dump(data))
    store, evaluator = stores(tmp_path)
    with pytest.raises(OnodaError, match="fixed at 0"):
        run_ablation(spec, store=store, evaluator_store=evaluator, work_dir=tmp_path / "w")


def test_a_held_out_case_is_never_read(tmp_path):
    cases = tmp_path / "cases"
    shutil.copytree(CASES_ROOT, cases)
    manifest = next(cases.glob(f"**/{CASE_ID}.case.yaml"))
    manifest.write_text(manifest.read_text().replace("split: development", "split: test"))
    store, evaluator = stores(tmp_path)
    with pytest.raises(OnodaError, match="A42"):
        run_ablation(
            FIXTURES / "spec.yaml",
            store=store,
            evaluator_store=evaluator,
            work_dir=tmp_path / "w",
            cases_root=cases,
        )
    assert store.list_record_ids("run") == []


@requires_pi
def test_cli_run_and_show(tmp_path, capsys):
    spec = spec_copy(tmp_path, arms=["S1", "S2d"])
    base = [f"--store={tmp_path / 'a'}", f"--evaluator-store={tmp_path / 'e'}"]
    assert main(["ablation", "run", str(spec), *base, f"--work-dir={tmp_path / 'w'}"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["evidence"] == "synthetic" and set(out["verdicts"]) == {"S2d"}
    assert main(["ablation", "show", out["report"], base[1]]) == 0
    assert json.loads(capsys.readouterr().out)["ablation_id"] == "fixture"


def test_report_is_built_offline_from_outcome_vectors():
    from test_ablation_gates import make_vector

    from onoda.ablation.contract import AblationSpec
    from onoda.ablation.run import build_report

    spec = AblationSpec.model_validate(
        yaml.safe_load((FIXTURES / "spec.yaml").read_text()) | {"arms": ["S1", "S2a", "S3"]}
    )
    lean = {"context_bytes.total": "60", "acquisitions": "1"}
    vectors = {
        "S1": [make_vector(arm="S1", case_id=CASE_ID)],
        "S2a": [make_vector(arm="S2a", case_id=CASE_ID, efficiency=lean)],
        "S3": [make_vector(arm="S3", case_id=CASE_ID, efficiency=lean)],
    }
    report = build_report(spec, None, vectors)
    assert report["evidence"] == "synthetic" and report["baseline"] == "S1"
    assert [c["verdict"] for c in report["comparisons"]] == ["unresolved", "unresolved"]
    # Only S2a is present of the singles, so the S2 sum (and the interaction) is unknown.
    term = report["interaction"]["context_bytes.total"]
    assert term == {"full_delta": "-40", "sum_single_deltas": None, "interaction": None}
    assert report["arms"]["S2a"]["efficiency"]["cost_usd"] is None  # unreported by `lean`


def test_the_evaluator_store_must_be_separate(tmp_path):
    store = Store(tmp_path / "same", clock=LogicalClock())
    for evaluator in (store, Store(tmp_path / "same", clock=LogicalClock())):
        with pytest.raises(OnodaError, match="separate store"):
            run_ablation(
                FIXTURES / "spec.yaml", store=store, evaluator_store=evaluator, work_dir=tmp_path
            )


@pytest.mark.parametrize("key", ["ladder", "tolerances"])
def test_spec_paths_stay_in_the_spec_directory(tmp_path, key):
    (tmp_path / "outside.md").write_text("not for the agent\n")
    store, evaluator = stores(tmp_path)
    with pytest.raises(OnodaError, match="outside the spec"):
        run_ablation(
            spec_copy(tmp_path, **{key: "../outside.md"}),
            store=store,
            evaluator_store=evaluator,
            work_dir=tmp_path / "w",
        )
