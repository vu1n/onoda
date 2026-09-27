"""M3 end to end on the real pinned Pi: stage -> launch -> ingest -> evaluate -> replay.

Runs are offline under the scripted provider (A32): the scripts, claims and
ladder are synthetic fixtures, and the ORCA output the agent reads is a
vendored upstream file recorded as acquired, never executed here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from conftest import requires_pi

from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi.contract import Script, ScriptTurn
from onoda.adapters.pi.runtime import LiveSettings
from onoda.cli import main
from onoda.clock import LogicalClock
from onoda.context import activation_counts
from onoda.errors import CapabilityError, IntegrityError, OnodaError, ReplayMismatch
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


# ------------------------------------------------------------ review fixes (M3)

FIXTURES = Path(__file__).parent / "pi_fixtures"


def turns_script(source: str, *turns: list[dict], **extra) -> Script:
    return Script.model_validate(
        {"source": source, "turns": [{"content": t} for t in turns], **extra}
    )


def tool(call_id: str, name: str, **args) -> dict:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": args}


def say(t: str) -> dict:
    return {"type": "text", "text": t}


def test_calls_pi_rejects_before_tool_call_still_ingest(tmp_path):
    store, _ = stores(tmp_path)
    bad = {"states": [{"root": 0, "energy": {"text": "1.0", "unit": "eV"}}]}
    s = turns_script(
        "test_rejected", [tool("g1", "grep", pattern="x"), tool("r0", "report_states", **bad)],
        [say("done")],
    )  # fmt: skip
    r = pi_run.run_case(
        store, case(), run_id="rej", variant_id="S0", work_dir=tmp_path / "w", script=s
    )
    assert (r.ingest.status, r.ingest.complete, r.ingest.claim) == ("settled", True, None)
    blocked = [e for e in store.events("rej") if e.type == "pi.tool-blocked"]
    assert {(e.payload["tool_call_id"], e.payload["reason"]) for e in blocked} == {
        ("g1", "rejected by Pi before tool_call"),
        ("r0", "rejected by Pi before tool_call"),
    }


def capture_launch(monkeypatch) -> dict:
    """Replace the Pi launch with one that records the prepared launch and stops."""
    seen: dict = {}

    def fake(prepared, timeout_s):
        seen["policy"] = json.loads(prepared.policy_path.read_text())
        sol_pi = prepared.agent_dir / "sol-pi.json"
        seen["sol_pi"] = json.loads(sol_pi.read_text()) if sol_pi.exists() else None
        seen["env"] = dict(prepared.env)
        raise RuntimeError("launch captured")

    monkeypatch.setattr(pi_run, "launch", fake)
    return seen


def test_live_run_uses_its_own_request_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("ONODA_TEST_KEY", "not-a-real-key")
    monkeypatch.setenv("ONODA_TEST_SECRET", "must-not-leak")
    store, _ = stores(tmp_path)
    live = LiveSettings(
        provider="anthropic",
        model="some-model",
        credential_env="ONODA_TEST_KEY",
        max_requests=3,
        max_cost_usd="0.50",
    )
    with pytest.raises(OnodaError, match="conflicts"):
        pi_run.run_case(
            store, case(), run_id="l0", variant_id="S0", work_dir=tmp_path / "w0",
            live=live, max_requests=16,
        )  # fmt: skip
    seen = capture_launch(monkeypatch)
    with pytest.raises(RuntimeError, match="captured"):
        pi_run.run_case(
            store, case(), run_id="l1", variant_id="S0", work_dir=tmp_path / "w1", live=live
        )
    assert seen["policy"]["max_requests"] == 3
    assert seen["policy"]["max_cost_usd"] == "0.50"
    # Only the named credential variable is passed through, nothing else.
    assert seen["env"]["ONODA_TEST_KEY"] == "not-a-real-key"
    assert "ONODA_TEST_SECRET" not in seen["env"]


def test_cli_passes_the_reducer_and_refuses_bad_budgets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ONODA_TEST_KEY", "not-a-real-key")
    seen = capture_launch(monkeypatch)
    ladder = tmp_path / "ladder.md"
    ladder.write_bytes(LADDER)
    base = ["pi", "run", CASE_ID, "--variant", "S2b", "--store", str(tmp_path / "agent"),
            "--ladder", str(ladder), "--synthetic-ladder"]  # fmt: skip
    live = ["--live", "--provider", "anthropic", "--model", "some-model",
            "--credential-env", "ONODA_TEST_KEY", "--max-cost-usd", "0.5"]  # fmt: skip
    with pytest.raises(RuntimeError, match="captured"):
        main([*base, "--run-id", "a", "--work-dir", str(tmp_path / "wa"), *live,
              "--max-requests", "4", "--reducer-provider", "anthropic",
              "--reducer-model", "reducer-model"])  # fmt: skip
    assert seen["sol_pi"]["evidencePreservingReducerProvider"] == "anthropic"
    assert seen["sol_pi"]["evidencePreservingReducerModel"] == "reducer-model"
    assert seen["policy"]["max_requests"] == 4
    capsys.readouterr()
    for bad in (["--max-requests", "0"], ["--max-requests", "-1"], ["--max-acquisitions", "-1"]):
        code = main([*base, "--run-id", "b", "--work-dir", str(tmp_path / "wb"), *live,
                     "--max-requests", "2", *bad])  # fmt: skip
        assert code == 2
        assert json.loads(capsys.readouterr().err)["error"] == "OnodaError"


def test_budget_exhausted_run_replays_to_match(tmp_path):
    store, _ = stores(tmp_path)
    r = pi_run.run_case(
        store, case(), run_id="bx", variant_id="S0", work_dir=tmp_path / "w",
        script=script("test_bx"), max_requests=2,
    )  # fmt: skip
    assert r.ingest.status == "budget-exhausted"
    out = pi_run.replay_run_pi(store, "bx", case(), work_dir=tmp_path / "r")
    assert (out["status"], out["end_status"], out["requests"]) == ("match", "budget-exhausted", 2)


def test_script_exhausted_run_replays_only_as_script_exhausted(tmp_path, monkeypatch):
    store, _ = stores(tmp_path)
    s = turns_script("test_ex", [tool("c1", "case_input")])
    r = pi_run.run_case(
        store, case(), run_id="ex", variant_id="S0", work_dir=tmp_path / "w", script=s
    )
    assert r.ingest.status == "script-exhausted"
    from onoda.adapters.pi.ingest import recorded_script

    assert len(recorded_script(store, "ex").turns) == 1  # the error response is not a turn
    out = pi_run.replay_run_pi(store, "ex", case(), work_dir=tmp_path / "r1")
    assert (out["status"], out["end_status"]) == ("match", "script-exhausted")

    failed = [store.receipt(i) for i in r.ingest.invocations]
    (failed,) = [x for x in failed if x.metadata.get("stop_reason") == "error"]

    def settles(store_, run_id):  # the old behaviour: the error became an ordinary turn
        real = recorded_script(store_, run_id)
        extra = {"content": [say("done")], "expect_replay_key": failed.metadata["replay_key"]}
        return real.model_copy(update={"turns": (*real.turns, ScriptTurn.model_validate(extra))})

    monkeypatch.setattr(pi_run, "recorded_script", settles)
    with pytest.raises(ReplayMismatch, match="settled"):
        pi_run.replay_run_pi(store, "ex", case(), work_dir=tmp_path / "r2")


@pytest.mark.parametrize("then_run", ["orca x", {"command": "python3 -c 1"}, {"command": 7}])
def test_then_run_outside_action_fusion_is_a_plain_call(tmp_path, then_run):
    store, _ = stores(tmp_path)
    s = turns_script(
        "test_then_run",
        [tool("w1", "write", path="a.txt", content="hi\n", then_run=then_run)],
        [say("done")],
    )
    r = pi_run.run_case(
        store, case(), run_id="tr", variant_id="S0", work_dir=tmp_path / "w", script=s
    )
    assert r.ingest.status == "settled"
    events = store.events("tr")
    assert "context.fused-step" not in [e.type for e in events]
    blocked = [e for e in events if e.type == "pi.tool-blocked"]
    if isinstance(then_run, dict) and isinstance(then_run["command"], str):
        assert not blocked  # Pi ignores then_run here; the write is a plain call
        assert (tmp_path / "w" / "pi" / "workspace" / "a.txt").exists()
    else:
        assert blocked[0].payload["reason"] == "then_run has no string command"
        assert blocked[0].payload["fused"] is False


def test_s2b_reducer_run_replays_to_match(tmp_path):
    from test_pi_extensions import REDUCER_COMMAND, REDUCER_LOG, receipt, sha

    store, _ = stores(tmp_path)
    s = turns_script(
        "test_s2b",
        [tool("b1", "bash", command=REDUCER_COMMAND)],
        [say("done")],
        reducer_turns=[{"content": [say(receipt(sha(REDUCER_LOG.encode())))]}],
    )
    r = pi_run.run_case(
        store, case(), run_id="s2b", variant_id="S2b", work_dir=tmp_path / "w", script=s,
        ladder=LADDER, synthetic_ladder=True,
    )  # fmt: skip
    assert r.ingest.status == "settled"
    assert "context.reduced" in [e.type for e in store.events("s2b")]
    out = pi_run.replay_run_pi(store, "s2b", case(), work_dir=tmp_path / "replay-elsewhere")
    assert (out["status"], out["reducer_requests"]) == ("match", 1)


def test_truncated_bash_output_replays_to_match(tmp_path):
    # 3000 lines exceed Pi's 2000-line limit: Pi spills to a random temp log path.
    store, _ = stores(tmp_path)
    cmd = "python3 -c \"print('y\\n' * 3000, end='')\""
    s = turns_script("test_trunc", [tool("b1", "bash", command=cmd)], [say("done")])
    pi_run.run_case(store, case(), run_id="tb", variant_id="S0", work_dir=tmp_path / "w", script=s)
    shown = [e for e in store.events("tb") if e.type == "invocation.committed"]
    assert shown
    assert pi_run.replay_run_pi(store, "tb", case(), work_dir=tmp_path / "r")["status"] == "match"


def with_extension(monkeypatch, ext: Path) -> None:
    """Load a test-only extension just before the recorder in every launch."""
    real = pi_run.build_launch

    def build(**kw):
        prepared = real(**kw)
        argv = list(prepared.argv)
        at = len(argv) - 1 - argv[::-1].index("-e")  # the recorder's -e
        argv[at:at] = ["-e", str(ext)]
        return dataclasses.replace(prepared, argv=tuple(argv))

    monkeypatch.setattr(pi_run, "build_launch", build)


def test_compaction_summaries_are_receipts_and_replay(tmp_path, monkeypatch):
    with_extension(monkeypatch, FIXTURES / "compact-on-settle.ts")
    store, _ = stores(tmp_path)
    s = turns_script(
        "test_compact",
        [tool("o1", "orca_run"), tool("o2", "orca_run")],
        [say("done")],
        summary_turns=[{"content": [say("synthetic summary")]}] * 2,
    )
    r = pi_run.run_case(
        store, case(), run_id="cmp", variant_id="S0", work_dir=tmp_path / "w", script=s,
        max_acquisitions=2,
    )  # fmt: skip
    assert "context.compaction" in [e.type for e in store.events("cmp")]
    assert r.ingest.status == "settled"
    receipts = [store.receipt(i) for i in r.ingest.invocations]
    summaries = [x for x in receipts if x.metadata.get("role") == "compaction-summary"]
    assert summaries and all("compaction-summary" in x.labels for x in summaries)
    assert all(x.outputs.get("message") for x in summaries)
    from onoda.adapters.pi.ingest import recorded_script

    assert len(recorded_script(store, "cmp").summary_turns) == len(summaries)
    out = pi_run.replay_run_pi(store, "cmp", case(), work_dir=tmp_path / "r")
    assert (out["status"], out["summary_requests"]) == ("match", len(summaries))


def test_imported_run_is_never_replayed(tmp_path):
    from onoda.portability import export_bundle, import_bundle

    store, _ = stores(tmp_path)
    pi_run.run_case(
        store, case(), run_id="s0", variant_id="S0", work_dir=tmp_path / "w",
        script=script("test_import"),
    )  # fmt: skip
    export_bundle(store, ["s0"], tmp_path / "bundle")
    other = Store(tmp_path / "other", clock=LogicalClock())
    import_bundle(tmp_path / "bundle", other)
    with pytest.raises(CapabilityError, match="imported"):
        pi_run.replay_run_pi(other, "s0", case(), work_dir=tmp_path / "r")
    assert not (tmp_path / "r").exists()  # refused before anything was staged or run


def test_replay_refuses_a_task_doc_name_that_is_a_path(tmp_path):
    from onoda.records import Run, TaskManifest

    store, _ = stores(tmp_path)
    doc = store.put_bytes(b"x", media_type="text/markdown").digest
    task = TaskManifest.new(
        task_id="t", inputs={"doc:../../escape.md": doc}, permitted_context=[], split="dev",
        target={"kind": "orca-case", "case_id": CASE_ID}, synthetic=True,
    )  # fmt: skip
    store.put_record(task)
    store.put_record(
        Run.new(
            run_id="forged",
            experiment_id="pi-dev",
            task="t",
            config={"mode": "pi-scripted", "variant": "S0", "case_id": CASE_ID},
            quota_id="q",
            created_at=store.clock.now(),
            labels=[],
        )  # fmt: skip
    )
    with pytest.raises(OnodaError, match="plain"):
        pi_run.replay_run_pi(store, "forged", case(), work_dir=tmp_path / "r")
    assert not (tmp_path / "escape.md").exists()


def test_secrets_stay_out_of_tools_and_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ONODA_TEST_SECRET", "hunter2-synthetic-secret")
    store, _ = stores(tmp_path)
    s = turns_script(
        "test_env", [tool("b1", "bash", command="env; echo spool=$ONODA_PI_SPOOL.")], [say("done")]
    )
    pi_run.run_case(store, case(), run_id="env", variant_id="S0", work_dir=tmp_path / "w", script=s)
    (bash,) = [
        store.receipt(i)
        for i in (
            e.payload["invocation_id"] for e in store.events("env") if "invocation_id" in e.payload
        )
        if store.receipt(i).metadata.get("tool_name") == "bash"
    ]
    shown = store.get_bytes(bash.outputs["result"]).decode()
    assert "PATH=" in shown and "spool=." in shown  # ran, with ONODA_PI_SPOOL unset
    assert "ONODA_PI_" not in shown and "hunter2" not in shown
    for path in (tmp_path / "agent").rglob("*"):
        if path.is_file():
            assert b"hunter2" not in path.read_bytes(), path
    assert not (tmp_path / "w" / "pi" / "spool.key").exists()


def test_spool_rewritten_after_the_run_is_refused(tmp_path, monkeypatch):
    real = pi_run.ingest_spool

    def tamper_then_ingest(store_, spool_dir, **kw):
        events = Path(spool_dir) / "events.jsonl"
        lines = events.read_bytes().splitlines(keepends=True)
        for n, raw in enumerate(lines):
            data = json.loads(raw)
            if data["type"] == "tool.call":
                forged = b'{"forged": true}'
                digest = hashlib.sha256(forged).hexdigest()
                (Path(spool_dir) / "objects" / digest).write_bytes(forged)
                lines[n] = raw.replace(data["input"].encode(), digest.encode())
                break
        events.write_bytes(b"".join(lines))
        assert kw["key"]  # run_case hands ingest the launch's key
        return real(store_, spool_dir, **kw)

    monkeypatch.setattr(pi_run, "ingest_spool", tamper_then_ingest)
    store, _ = stores(tmp_path)
    with pytest.raises(IntegrityError, match="mac"):
        pi_run.run_case(
            store, case(), run_id="t", variant_id="S0", work_dir=tmp_path / "w",
            script=script("test_tamper"),
        )  # fmt: skip
    assert not store.has_record("run", "t")


def test_two_variants_on_one_case_share_a_store(tmp_path):
    # Every ablation arm stages the same case into one store; the task id must
    # not collide across variants (their manifests differ in profile_versions).
    store, _ = stores(tmp_path)
    for variant in ("S0", "S2d"):
        r = pi_run.run_case(
            store, case(), run_id=f"v-{variant}", variant_id=variant,
            work_dir=tmp_path / variant, script=script(f"test_{variant}"),
            ladder=LADDER if variant != "S0" else None, synthetic_ladder=variant != "S0",
        )  # fmt: skip
        assert r.ingest.status == "settled"
    tasks = {store.run_record(f"v-{v}").task for v in ("S0", "S2d")}
    assert len(tasks) == 2
