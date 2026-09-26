"""M3 end to end on the real pinned Pi: stage -> launch -> ingest -> evaluate -> replay.

Runs are offline under the scripted provider (A32): the scripts, claims and
ladder are synthetic fixtures, and the ORCA output the agent reads is a
vendored upstream file recorded as acquired, never executed here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import requires_pi

from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi.contract import Script
from onoda.adapters.pi.runtime import LiveSettings
from onoda.cli import main
from onoda.clock import LogicalClock
from onoda.context import activation_counts
from onoda.errors import CapabilityError, ReplayMismatch
from onoda.replay import replay_run
from onoda.science.contract import case_manifests, load_case
from onoda.science.orca_parser import parse_orca_output
from onoda.store import Store

pytestmark = requires_pi

CASE_ID = "orca6-water-mp2"  # a 43 KB output: large enough for ObservationPack
LADDER = b"# Synthetic advisory ladder (test fixture, no scientific content)\n"


def case():
    return load_case(case_manifests()[CASE_ID])


def claim_args() -> dict:
    """A claim copied from the deterministic parse: what a careful agent would report."""
    ((_, out),) = case().output_bytes.items()
    energy = parse_orca_output(out).final_energy
    return {
        "final_energy": {"text": energy.value, "unit": energy.units},
        "states": [],
        "notes": "synthetic scripted claim",
    }


def script(source: str, claim: dict | None = None) -> Script:
    def call(i: int, name: str, **args):
        return {"type": "toolCall", "id": f"c{i}", "name": name, "arguments": args}

    return Script.model_validate(
        {
            "schema": "onoda.pi-script/1",
            "source": source,
            "turns": [
                {"content": [call(1, "case_input")]},
                {"content": [call(2, "orca_run")]},
                {"content": [call(3, "report_states", **(claim or claim_args()))]},
                {"content": [{"type": "text", "text": "reported"}]},
            ],
        }
    )


def stores(tmp_path: Path) -> tuple[Store, Store]:
    return (
        Store(tmp_path / "agent", clock=LogicalClock()),
        Store(tmp_path / "evaluator", clock=LogicalClock()),
    )


def evaluate(store: Store, evaluator: Store, run_id: str) -> dict:
    from onoda.adapters.pi.ingest import run_claim
    from onoda.adapters.pi.task import evaluate_claims

    claims = [e for e in store.events(run_id) if e.type == "pi.claim"]
    ev = evaluate_claims(
        evaluator,
        run_id=run_id,
        case=case(),
        claim=run_claim(store, run_id),
        claim_ref=claims[-1].payload_refs["claim"],
    )
    return ev.metrics


def test_s0_runs_ingests_evaluates_and_replays(tmp_path):
    store, evaluator = stores(tmp_path)
    pi = pi_run.run_case(
        store,
        case(),
        run_id="s0",
        variant_id="S0",
        work_dir=tmp_path / "w",
        script=script("test_s0"),
        docs={"notes.md": b"# Synthetic allowed doc\n"},
    )
    r = pi.ingest
    assert (r.status, r.complete) == ("settled", True)
    assert r.claim is not None and r.observation_pack == {}

    report = replay_run(store, "s0")
    assert report["external_calls"] == 0
    run = store.run_record("s0")
    assert {"pi-scripted", "S0", "synthetic"} <= set(run.labels)
    final = store.checkpoints("s0")[-1][2]
    assert final.boundary == "finalized" and "task.case" not in final.visible

    metrics = evaluate(store, evaluator, "s0")
    assert metrics["false_success_claims"] == "0"
    assert not store.list_record_ids("evaluation")  # grading stays in the evaluator store

    assert pi_run.replay_run_pi(store, "s0", case(), work_dir=tmp_path / "r")["status"] == "match"


def test_s3_full_stack_records_mechanisms_and_replays(tmp_path):
    store, evaluator = stores(tmp_path)
    pi = pi_run.run_case(
        store,
        case(),
        run_id="s3",
        variant_id="S3",
        work_dir=tmp_path / "w",
        script=script("test_s3"),
        ladder=LADDER,
        synthetic_ladder=True,
    )
    r = pi.ingest
    assert (r.status, r.complete) == ("settled", True)
    # The 43 KB orca_run output is archived by SoL-Pi and mapped into the store.
    assert len(r.observation_pack) == 1
    assert all(store.has_object(d) for d in r.observation_pack.values())
    assert activation_counts(store, "s3").get("context.observation-archived") == 1
    ctx = store.checkpoints("s3")[-1][2].context
    assert ctx["pi.variant"] == "S3" and ctx["pi.mechanisms"]["observationPack"] is True
    replay_run(store, "s3")
    assert evaluate(store, evaluator, "s3")["false_success_claims"] == "0"
    assert pi_run.replay_run_pi(store, "s3", case(), work_dir=tmp_path / "r")["status"] == "match"


def test_wrong_claim_counts_as_false_success(tmp_path):
    store, evaluator = stores(tmp_path)
    bad = claim_args()
    bad["final_energy"]["text"] = "-1.0"
    pi_run.run_case(
        store,
        case(),
        run_id="bad",
        variant_id="S0",
        work_dir=tmp_path / "w",
        script=script("test_bad", bad),
    )
    assert evaluate(store, evaluator, "bad")["false_success_claims"] == "1"


def test_replay_fails_closed_when_the_ladder_drifts(tmp_path, monkeypatch):
    store, _ = stores(tmp_path)
    pi_run.run_case(
        store,
        case(),
        run_id="s1",
        variant_id="S1",
        work_dir=tmp_path / "w",
        script=script("test_s1"),
        ladder=LADDER,
        synthetic_ladder=True,
    )
    real_stage = pi_run.stage_bundle

    def drifted(case_, dest, **kw):
        return real_stage(case_, dest, **{**kw, "ladder": LADDER + b"one more line\n"})

    monkeypatch.setattr(pi_run, "stage_bundle", drifted)
    with pytest.raises(ReplayMismatch):
        pi_run.replay_run_pi(store, "s1", case(), work_dir=tmp_path / "r")


@pytest.mark.parametrize(
    ("variant", "kwargs"),
    [
        ("S1", {}),  # ladder variant without a ladder (A33)
        ("S1r", {"ladder": LADDER}),  # RLM not built in M3
    ],
)
def test_refused_variants_commit_nothing(tmp_path, variant, kwargs):
    store, _ = stores(tmp_path)
    with pytest.raises(CapabilityError):
        pi_run.run_case(
            store,
            case(),
            run_id="x",
            variant_id=variant,
            work_dir=tmp_path / "w",
            script=script("test_refused"),
            **kwargs,
        )
    assert not store.list_record_ids("run") and not store.list_record_ids("task")


def test_live_run_without_every_setting_is_refused(tmp_path):
    store, _ = stores(tmp_path)
    with pytest.raises(CapabilityError):
        pi_run.run_case(
            store,
            case(),
            run_id="live",
            variant_id="S0",
            work_dir=tmp_path / "w",
            live=LiveSettings(provider="anthropic", model="some-model"),
        )
    assert not store.list_record_ids("run")


def test_cli_run_evaluate_replay(tmp_path, capsys):
    script_path = tmp_path / "script.json"
    script_path.write_text(script("test_cli").model_dump_json(by_alias=True))
    agent, ev = str(tmp_path / "agent"), str(tmp_path / "evaluator")
    assert (
        main(
            [
                "pi",
                "run",
                CASE_ID,
                "--variant",
                "S0",
                "--run-id",
                "cli",
                "--store",
                agent,
                "--work-dir",
                str(tmp_path / "w"),
                "--script",
                str(script_path),
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert (out["status"], out["complete"]) == ("settled", True)
    assert main(["pi", "evaluate", "cli", "--store", agent, "--evaluator-store", ev]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["metrics"]["false_success_claims"] == "0" and out["synthetic_run"] is True
    assert main(["pi", "replay", "cli", "--store", agent, "--work-dir", str(tmp_path / "r")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"
