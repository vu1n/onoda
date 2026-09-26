"""Fixture workflow, checkpoint boundaries, idempotent advance, resume honesty (T14)."""

from __future__ import annotations

import pytest

from onoda.controller import normalized_final_state
from onoda.errors import CapabilityError

FULL_V1 = [
    "initialized",
    "job-collected",
    "diagnostics-committed",
    "decision-committed-before-submission",
    "job-collected",
    "diagnostics-committed",
    "decision-committed-before-submission",
    "finalized",
]


def boundaries(env, run_id):
    return [cp.boundary for _, _, cp in env.controller.checkpoints(run_id)]


def test_v1_refines_then_accepts(fixture_run):
    env, state = fixture_run
    assert state.done
    assert boundaries(env, "demo") == FULL_V1
    final = env.store.get_json(state.visible["final"])
    assert final["actions"] == ["refine", "accept"]
    assert final["synthetic"] is True
    assert env.scheduler.executions == 2


def test_v2_accepts_immediately(make_env):
    env = make_env()
    state = env.controller.run_fixture("v2", policy="mock-policy-v2")
    assert env.store.get_json(state.visible["final"])["actions"] == ["accept"]
    assert env.scheduler.executions == 1


def test_advance_stops_at_boundary_and_is_idempotent(make_env):
    env = make_env()
    env.controller.start_fixture_run("r")
    st = env.controller.advance("r", until="diagnostics-committed")
    assert st.boundary == "diagnostics-committed" and st.phase == "decide"
    done = env.controller.advance("r")
    n_events = len(env.store.events("r"))
    assert env.controller.advance("r") == done
    assert len(env.store.events("r")) == n_events


def test_runs_are_deterministic_across_stores(make_env):
    a, b = make_env("a"), make_env("b")
    sa = a.controller.run_fixture("demo")
    sb = b.controller.run_fixture("demo")
    assert normalized_final_state(a.store, sa) == normalized_final_state(b.store, sb)
    assert a.store.events("demo") == b.store.events("demo")


def test_checkpoints_declare_semantic_restart_only(fixture_run):
    env, _ = fixture_run
    for _, _, cp in env.controller.checkpoints("demo"):
        assert cp.capabilities["native_resume"] == "unsupported"
        assert cp.capabilities["semantic_restart"] == "supported"
        assert cp.quiescent
    with pytest.raises(CapabilityError):
        env.controller.resume("demo", mode="native")


def test_semantic_restart_in_fresh_process(make_env):
    env = make_env()
    env.controller.start_fixture_run("r")
    env.controller.advance("r", until="job-collected")
    fresh = env.reopen()
    st = fresh.controller.resume("r")
    assert st.done and boundaries(fresh, "r") == FULL_V1
    assert fresh.scheduler.executions == 2


def test_live_execution_refused(make_env):
    env = make_env()
    with pytest.raises(CapabilityError):
        env.controller.start_fixture_run("live", allow_live_execution=True)


def test_checkpoint_never_references_uncommitted_refs(fixture_run):
    env, _ = fixture_run
    for _, _, cp in env.controller.checkpoints("demo"):
        assert all(env.store.has_object(d) for d in cp.visible.values())
        assert all(env.store.has_record("invocation", i) for i in cp.frontier)
