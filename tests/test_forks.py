"""T03, T05, T07, T11 (branch half): forks, invalidation, visibility cut, memory provenance."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from labrun.budgets import cost_views, own_invocation_ids, physical_cost, summarize
from labrun.errors import BudgetExceeded, ForkError, IntegrityError, VisibilityError
from labrun.forks import RunView, fork_run
from labrun.memory import freeze_release, retrieve, write_lesson
from labrun.records import Checkpoint, ForkDeclaration, InvocationReceipt, Run

TEMPLATE = Path(__file__).parents[1] / "docs/handoff/examples/fork.template.yaml"
R1 = ["params", "log.r1", "wavefunction.r1", "observation.r1", "mapping.r1"]
V2 = {"kind": "decision-policy-change", "to_policy": "mock-policy-v2"}
V1 = {"kind": "decision-policy-change", "to_policy": "mock-policy-v1"}


def cp_at(env, run_id, boundary, nth=0):
    """Digest of the nth checkpoint at `boundary` (a refine decision's round is already r+1)."""
    return [d for _, d, cp in env.controller.checkpoints(run_id) if cp.boundary == boundary][nth]


def snapshot_files(env) -> dict[str, str]:
    root = env.store.root
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for sub in ("records", "objects", "events")
        for p in (root / sub).rglob("*")
        if p.is_file()
    }


def assert_unchanged(before: dict[str, str], after: dict[str, str], parent: str) -> None:
    assert all(after[path] == digest for path, digest in before.items())
    assert after[f"events/{parent}.jsonl"] == before[f"events/{parent}.jsonl"]


def receipt(env, inv_id) -> InvocationReceipt:
    rec = env.store.get_record("invocation", inv_id)
    assert isinstance(rec, InvocationReceipt)
    return rec


def decision_of(env, state, rnd=1):
    return env.store.get_json(state.visible[f"decision.r{rnd}"])["action"]


def declaration(parent_run_id, checkpoint, new_run_id, **overrides):
    decl = yaml.safe_load(TEMPLATE.read_text())
    decl["new_run_id"] = new_run_id
    decl["parent"]["run_id"] = parent_run_id
    decl["parent"]["checkpoint_digest"] = checkpoint
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        decl[section][key] = value
    return decl


# ------------------------------------------------------------------- T03


def test_t03_policy_branches_share_inherited_evidence(fixture_run):
    env, parent_final = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    before = snapshot_files(env)
    a = fork_run(
        env.controller,
        "demo",
        cp,
        new_run_id="br-v1",
        intervention=V1,
        reason="replicate",
        declaration=declaration("demo", cp, "br-v1", intervention__to_policy="mock-policy-v1"),
    )
    b = fork_run(
        env.controller,
        "demo",
        cp,
        new_run_id="br-v2",
        intervention=V2,
        reason="policy",
        declaration=declaration("demo", cp, "br-v2"),
    )
    assert_unchanged(before, snapshot_files(env), "demo")
    parent_cp = env.store.get_record("checkpoint", cp)
    assert isinstance(parent_cp, Checkpoint)
    for st in (a, b):
        assert {n: st.visible[n] for n in R1} == {n: parent_cp.visible[n] for n in R1}
        assert st.frontier == parent_cp.frontier and st.phase == "decide"
        run = env.controller.run_record(st.run_id)
        assert run.parent is not None and run.parent.run_id == "demo"
        assert run.parent.checkpoint == cp and run.quota_id == "portable-fixture-demo-quota"
        decl = env.store.get_record("fork", run.fork)
        assert isinstance(decl, ForkDeclaration) and decl.invalidated == []
        assert decl.visibility_cut == {"demo": parent_cp.event_seq}
        assert {"forked", "policy-intervention", "synthetic"} <= set(run.labels)
        assert env.store.events(st.run_id)[0].type == "fork.created"

    executions = env.scheduler.executions
    a, b = env.controller.advance("br-v1"), env.controller.advance("br-v2")
    assert_unchanged(before, snapshot_files(env), "demo")
    assert (decision_of(env, a), decision_of(env, b)) == ("refine", "accept")
    assert env.store.get_json(a.visible["final"])["actions"] == ["refine", "accept"]
    assert env.store.get_json(b.visible["final"])["actions"] == ["accept"]
    da, db = receipt(env, a.frontier[2]), receipt(env, b.frontier[2])
    assert da.invocation_kind == db.invocation_kind == "decision"
    assert da.invocation_id != db.invocation_id
    assert da.run_id == "br-v1" and db.run_id == "br-v2"
    assert da.metadata["request_digest"] != db.metadata["request_digest"]
    # only v1's refine suffix submitted a new job; inherited calc r1 was not rerun
    assert env.scheduler.executions == executions + 1
    assert own_invocation_ids(env.store, "br-v2") == [db.invocation_id]
    assert all(i.startswith("br-v1.") for i in own_invocation_ids(env.store, "br-v1"))
    assert parent_final == env.controller.state("demo")


def test_child_resumes_in_fresh_process(fixture_run):
    env, _ = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    fork_run(env.controller, "demo", cp, new_run_id="c", intervention=V2, reason="r")
    fresh = env.reopen()
    st = fresh.controller.resume("c")
    assert st.done and fresh.store.get_json(st.visible["final"])["actions"] == ["accept"]


# ------------------------------------------------------------ validation


def test_unknown_or_non_quiescent_checkpoint_rejected(fixture_run):
    env, _ = fixture_run
    with pytest.raises(ForkError):
        fork_run(
            env.controller,
            "demo",
            "sha256:" + "0" * 64,
            new_run_id="x",
            intervention=V2,
            reason="r",
        )
    other = env.controller.run_fixture("other")
    with pytest.raises(ForkError):  # journaled, but in a different run
        fork_run(
            env.controller, "demo", other.checkpoint, new_run_id="x", intervention=V2, reason="r"
        )

    env.controller.start_fixture_run("busy")
    st = env.controller.state("busy")
    cp = Checkpoint.new(
        run_id="busy",
        boundary="job-collected",
        event_seq=len(env.store.events("busy")),
        frontier=[],
        visible=st.visible,
        phase="parse",
        jobs=[{"job_id": "job-99999", "status": "running"}],
        quiescent=False,
    )
    digest = env.store.put_record(cp)
    env.store.append_event(
        "busy", actor="test", type="checkpoint", payload_refs={"checkpoint": f"checkpoint/{digest}"}
    )
    with pytest.raises(ForkError, match="quiescent"):
        fork_run(env.controller, "busy", digest, new_run_id="x", intervention=V2, reason="r")
    assert not env.store.has_record("run", "x") and env.store.events("x") == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"inheritance__copy_original_future": True},
        {"inheritance__reuse_changed_node_outputs": True},
        {"execution__allow_live_execution": True},
        {"intervention__kind": "parser-change"},
        {"intervention__to_policy": "mock-policy-v1"},
        {"intervention__from_policy": "mock-policy-v9"},
        {"parent__required_boundary": "finalized"},
        {"budgets__allowance": "fresh-allowance"},
        {"budgets__retain_parent_quota": False},
        {"parent__run_id": "someone-else"},
    ],
)
def test_declaration_must_agree_and_forbid_future(fixture_run, overrides):
    env, _ = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    decl = declaration("demo", cp, "x", **overrides)
    with pytest.raises(ForkError):
        fork_run(
            env.controller,
            "demo",
            cp,
            new_run_id="x",
            intervention=V2,
            reason="r",
            declaration=decl,
        )
    assert not env.store.has_record("run", "x")


def test_bad_interventions_rejected(fixture_run):
    env, _ = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    bad = [
        {"kind": "rewrite-history"},
        {**V2, "quota": {"id": "free", "caps": {}}},
        {"kind": "decision-policy-change", "to_policy": "gpt-live"},
        {"kind": "calc-config-change", "level": "coarse"},  # would not change the wavefunction
        {"kind": "memory-change", "to_release": "sha256:" + "1" * 64},
        {"kind": "task-input-change", "params": {"system": "fixture-A", "points": 5, "seed": 7}},
    ]
    for iv in bad:
        with pytest.raises(ForkError):
            fork_run(env.controller, "demo", cp, new_run_id="x", intervention=iv, reason="r")
    fork_run(env.controller, "demo", cp, new_run_id="x", intervention=V2, reason="r")
    with pytest.raises(ForkError):
        fork_run(env.controller, "demo", cp, new_run_id="x", intervention=V2, reason="r")


# ------------------------------------------------------------------- T05


def test_t05_no_inherited_future(make_env):
    env = make_env()
    ctl = env.controller
    ctl.start_fixture_run("p")
    diag = ctl.advance("p", until="diagnostics-committed")
    parse_r1 = diag.frontier[1]
    early = write_lesson(
        env.store, "p", "coarse scan shows warnings", [parse_r1, diag.visible["observation.r1"]]
    )
    ctl.advance("p")
    done = ctl.state("p")
    future_decide = done.frontier[-1]
    future = write_lesson(
        env.store,
        "p",
        "scan converged after refine to fine level",
        [future_decide, done.visible["final"]],
    )
    disguised = write_lesson(
        env.store,
        "p",
        "scan: warnings mean refine",
        [diag.visible["observation.r1"]],
        derived_from=[future],
    )
    hindsight = write_lesson(env.store, "p", "scan hint", [diag.visible["log.r1"]])
    release = freeze_release(env.store, [future, disguised, hindsight, early])

    cp = cp_at(env, "p", "decision-committed-before-submission")
    child = fork_run(
        ctl,
        "p",
        cp,
        new_run_id="c",
        intervention={"kind": "memory-change", "to_release": release.digest},
        reason="memory treatment",
    )
    assert child.phase == "calc" and child.round == 2
    view = RunView(env.store, "c")

    later = [done.visible[n] for n in ("final", "log.r2", "observation.r2", "decision.r2")]
    for digest in [*later, future, disguised, hindsight]:
        with pytest.raises(VisibilityError):
            view.resolve(digest)
        with pytest.raises(VisibilityError):
            view.get_record("artifact", digest)
    with pytest.raises(VisibilityError):
        view.get_record("invocation", future_decide)
    with pytest.raises(VisibilityError):
        view.get_record("checkpoint", done.checkpoint)
    assert view.search("final") == []
    assert view.search("memory-lesson") == [early]
    assert set(view.search("job-output")) == {
        diag.visible["log.r1"],
        diag.visible["wavefunction.r1"],
    }
    assert view.resolve(diag.visible["log.r1"]) == env.store.get_bytes(diag.visible["log.r1"])
    assert view.get_record("invocation", parse_r1).run_id == "p"
    assert view.get_record("fork", env.controller.run_record("c").fork)

    got = retrieve(view, release.digest, "scan refine warnings")
    assert [lesson.digest for lesson in got] == [early]
    ev = env.store.events("c")[-1]
    assert ev.type == "memory.retrieval"
    assert ev.payload["selected"] == [early]
    assert ev.payload["candidates"][0] == disguised  # ranked first, still rejected
    assert set(ev.payload["candidates"]) == {future, disguised, hindsight, early}
    assert ev.payload["rejected"][future].startswith("source-invisible")
    assert ev.payload["rejected"][disguised] == f"derived-from-invisible:{future}"
    assert ev.payload["rejected"][hindsight] == "authored-after-cut"

    # the parent itself may see and retrieve its own future
    parent_view = RunView(env.store, "p")
    assert parent_view.resolve(done.visible["final"])
    assert {lesson.digest for lesson in retrieve(parent_view, release.digest, "scan")} == {
        early,
        future,
        disguised,
        hindsight,
    }
    # a branch cannot author a lesson citing evidence outside its view
    with pytest.raises(VisibilityError):
        write_lesson(env.store, "c", "leak", [early], derived_from=[future])
    with pytest.raises(VisibilityError):
        write_lesson(env.store, "c", "leak", [done.visible["final"]])


def test_t05_advanced_policy_branch_still_blind_to_parent_future(fixture_run):
    env, done = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    fork_run(env.controller, "demo", cp, new_run_id="c", intervention=V2, reason="r")
    st = env.controller.advance("c")
    view = RunView(env.store, "c")
    assert view.resolve(st.visible["final"])
    for name in ("final", "log.r2", "decision.r1", "decision.r2"):
        with pytest.raises(VisibilityError):
            view.resolve(done.visible[name])


# ------------------------------------------------------------------- T07


def test_t07_parser_change_reruns_parse_and_decision(fixture_run):
    env, done = fixture_run
    cp = cp_at(env, "demo", "decision-committed-before-submission")
    parent_cp = env.store.get_record("checkpoint", cp)
    executions = env.scheduler.executions
    st = fork_run(
        env.controller,
        "demo",
        cp,
        new_run_id="p2",
        intervention={"kind": "parser-change", "to_parser": "fixture-parser@2"},
        reason="parser fix",
    )
    calc_r1, parse_r1, decide_r1 = parent_cp.frontier
    decl = env.store.get_record("fork", env.controller.run_record("p2").fork)
    assert decl.invalidated == [parse_r1, decide_r1]
    assert (st.phase, st.round, st.frontier) == ("parse", 1, [calc_r1])
    assert set(st.visible) == {"params", "log.r1", "wavefunction.r1"}

    st = env.controller.advance("p2", until="decision-committed-before-submission")
    assert env.scheduler.executions == executions  # inherited calc reused, not rerun
    assert st.visible["log.r1"] == done.visible["log.r1"]
    assert st.visible["wavefunction.r1"] == done.visible["wavefunction.r1"]
    assert st.visible["observation.r1"] != done.visible["observation.r1"]
    assert "energy_spread" in env.store.get_json(st.visible["observation.r1"])
    new_parse, new_decide = (receipt(env, i) for i in st.frontier[1:])
    assert new_parse.metadata["parser"] == "fixture-parser@2" and new_parse.run_id == "p2"
    assert new_parse.parents == [calc_r1]
    assert (
        new_decide.run_id == "p2"
        and new_decide.inputs["observation"] == st.visible["observation.r1"]
    )


def test_t07_policy_change_after_decision_invalidates_it(fixture_run):
    env, done = fixture_run
    cp = cp_at(env, "demo", "decision-committed-before-submission")
    parent_cp = env.store.get_record("checkpoint", cp)
    st = fork_run(env.controller, "demo", cp, new_run_id="v2", intervention=V2, reason="r")
    assert (st.phase, st.boundary) == ("decide", "diagnostics-committed")
    assert "decision.r1" not in st.visible and st.frontier == parent_cp.frontier[:2]
    st = env.controller.advance("v2")
    assert decision_of(env, st) == "accept"
    assert st.visible["decision.r1"] != done.visible["decision.r1"]


def test_policy_change_at_finalized_checkpoint_drops_whole_suffix(fixture_run):
    env, done = fixture_run
    st = fork_run(
        env.controller, "demo", done.checkpoint, new_run_id="v2", intervention=V2, reason="r"
    )
    assert (st.phase, st.round) == ("decide", 1)
    assert set(st.visible) == set(R1) and len(st.frontier) == 2
    decl = env.store.get_record("fork", env.controller.run_record("v2").fork)
    assert len(decl.invalidated) == 4  # decide r1, then calc/parse/decide r2 it chose
    assert env.controller.advance("v2").done


def test_t07_task_input_change_invalidates_everything(fixture_run):
    env, done = fixture_run
    executions = env.scheduler.executions
    params = {"system": "fixture-A", "points": 5, "seed": 8}
    st = fork_run(
        env.controller,
        "demo",
        done.checkpoint,
        new_run_id="t2",
        intervention={"kind": "task-input-change", "params": params},
        reason="new input",
    )
    run = env.controller.run_record("t2")
    assert run.task != env.controller.run_record("demo").task
    assert (st.phase, st.round, st.frontier, st.boundary) == ("calc", 1, [], "initialized")
    assert env.store.get_json(st.visible["params"]) == params
    decl = env.store.get_record("fork", run.fork)
    assert decl.invalidated == done.frontier
    st = env.controller.advance("t2")
    assert st.done and env.scheduler.executions > executions
    assert st.visible["log.r1"] != done.visible["log.r1"]
    assert cost_views(env.store, "t2")["inherited_prefix"]["invocations"] == 0


def test_t07_wavefunction_change_invalidates_mapping(fixture_run):
    env, done = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    parent_cp = env.store.get_record("checkpoint", cp)
    st = fork_run(
        env.controller,
        "demo",
        cp,
        new_run_id="wf",
        intervention={"kind": "calc-config-change", "round": 1, "level": "fine"},
        reason="finer calc",
    )
    decl = env.store.get_record("fork", env.controller.run_record("wf").fork)
    assert decl.invalidated == parent_cp.frontier
    assert st.phase == "calc" and "wavefunction.r1" not in st.visible
    assert "mapping.r1" not in st.visible and "observation.r1" not in st.visible
    # the override lives in the run config, so a crash here resumes at the fine level
    st = env.reopen().controller.advance("wf", until="job-collected")
    assert st.visible["wavefunction.r1"] != done.visible["wavefunction.r1"]

    # pairing the old mapping with the new wavefunction is refused at decide
    run = env.controller.run_record("wf")
    bad = {
        **st.visible,
        "observation.r1": done.visible["observation.r1"],
        "mapping.r1": done.visible["mapping.r1"],
    }
    env.controller.write_checkpoint(
        run, "diagnostics-committed", "decide", 1, bad, st.frontier, st.ancestry_cut
    )
    with pytest.raises(IntegrityError):
        env.controller.advance("wf")

    ok = fork_run(
        env.controller,
        "demo",
        cp,
        new_run_id="wf2",
        intervention={"kind": "calc-config-change", "round": 1, "level": "fine"},
        reason="finer calc",
    )
    ok = env.controller.advance("wf2")
    mapping = env.store.get_json(ok.visible["mapping.r1"])
    assert mapping["wavefunction"] == ok.visible["wavefunction.r1"]
    assert decision_of(env, ok) == "accept"  # fine level has no warnings


# ------------------------------------------------------------------- T11


def test_t11_branch_cost_views(fixture_run):
    env, done = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    fork_run(env.controller, "demo", cp, new_run_id="a", intervention=V1, reason="r")
    fork_run(env.controller, "demo", cp, new_run_id="b", intervention=V2, reason="r")
    env.controller.advance("a")
    env.controller.advance("b")
    views = {r: cost_views(env.store, r) for r in ("demo", "a", "b")}
    prefix = summarize(receipt(env, i) for i in done.frontier[:2])
    for r in ("a", "b"):
        assert views[r]["inherited_prefix"] == prefix
        assert views[r]["full_path"] == summarize(
            receipt(env, i) for i in done.frontier[:2] + own_invocation_ids(env.store, r)
        )
        assert views[r]["marginal"]["invocations"] == len(own_invocation_ids(env.store, r))
    assert views["a"]["marginal"]["cost_usd"] == "0.40"  # fine calc only
    assert views["b"]["marginal"]["cost_usd"] == "0.00"
    assert views["a"]["full_path"]["cost_usd"] == views["demo"]["full_path"]["cost_usd"] == "0.80"

    unique = {i for r in ("demo", "a", "b") for i in own_invocation_ids(env.store, r)}
    physical = physical_cost(env.store, ["demo", "a", "b"])
    assert physical == summarize(receipt(env, i) for i in sorted(unique))
    assert physical["invocations"] == 6 + 4 + 1
    assert physical["cost_usd"] == "1.20"
    naive = sum(views[r]["full_path"]["invocations"] for r in views)
    assert naive == physical["invocations"] + 2 * prefix["invocations"]


def test_t11_inherit_remaining_respects_run_cap(make_env):
    env = make_env()
    env.controller.start_fixture_run("p", limits={"fixture_steps": 4})
    diag = env.controller.advance("p", until="diagnostics-committed")
    fork_run(env.controller, "p", diag.checkpoint, new_run_id="inh", intervention=V1, reason="r")
    with pytest.raises(BudgetExceeded):
        env.controller.advance("inh")  # 2 inherited + decide + calc r2 fits; parse r2 does not
    fresh = fork_run(
        env.controller,
        "p",
        diag.checkpoint,
        new_run_id="fresh",
        intervention=V1,
        reason="r",
        budget_treatment="fresh-allowance",
    )
    assert env.controller.run_record("fresh").config["budget_treatment"] == "fresh-allowance"
    assert fresh.config["limits"] == {"fixture_steps": 4}
    assert env.controller.advance("fresh").done


def test_t11_fork_cannot_bypass_quota(make_env):
    env = make_env()
    quota = {"id": "q", "caps": {"fixture_steps": 5}}
    env.controller.start_fixture_run("p", quota=quota)
    diag = env.controller.advance("p", until="diagnostics-committed")
    fork_run(
        env.controller,
        "p",
        diag.checkpoint,
        new_run_id="f",
        intervention=V1,
        reason="r",
        budget_treatment="fresh-allowance",
    )
    run = env.controller.run_record("f")
    assert isinstance(run, Run) and run.quota_id == "q" and run.config["quota"] == quota
    with pytest.raises(BudgetExceeded, match="quota q"):
        env.controller.advance("f")
    with pytest.raises(ForkError):
        fork_run(
            env.controller,
            "p",
            diag.checkpoint,
            new_run_id="g",
            intervention={**V1, "quota": {"id": "free", "caps": {}}},
            reason="r",
        )


def test_t11_unknown_branch_cost_is_not_zero(fixture_run):
    env, _ = fixture_run
    cp = cp_at(env, "demo", "diagnostics-committed")
    st = fork_run(env.controller, "demo", cp, new_run_id="s", intervention=V2, reason="r")
    env.controller.run_job("s", "sample:1", "fixture-sample", {"params": st.visible["params"]}, {})
    views = cost_views(env.store, "s")
    assert views["marginal"]["cost_usd"] is None
    assert views["full_path"]["cost_usd"] is None
    assert views["full_path"]["known_cost_usd"] == "0.40"
    assert views["inherited_prefix"]["cost_usd"] == "0.40"
    assert physical_cost(env.store, ["demo", "s"])["cost_usd"] is None
