"""T04 changed-request rejection and T06 distinct invocations of one specification."""

from __future__ import annotations

import dataclasses

import pytest

from labrun.canonical import digest_of
from labrun.decisions import RecordedDecisionProvider
from labrun.errors import ReplayMismatch
from labrun.fixture import decision_request
from labrun.records import InvocationReceipt


def recorded_request(env, run_id="demo", round=1):
    vis = env.controller.state(run_id).visible
    evidence = {"observation": vis[f"observation.r{round}"], "mapping": vis[f"mapping.r{round}"]}
    return decision_request(evidence, "mock-policy-v1"), evidence


def test_exact_request_replays(fixture_run):
    env, _ = fixture_run
    req, _ = recorded_request(env)
    result = RecordedDecisionProvider(env.store).decide(req, {})
    assert result.action == "refine" and result.replayed_from == "demo.i003"


@pytest.mark.parametrize(
    "change",
    [
        {"question": "Is the scan done?"},
        {"evidence": "observation"},
        {"tool_schema_digest": digest_of({"name": "other"})},
        {"policy": {"name": "mock-policy", "version": "v9"}},
        {"candidates": ("refine", "accept", "escalate")},
    ],
)
def test_changed_request_fails_closed(fixture_run, change):
    env, _ = fixture_run
    req, evidence = recorded_request(env)
    if change.get("evidence"):
        change = {"evidence": {**evidence, "observation": digest_of({"tampered": True})}}
    with pytest.raises(ReplayMismatch):
        RecordedDecisionProvider(env.store).decide(dataclasses.replace(req, **change), {})


def test_recorded_run_replays_offline(fixture_run):
    env, _ = fixture_run
    replay = env.controller.run_fixture("again", decision_provider="recorded")
    assert env.store.get_json(replay.visible["final"])["actions"] == ["refine", "accept"]
    decisions = [
        env.store.get_record("invocation", i)
        for i in replay.frontier
        if env.store.get_record("invocation", i).invocation_kind == "decision"
    ]
    assert [d.metadata["replayed_from"] for d in decisions] == ["demo.i003", "demo.i006"]


def test_changed_parser_cannot_reuse_recording(fixture_run):
    env, _ = fixture_run
    env.controller.start_fixture_run("p2", parser="fixture-parser@2", decision_provider="recorded")
    with pytest.raises(ReplayMismatch):
        env.controller.advance("p2")


def test_same_spec_distinct_invocations(make_env):
    env = make_env()
    env.controller.start_fixture_run("r")
    params = env.controller.state("r").visible["params"]
    a = env.controller.run_job("r", "sample:1", "fixture-sample", {"params": params}, {})
    b = env.controller.run_job("r", "sample:2", "fixture-sample", {"params": params}, {})
    assert a.spec_digest == b.spec_digest
    assert a.invocation_id != b.invocation_id
    assert a.outputs["sample"] != b.outputs["sample"]
    for rec in (a, b):
        stored = env.store.get_record("invocation", rec.invocation_id)
        assert isinstance(stored, InvocationReceipt) and stored.outputs == rec.outputs


def test_replay_names_which_recording(make_env):
    env = make_env()
    env.controller.run_fixture("one")
    env.controller.run_fixture("two")  # identical request, second recording
    req, _ = recorded_request(env, "one")
    provider = RecordedDecisionProvider(env.store)
    with pytest.raises(ReplayMismatch, match="name one"):
        provider.decide(req, {})
    assert provider.decide(req, {}, invocation_id="two.i003").replayed_from == "two.i003"
