"""T10: crash-safe submission. Resume reconciles and never duplicates a side effect."""

from __future__ import annotations

import pytest

from labrun.errors import SubmissionUncertain


class Crash(Exception):
    pass


def crash_at(point):
    def hook(p):
        if p == point:
            raise Crash(p)

    return hook


def types(env, run_id):
    return [e.type for e in env.store.events(run_id)]


def test_crash_after_intent_before_remote_accept(make_env, monkeypatch):
    env = make_env()
    env.controller.start_fixture_run("r")

    def dies(*a, **k):
        raise Crash("process died before the request left")

    monkeypatch.setattr(env.scheduler, "submit", dies)
    with pytest.raises(Crash):
        env.controller.advance("r")
    assert types(env, "r")[-1] == "job.intent"

    fresh = env.reopen()
    assert fresh.controller.resume("r").done
    assert fresh.scheduler.executions == 2
    first = next(e for e in fresh.store.events("r") if e.type == "job.submitted")
    assert first.payload["reconciled"] is False  # lookup said never accepted; submitted once


def test_crash_after_remote_accept_before_ack(make_env):
    env = make_env()
    env.scheduler.crash_hook = crash_at("remote.accepted")
    env.controller.start_fixture_run("r")
    with pytest.raises(Crash):
        env.controller.advance("r")
    assert env.scheduler.executions == 1
    assert "job.submitted" not in types(env, "r")

    fresh = env.reopen()
    assert fresh.controller.resume("r").done
    assert fresh.scheduler.executions == 2  # coarse + fine, no duplicate of coarse
    first = next(e for e in fresh.store.events("r") if e.type == "job.submitted")
    assert first.payload["reconciled"] is True


def test_crash_after_output_commit_before_receipt(make_env):
    env = make_env(crash_hook=crash_at("job.outputs-committed"))
    env.controller.start_fixture_run("r")
    with pytest.raises(Crash):
        env.controller.advance("r")
    assert "invocation.committed" not in types(env, "r")

    fresh = env.reopen()
    assert fresh.controller.resume("r").done
    assert fresh.scheduler.executions == 2
    committed = [e for e in fresh.store.events("r") if e.type == "invocation.committed"]
    assert len({e.payload["invocation_id"] for e in committed}) == len(committed)


def test_uncertain_submission_stays_explicit(make_env):
    env = make_env(lookup_available=False)
    env.scheduler.crash_hook = crash_at("remote.accepted")
    env.controller.start_fixture_run("r")
    with pytest.raises(Crash):
        env.controller.advance("r")

    blind = env.reopen(lookup_available=False)
    with pytest.raises(SubmissionUncertain):
        blind.controller.resume("r")
    assert types(blind, "r")[-1] == "job.submission-uncertain"
    assert blind.scheduler.executions == 1

    reconciled = env.reopen()
    assert reconciled.controller.resume("r").done
    assert reconciled.scheduler.executions == 2


def test_scheduler_submission_is_idempotent_by_key(make_env):
    env = make_env()
    params = env.store.put_json({"system": "x", "points": 2, "seed": 1})
    a = env.scheduler.submit(
        "k1", "fixture-calc", {"params": env.store.get_bytes(params.digest)}, {}
    )
    b = env.scheduler.submit(
        "k1", "fixture-calc", {"params": env.store.get_bytes(params.digest)}, {}
    )
    assert a == b and env.scheduler.executions == 1
