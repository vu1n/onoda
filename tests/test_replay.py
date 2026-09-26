"""Offline replay and deterministic reanalysis (T02, replay half of T01, T09-ish)."""

from __future__ import annotations

import json
import shutil
import socket

import pytest

from onoda.clock import LogicalClock
from onoda.controller import normalized_final_state
from onoda.errors import CapabilityError, IntegrityError, MissingObjectError
from onoda.replay import reanalyze, replay_run, replay_state_at
from onoda.store import Store

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


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """The whole module must work with zero external calls."""

    def deny(*args, **kwargs):
        raise AssertionError("network access attempted during offline replay/reanalysis")

    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    yield


def _journal_bytes(env, run_id: str) -> bytes:
    return (env.store.root / "events" / f"{run_id}.jsonl").read_bytes()


def _object_path(store, digest: str):
    hexpart = digest.split(":", 1)[1]
    return store.root / "objects" / "sha256" / hexpart[:2] / hexpart[2:]


def _checkpoint_path(store, digest: str):
    hexpart = digest.split(":", 1)[1]
    return store.root / "records" / "checkpoint" / f"{hexpart}.json"


# --------------------------------------------------------------- replay: T01


def test_replay_completed_run_matches_final_state(fixture_run):
    env, state = fixture_run
    report = replay_run(env.store, "demo")

    assert report["operation"] == "inspect-replay"
    assert report["run_id"] == "demo"
    assert report["external_calls"] == 0
    assert report["demonstrates"] == "recorded history reconstruction; not recomputation"
    assert [c["boundary"] for c in report["checkpoints"]] == FULL_V1
    assert len(report["checkpoints"]) == 8

    committed = [
        e.payload["invocation_id"]
        for e in env.store.events("demo")
        if e.type == "invocation.committed"
    ]
    assert report["invocations"] == committed

    assert report["final"] == normalized_final_state(env.store, state)
    assert report["final_digest"] == state.visible["final"]


def test_replay_survives_dropped_index_and_relocated_store(fixture_run, tmp_path):
    env, state = fixture_run
    env.store.drop_index()
    assert not env.store.index_path.exists()

    report_before = replay_run(env.store, "demo")

    relocated_root = tmp_path / "relocated-elsewhere"
    shutil.copytree(env.store.root, relocated_root)
    relocated = Store(relocated_root, clock=LogicalClock())

    report_after = replay_run(relocated, "demo")
    assert report_after == report_before
    assert report_after["final"] == normalized_final_state(env.store, state)


def test_replay_detects_tampered_checkpoint_record(fixture_run):
    env, _ = fixture_run
    _, digest, cp = env.controller.checkpoints("demo")[2]
    path = _checkpoint_path(env.store, digest)
    data = json.loads(path.read_text())
    data["phase"] = "tampered-phase"
    path.write_text(json.dumps(data))

    with pytest.raises(IntegrityError):
        replay_run(env.store, "demo")


def test_replay_detects_missing_visible_object(fixture_run):
    env, _ = fixture_run
    _, _, cp = env.controller.checkpoints("demo")[2]
    digest = cp.visible["observation.r1"]
    _object_path(env.store, digest).unlink()

    with pytest.raises(MissingObjectError):
        replay_run(env.store, "demo")


def test_replay_state_at_diagnostics_checkpoint_returns_observation(fixture_run):
    env, _ = fixture_run
    _, digest, cp = next(
        c for c in env.controller.checkpoints("demo") if c[2].boundary == "diagnostics-committed"
    )
    state = replay_state_at(env.store, "demo", digest)
    assert state["observation.r1"]["synthetic"] is True
    assert "energies" in state["observation.r1"]
    assert state["mapping.r1"]["state_map"]


# ------------------------------------------------------------------- T02


def _parse_receipts(env, run_id: str):
    return [
        env.store.get_record("invocation", e.payload["invocation_id"])
        for e in env.store.events(run_id)
        if e.type == "invocation.committed"
        and "parser" in env.store.get_record("invocation", e.payload["invocation_id"]).metadata
    ]


def test_reanalyze_with_original_parser_matches_and_preserves_history(fixture_run):
    env, _ = fixture_run
    originals = _parse_receipts(env, "demo")
    original_snapshot = {r.invocation_id: r.to_json() for r in originals}
    journal_before = _journal_bytes(env, "demo")

    results = reanalyze(env.store, "demo")

    assert len(results) == 2
    seen_originals = set()
    for r in results:
        assert r["matches"] is True
        assert r["differences"] == []
        assert r["original_outputs"] == r["new_outputs"]
        new_rec = env.store.get_record("invocation", r["derivation"])
        assert new_rec.derived_from == r["original"]
        assert new_rec.invocation_kind == "derivation"
        assert "reanalysis" in new_rec.labels
        assert "fixture-reanalysis-not-scientific-replication" in new_rec.labels
        seen_originals.add(r["original"])
    assert seen_originals == set(original_snapshot)

    # originals untouched
    for inv_id, snapshot in original_snapshot.items():
        assert env.store.get_record("invocation", inv_id).to_json() == snapshot

    # journal is append-only: the old prefix is byte-identical
    journal_after = _journal_bytes(env, "demo")
    assert journal_after[: len(journal_before)] == journal_before
    assert len(journal_after) > len(journal_before)


def test_reanalyze_with_different_parser_flags_the_difference(fixture_run):
    env, _ = fixture_run
    originals = _parse_receipts(env, "demo")
    outputs_before = {r.invocation_id: dict(r.outputs) for r in originals}

    results = reanalyze(env.store, "demo", transform="fixture-parser@2")

    assert len(results) == 2
    for r in results:
        assert r["transform"] == "fixture-parser@2"
        assert r["matches"] is False
        assert r["differences"] == ["observation"]
        assert r["original_outputs"]["mapping"] == r["new_outputs"]["mapping"]
        assert r["original_outputs"]["observation"] != r["new_outputs"]["observation"]

    for inv_id, outputs in outputs_before.items():
        assert env.store.get_record("invocation", inv_id).outputs == outputs


def test_reanalyze_calc_step_is_a_different_capability(fixture_run):
    env, _ = fixture_run
    with pytest.raises(CapabilityError, match="(?i)reexecution"):
        reanalyze(env.store, "demo", step="calc")


def test_reanalyze_unknown_transform_rejected(fixture_run):
    env, _ = fixture_run
    with pytest.raises(CapabilityError):
        reanalyze(env.store, "demo", transform="fixture-parser@999")
