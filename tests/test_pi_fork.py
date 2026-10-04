"""Forking a recorded Pi run (A36): replay the parent's prefix, then continue differently.

Every run here is scripted on the real pinned Pi, so its outputs are synthetic.
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import test_pi_vte_end_to_end as v
from conftest import requires_pi
from test_pi_end_to_end import (
    FIXTURES,
    LADDER,
    case,
    claim_args,
    script,
    stores,
    tool,
    turns_script,
)

from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi.fork import fork_pi_run, fork_point
from onoda.adapters.pi.runtime import LiveSettings
from onoda.errors import CapabilityError, ForkError, ReplayMismatch
from onoda.records import ForkDeclaration
from onoda.science.orca_broker import read_journal
from onoda.store import Store

pytestmark = requires_pi


def checkpoint(store: Store, run_id: str, boundary: str) -> str:
    return next(d for _, d, cp in store.checkpoints(run_id) if cp.boundary == boundary)


def agent_keys(store: Store, run_id: str) -> list[str]:
    return pi_run._recorded_requests(store, run_id)["agent"]


def parent(tmp_path: Path, **kw) -> Store:
    store, _ = stores(tmp_path)
    pi_run.run_case(
        store,
        case(),
        run_id="p",
        variant_id="S1",
        work_dir=tmp_path / "p",
        script=script("test_fork_parent"),
        ladder=LADDER,
        synthetic_ladder=True,
        **kw,
    )
    return store


def wrong_claim() -> dict:
    claim = claim_args()
    claim["final_energy"] = {**claim["final_energy"], "text": "-1.0"}
    return claim


CONTINUATION = turns_script(
    "test_fork_continuation",
    [tool("k1", "report_states", **wrong_claim())],
    [{"type": "text", "text": "reported something else"}],
)


def fork(store: Store, tmp_path: Path, at: str, **kw):
    return fork_pi_run(
        store,
        "p",
        at,
        case(),
        new_run_id=kw.pop("new_run_id", "f"),
        continuation=kw.pop("continuation", CONTINUATION),
        reason="what if the agent had reported a different energy",
        work_dir=tmp_path / kw.pop("work", "f"),
        **kw,
    )


def test_a_fork_replays_the_prefix_then_continues_with_its_own_answers(tmp_path):
    store = parent(tmp_path)
    before = len(store.events("p"))
    at = checkpoint(store, "p", "job-collected")
    point = fork_point(store, "p", at)
    assert point.resume_request == 2 and point.counts()["agent"] == 2

    pi = fork(store, tmp_path, at)
    assert (pi.ingest.status, pi.ingest.complete) == ("settled", True)
    assert len(store.events("p")) == before  # the parent is never written

    child = store.run_record("f")
    assert child.parent is not None and child.parent.run_id == "p" and child.fork is not None
    assert child.parent.checkpoint == at and "forked" in child.labels
    decl = store.get_record("fork", child.fork)
    assert isinstance(decl, ForkDeclaration)
    assert decl.intervention["kind"] == "decision-policy-change"
    assert decl.intervention["from_policy"] == f"script:{store.run_record('p').config['script']}"
    assert decl.invalidated == [] and decl.context_translation == "replay-prefix"
    cut = store.get_record("checkpoint", at).event_seq
    assert decl.visibility_cut == {"p": cut}
    assert all(cp.ancestry_cut == {"p": cut} for _, _, cp in store.checkpoints("f"))
    assert child.config["fork"]["resume_request"] == 2
    assert child.config["fork"]["prefix"] == {"agent": 2, "reducer": 0, "compaction-summary": 0}
    types = [e.type for e in store.events("f")]
    assert types[:2] == ["run.created", "fork.created"]

    # Same context as the parent up to the fork point, its own answers after it.
    assert agent_keys(store, "f")[:3] == agent_keys(store, "p")[:3]
    claims = {r: [e for e in store.events(r) if e.type == "pi.claim"] for r in ("p", "f")}
    assert claims["f"][-1].payload_refs["claim"] != claims["p"][-1].payload_refs["claim"]


def test_a_fork_from_the_start_has_no_prefix(tmp_path):
    store = parent(tmp_path)
    at = checkpoint(store, "p", "initialized")
    assert fork_point(store, "p", at).counts()["agent"] == 0
    cont = turns_script("test_fork_from_start", [{"type": "text", "text": "done"}])
    pi = fork(store, tmp_path, at, continuation=cont)
    assert pi.ingest.claim is None


def test_fresh_allowance_adds_the_parents_cap_on_top_of_the_prefix(tmp_path):
    store = parent(tmp_path)
    fork(
        store, tmp_path, checkpoint(store, "p", "job-collected"), budget_treatment="fresh-allowance"
    )
    parent_cap = store.run_record("p").config["max_requests"]
    assert store.run_record("f").config["max_requests"] == parent_cap + 2
    assert store.get_record("fork", store.run_record("f").fork).budget_treatment == (
        "fresh-allowance"
    )


def test_a_fork_whose_prefix_drifts_is_recorded_and_refused(tmp_path, monkeypatch):
    store = parent(tmp_path)
    real_stage = pi_run.stage_bundle

    def drifted(case_, dest, **kw):
        return real_stage(case_, dest, **{**kw, "ladder": LADDER + b"one more line\n"})

    monkeypatch.setattr(pi_run, "stage_bundle", drifted)
    with pytest.raises(ReplayMismatch, match="diverged"):
        fork(store, tmp_path, checkpoint(store, "p", "job-collected"))
    ends = [e for e in store.events("f") if e.type == "pi.spool-end"]
    assert ends[-1].payload["status"] == "replay-mismatch"


def test_forks_that_cannot_continue_the_parent_are_refused(tmp_path):
    store = parent(tmp_path)
    at = checkpoint(store, "p", "job-collected")
    with pytest.raises(ForkError, match="run's end"):
        fork(store, tmp_path, checkpoint(store, "p", "finalized"))
    with pytest.raises(ForkError, match="not journaled"):
        fork(store, tmp_path, "sha256:" + "0" * 64)
    with pytest.raises(ForkError, match="unknown parent"):
        fork_point(store, "nobody", at)
    with pytest.raises(ForkError, match="needs a reason"):
        fork_pi_run(
            store, "p", at, case(), new_run_id="f", continuation=CONTINUATION, reason=" ",
            work_dir=tmp_path / "f",
        )  # fmt: skip
    with pytest.raises(ForkError, match="already exists"):
        fork(store, tmp_path, at, new_run_id="p")
    assert not store.has_record("run", "f")


# --- VTE: the broker replays the parent's jobs, then runs the continuation's


@pytest.fixture
def short():
    """A short work root: the broker socket path must fit AF_UNIX (104 bytes on macOS)."""
    d = Path(tempfile.mkdtemp(prefix="fork-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_a_vte_fork_replays_the_parents_jobs_then_runs_new_ones(short):
    store, _ = v.stores(short)
    binary = v.stand_in(short)
    v.run(store, short, "p", binary, v.script("test_vte_fork_parent", v.INPUT, v.INPUT))
    assert v.runs(binary) == 1  # the second submission was a cache hit
    at = checkpoint(store, "p", "job-collected")  # after the first of the turn's two jobs
    point = fork_point(store, "p", at)
    assert point.counts()["agent"] == 2  # the rest of that turn is in the prefix

    new_input = v.INPUT.replace("aug-cc-pVTZ", "aug-cc-pVDZ")
    cont = turns_script(
        "test_vte_fork_continuation",
        [tool("n1", "orca_submit", input=new_input)],
        [tool("r1", "report_vtes", states=[v.state("j0003")], notes="synthetic scripted claim")],
        [{"type": "text", "text": "reported"}],
    )
    fork_pi_run(
        store,
        "p",
        at,
        v.case(),
        new_run_id="f",
        continuation=cont,
        reason="what if the agent had tried a smaller basis next",
        work_dir=short / "f",
        orca=v.orca(short, binary),
        cache_dir=short / "cache",
        timeout_s=120,
    )
    assert v.runs(binary) == 2  # only the continuation's job ran
    child = store.run_record("f")
    assert child.config["fork"]["prefix"]["jobs"] == 2
    journal = read_journal(short / "f" / "broker" / "broker.jsonl")
    replies = [e for e in journal if e.event == "reply"]
    assert [(e.replay, e.executed_here) for e in replies] == [
        (True, False),
        (True, False),
        (False, True),
    ]
    assert set(v.jobs(store, "f")) == {"j0001", "j0002", "j0003"}


def test_check_prefix_compares_what_each_prefix_tool_call_showed(tmp_path):
    from onoda.adapters.pi.fork import check_prefix
    from onoda.science.contract import case_manifests, load_case

    store = parent(tmp_path)
    at = checkpoint(store, "p", "job-collected")
    fork(store, tmp_path, at)  # checked on the way: the same results
    point = fork_point(store, "p", at)
    check_prefix(store, point, "f")
    # Another case under the same tool call ids shows different results.
    other = next(c for c in sorted(case_manifests()) if c != case().manifest.case_id)
    pi_run.run_case(
        store,
        load_case(case_manifests()[other]),
        run_id="q",
        variant_id="S0",
        work_dir=tmp_path / "q",
        script=turns_script(
            "test_other_case", [tool("c1", "case_input")], [{"type": "text", "text": "ok"}]
        ),
    )
    with pytest.raises(ReplayMismatch, match="tool call c1"):
        check_prefix(store, point, "q")


def test_cli_fork_and_the_fixture_fork_points_pi_runs_to_it(tmp_path, capsys):
    import json

    from onoda.cli import main

    store = parent(tmp_path)
    at = checkpoint(store, "p", "job-collected")
    script_path = tmp_path / "continuation.json"
    script_path.write_text(CONTINUATION.model_dump_json(by_alias=True))
    agent = str(tmp_path / "agent")
    argv = ["pi", "fork", "p", "--checkpoint", at, "--new-run", "f", "--store", agent]
    argv += ["--work-dir", str(tmp_path / "f"), "--script", str(script_path)]
    assert main([*argv, "--reason", "cli fork"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["parent"] == "p" and out["complete"] and out["run_id"] == "f"

    decl = tmp_path / "decl.yaml"
    decl.write_text("intervention: {kind: decision-policy-change, to_policy: p1}\n")
    argv = ["fork", "p", "--checkpoint", at, "--declaration", str(decl), "--new-run", "g"]
    assert main([*argv, "--store", agent]) != 0
    assert "onoda pi fork" in capsys.readouterr().err


# --- a live continuation: the prefix replays, then the session hands off to a live model


class PassThroughSandbox:
    """Test-only: a 'sandbox' that wraps nothing, so a live launch runs here without bwrap.

    The live provider below is a local stand-in too; nothing leaves the machine.
    """

    kind = "test-passthrough"

    def stage(self, *, pi_work_dir: Path, workspace: Path, bundle_dir: Path):
        return Path("/bin/bash"), Path(sys.executable)

    def outer_argv(self, *, pi_work_dir: Path, bundle_dir: Path) -> list[str]:
        return []


def live_stand_in(monkeypatch, tmp_path: Path, *turns) -> LiveSettings:
    """Load tests/pi_fixtures/live-provider.ts, serving `turns`, as the fork's live model."""
    served = tmp_path / "live-turns.json"
    served.write_text(turns_script("test_live_stand_in", *turns).model_dump_json(by_alias=True))
    real = pi_run.build_launch

    def build(**kw):
        prepared = real(**kw)
        argv = list(prepared.argv)
        at = len(argv) - 1 - argv[::-1].index("-e")  # the recorder's -e
        argv[at:at] = ["-e", str(FIXTURES / "live-provider.ts")]
        env = {**prepared.env, "ONODA_TEST_LIVE_TURNS": str(served)}
        return dataclasses.replace(prepared, argv=tuple(argv), env=env)

    monkeypatch.setattr(pi_run, "build_launch", build)
    monkeypatch.setenv("ONODA_TEST_KEY", "not-a-real-key")
    return LiveSettings(
        provider="onoda-test-live",
        model="live",
        credential_env="ONODA_TEST_KEY",
        max_requests=3,
        max_cost_usd="1",
    )


def decisions(store: Store, run_id: str):
    out = []
    for e in store.events(run_id):
        if e.type == "invocation.committed":
            r = store.receipt(e.payload["invocation_id"])
            if r.invocation_kind == "decision":
                out.append(r)
    return sorted(out, key=lambda r: r.metadata["request_index"])


def test_a_live_continuation_replays_the_prefix_then_hands_off(tmp_path, monkeypatch):
    store = parent(tmp_path)
    at = checkpoint(store, "p", "job-collected")
    live = live_stand_in(
        monkeypatch,
        tmp_path,
        [tool("k1", "report_states", **wrong_claim())],
        [{"type": "text", "text": "reported something else"}],
    )
    pi = fork(store, tmp_path, at, continuation=None, live=live, sandbox=PassThroughSandbox())
    assert (pi.ingest.status, pi.ingest.complete) == ("settled", True)

    child = store.run_record("f")
    assert child.config["mode"] == "pi-live" and "synthetic" not in child.labels
    assert child.config["fork"]["handoff"] == {"provider": "onoda-test-live", "model": "live"}
    assert child.config["max_requests"] == 3 + 2  # the live model's own cap, on top of the prefix
    decl = store.get_record("fork", child.fork)
    assert decl.intervention["to_policy"] == "onoda-test-live/live"
    assert decl.budget_treatment == "fresh-allowance"

    # The prefix was replayed by the scripted provider at no cost; the live model answered after.
    rs = decisions(store, "f")
    assert [(r.metadata["provider"], r.metadata["source"]) for r in rs] == [
        ("onoda-scripted", "scripted-provider"),
        ("onoda-scripted", "scripted-provider"),
        ("onoda-test-live", "before_provider_request"),
        ("onoda-test-live", "before_provider_request"),
    ]
    assert all("replayed" in r.labels and r.usage.simulated for r in rs[:2])
    assert all(r.metadata["replayed_from"] == "p" for r in rs[:2])
    assert all(r.usage.measured and r.usage.cost_usd == "0.001" for r in rs[2:])
    assert agent_keys(store, "f")[:2] == agent_keys(store, "p")[:2]
    claims = {r: [e for e in store.events(r) if e.type == "pi.claim"] for r in ("p", "f")}
    assert claims["f"][-1].payload_refs["claim"] != claims["p"][-1].payload_refs["claim"]


def test_live_continuations_that_cannot_hand_off_are_refused(tmp_path, monkeypatch):
    store = parent(tmp_path)
    at = checkpoint(store, "p", "job-collected")
    live = live_stand_in(monkeypatch, tmp_path, [{"type": "text", "text": "never"}])
    with pytest.raises(ForkError, match="exactly one"):
        fork(store, tmp_path, at, live=live)  # and the default script continuation
    with pytest.raises(ForkError, match="fresh allowance"):
        fork(store, tmp_path, at, continuation=None, live=live,
             budget_treatment="inherit-remaining")  # fmt: skip
    with pytest.raises(CapabilityError, match="sandbox"):
        fork(store, tmp_path, at, continuation=None, live=live)
    assert not store.has_record("run", "f")


def test_a_live_fork_from_the_start_is_live_from_its_first_request(tmp_path, monkeypatch):
    store = parent(tmp_path)
    live = live_stand_in(monkeypatch, tmp_path, [{"type": "text", "text": "done"}])
    at = checkpoint(store, "p", "initialized")
    fork(store, tmp_path, at, continuation=None, live=live, sandbox=PassThroughSandbox())
    rs = decisions(store, "f")
    assert [r.metadata["provider"] for r in rs] == ["onoda-test-live"]
