"""SoL-Pi context mechanism fixtures (SOL_PI_DELTA M1)."""

from __future__ import annotations

import pytest

from labrun.clock import LogicalClock
from labrun.context import (
    FUSABLE_OPERATIONS,
    ObservationPack,
    activation_counts,
    compaction_history,
    decompose_fused_call,
    record_compaction,
    reduce_log,
    required_diagnostics_present,
)
from labrun.errors import CapabilityError, MissingObjectError
from labrun.store import Store

BIG = b"x" * 20000  # over the default 10 KiB threshold
SMALL = b"tiny output"


# --------------------------------------------------------------- ObservationPack


def test_small_output_always_renders_full(tmp_path):
    store = Store(tmp_path)
    pack = ObservationPack(store, "r1")
    handle = pack.observe("stdout", SMALL, "text/plain")
    for request_index in (0, 1, 5, 100):
        entries = pack.render(request_index)
        assert entries == [
            {
                "name": "stdout",
                "handle": handle,
                "size": len(SMALL),
                "arrival_request": 0,
                "age": request_index,
                "full": True,
                "data": SMALL,
            }
        ]


def test_large_output_ages_into_excerpt(tmp_path):
    store = Store(tmp_path)
    pack = ObservationPack(store, "r1", threshold_bytes=10240, full_requests=2)
    handle = pack.observe("orca_log", BIG, "text/plain")

    full0 = pack.render(0)[0]
    assert full0["full"] is True
    assert full0["data"] == BIG

    full1 = pack.render(1)[0]
    assert full1["full"] is True

    excerpt = pack.render(2)[0]
    assert excerpt["full"] is False
    assert excerpt["handle"] == handle
    assert excerpt["size"] == len(BIG)
    assert excerpt["head"] == BIG[:256]
    assert excerpt["tail"] == BIG[-256:]
    assert "data" not in excerpt

    # aging never regresses: later requests stay excerpted
    assert pack.render(3)[0]["full"] is False


def test_page_reads_exact_slice_and_records_event(tmp_path):
    store = Store(tmp_path)
    pack = ObservationPack(store, "r1")
    handle = pack.observe("log", BIG, "text/plain")

    chunk = pack.page(handle, 100, 140)
    assert chunk == BIG[100:140]

    events = [e for e in store.events("r1") if e.type == "context.page"]
    assert len(events) == 1
    assert events[0].payload["handle"] == handle
    assert events[0].payload["start"] == 100
    assert events[0].payload["end"] == 140


def test_state_is_json_serializable_with_no_floats(tmp_path):
    import json

    store = Store(tmp_path)
    pack = ObservationPack(store, "r1", threshold_bytes=5, full_requests=2)
    pack.observe("a", BIG, "text/plain")
    pack.render(0)
    state = pack.state()
    # canonical_bytes rejects floats; round-tripping through json is enough
    # to prove there is nothing but ints/strings/bools/dicts/lists.
    assert json.loads(json.dumps(state)) == state
    assert state["threshold_bytes"] == 5
    assert state["full_requests"] == 2
    assert state["observations"]["a"]["archived"] is True


def test_from_state_resolves_handles_in_a_different_store(tmp_path):
    store_a = Store(tmp_path / "a")
    pack = ObservationPack(store_a, "r1")
    handle = pack.observe("orca_log", BIG, "text/plain")
    pack.render(0)
    pack.render(1)
    state = pack.state()

    # Export/import: copy the object (and nothing else) into a second store
    # at another path, then restore purely from the recorded state.
    store_b = Store(tmp_path / "b")
    store_b.put_bytes(store_a.get_bytes(handle), media_type="text/plain")

    restored = ObservationPack.from_state(store_b, "r1", state)
    assert restored.threshold_bytes == pack.threshold_bytes
    assert restored.full_requests == pack.full_requests
    assert restored.page(handle, 0, 10) == BIG[:10]
    assert restored.render(2)[0]["full"] is False  # aging state carried over

    with pytest.raises(MissingObjectError):
        store_a.get_bytes("sha256:" + "00" * 32)  # sanity: store_a untouched


# ------------------------------------------------------------------- compaction


def test_compaction_records_trigger_and_replays_in_order(tmp_path):
    store = Store(tmp_path)
    e1 = record_compaction(
        store,
        "r1",
        trigger_inputs={"remaining_requests": 3, "growth_rate_bp": 120},
        summary=b"summary one",
        replaced_refs=["obs/a", "obs/b"],
        boundary="diagnostics-committed",
    )
    e2 = record_compaction(
        store,
        "r1",
        trigger_inputs={"remaining_requests": 1, "growth_rate_bp": 340},
        summary=b"summary two",
        replaced_refs=["obs/c"],
        boundary=None,
    )
    assert e1.seq < e2.seq

    history = compaction_history(store, "r1")
    assert [h["seq"] for h in history] == [e1.seq, e2.seq]
    assert history[0]["trigger_inputs"] == {"remaining_requests": 3, "growth_rate_bp": 120}
    assert history[0]["replaced_refs"] == ["obs/a", "obs/b"]
    assert history[1]["boundary"] is None

    summary_desc = store.descriptor(history[0]["summary_handle"])
    assert "derived-interpretation" in summary_desc.labels
    assert store.get_bytes(history[0]["summary_handle"]) == b"summary one"


# --------------------------------------------------------------------- reducer


def _good_reducer(log: bytes) -> dict:
    from labrun.canonical import sha256_digest

    return {
        "source_digest": sha256_digest(log),
        "exit_status": 0,
        "quotes": ["FINAL SINGLE POINT ENERGY -76.0267"],
        "summary": "converged",
    }


def test_reducer_success_writes_labelled_receipt_and_invocation(tmp_path):
    store = Store(tmp_path)
    log = b"step 1 ok\n" * 200 + b"FINAL SINGLE POINT ENERGY -76.0267\n" + b"normal termination\n"
    clock = LogicalClock()

    result = reduce_log(
        store, "r1", log, _good_reducer, exit_status=0, invocation_id="inv1", clock=clock
    )

    assert result.success is True
    desc = store.descriptor(result.digest)
    assert desc.labels == ["derived-interpretation"]
    assert "parser-output" not in desc.labels

    inv = result.invocation
    assert inv is not None
    assert inv.invocation_kind == "derivation"
    assert inv.labels == ["reducer-receipt"]
    assert inv.metadata["reducer_id"] == "_good_reducer"
    assert inv.usage.simulated is True
    assert inv.usage.units["reducer_tokens"] > 0
    assert store.get_record("invocation", "inv1") == inv

    assert activation_counts(store, "r1") == {}  # success path logs no fallback


def test_reducer_falls_back_on_bad_digest(tmp_path):
    store = Store(tmp_path)
    log = b"some build output\n"

    def lying_reducer(_log: bytes) -> dict:
        return {
            "source_digest": "sha256:" + "00" * 32,
            "exit_status": 0,
            "quotes": [],
            "summary": "fine",
        }

    result = reduce_log(
        store, "r1", log, lying_reducer, exit_status=0, invocation_id="inv2", clock=LogicalClock()
    )
    assert result.success is False
    assert result.receipt is None
    assert store.get_bytes(result.digest) == log
    assert "does not match the log" in result.reason

    fallback = [e for e in store.events("r1") if e.type == "context.reducer-fallback"]
    assert len(fallback) == 1
    assert fallback[0].payload["invocation_id"] == "inv2"
    assert activation_counts(store, "r1") == {"context.reducer-fallback": 1}


def test_reducer_falls_back_on_unquoted_text(tmp_path):
    store = Store(tmp_path)
    log = b"line one\nline two\n"

    def bogus_quote_reducer(log_bytes: bytes) -> dict:
        from labrun.canonical import sha256_digest

        return {
            "source_digest": sha256_digest(log_bytes),
            "exit_status": 0,
            "quotes": ["this text is not in the log"],
            "summary": "ok",
        }

    result = reduce_log(
        store,
        "r1",
        log,
        bogus_quote_reducer,
        exit_status=0,
        invocation_id="inv3",
        clock=LogicalClock(),
    )
    assert result.success is False
    assert "quote not found verbatim" in result.reason


def test_reducer_verification_proves_fidelity_not_completeness(tmp_path):
    """A reducer that drops a warning line still passes: quotes are verbatim,
    digest and exit status match, and the receipt is smaller than the log.
    Completeness is a separate, explicit check."""
    store = Store(tmp_path)
    log = (
        b"step 1 ok\n" * 200
        + b"WARNING: SCF did not fully converge, root flip suspected\n"
        + b"FINAL SINGLE POINT ENERGY -76.0267\n"
        + b"normal termination\n"
    )

    def drops_warning_reducer(log_bytes: bytes) -> dict:
        from labrun.canonical import sha256_digest

        return {
            "source_digest": sha256_digest(log_bytes),
            "exit_status": 0,
            "quotes": ["FINAL SINGLE POINT ENERGY -76.0267"],
            "summary": "converged cleanly",
        }

    result = reduce_log(
        store,
        "r1",
        log,
        drops_warning_reducer,
        exit_status=0,
        invocation_id="inv4",
        clock=LogicalClock(),
    )
    assert result.success is True
    desc = store.descriptor(result.digest)
    assert desc.labels == ["derived-interpretation"]  # verification passed

    report = required_diagnostics_present(
        result.receipt, required=["SCF did not fully converge, root flip suspected"]
    )
    assert report["missing"] == ["SCF did not fully converge, root flip suspected"]
    assert report["present"] == []


# ---------------------------------------------------------------------- fusion


def test_fusion_allows_idempotent_local_checks_and_shares_fusion_id(tmp_path):
    store = Store(tmp_path)
    steps = [
        {"operation": "write_input", "path": "input.inp"},
        {"operation": "validate_input", "path": "input.inp"},
        {"operation": "read_file", "path": "input.inp"},
    ]
    events = decompose_fused_call(store, "r1", steps)
    assert [e.type for e in events] == ["context.fused-step"] * 3
    fusion_ids = {e.payload["fusion_id"] for e in events}
    assert len(fusion_ids) == 1
    assert [e.payload["step_index"] for e in events] == [0, 1, 2]
    assert [e.payload["operation"] for e in events] == [op["operation"] for op in steps]
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    assert events[1].causes == [events[0].event_id]

    assert FUSABLE_OPERATIONS == {"write_input", "validate_input", "read_file"}
    assert activation_counts(store, "r1") == {"context.fused-step": 3}


@pytest.mark.parametrize(
    "steps",
    [
        [{"operation": "write_input"}, {"operation": "submit"}],
        [{"operation": "cancel"}],
        [{"operation": "validate_input", "budget_impact": True}],
        [{"operation": "reserve_solver_slot"}],
    ],
)
def test_fusion_refuses_submit_cancel_and_budget_impact(tmp_path, steps):
    store = Store(tmp_path)
    with pytest.raises(CapabilityError):
        decompose_fused_call(store, "r1", steps)
    # refused before recording anything
    assert store.events("r1") == []


# ----------------------------------------------------------------- activation


def test_activation_counts_across_all_mechanisms(tmp_path):
    store = Store(tmp_path)
    pack = ObservationPack(store, "r1")
    handle = pack.observe("log", BIG, "text/plain")
    pack.page(handle, 0, 10)
    record_compaction(
        store, "r1", trigger_inputs={"n": 1}, summary=b"s", replaced_refs=[], boundary=None
    )
    decompose_fused_call(store, "r1", [{"operation": "write_input"}])

    def bad_reducer(_log: bytes) -> dict:
        return {
            "source_digest": "sha256:" + "00" * 32,
            "exit_status": 1,
            "quotes": [],
            "summary": "",
        }

    reduce_log(
        store, "r1", b"log", bad_reducer, exit_status=1, invocation_id="i1", clock=LogicalClock()
    )

    counts = activation_counts(store, "r1")
    assert counts == {
        "context.observe": 1,
        "context.page": 1,
        "context.compaction": 1,
        "context.fused-step": 1,
        "context.reducer-fallback": 1,
    }
