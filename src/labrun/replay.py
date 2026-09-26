"""Offline replay and deterministic reanalysis (PORTABLE_RUN_SPEC §8).

`replay_run` is pure inspection: it walks the journal and committed records
that are already on disk and never constructs a `Controller`, a scheduler, or
a decision provider. It must succeed with no network, no provider credentials,
and no SQLite index (the index is disposable; see `Store.drop_index`).

`reanalyze` is the one exception to "never touch history": it *adds* to a
run, it never edits it. It reruns a named pure transform (a fixture parser)
against the exact recorded input bytes, stores new output artifacts, writes a
brand-new `InvocationReceipt` linked to the original via `derived_from`, and
appends a `"reanalysis.committed"` event. The original receipt, its outputs,
and every earlier journal line are untouched byte-for-byte.

Reexecution (rerunning an external step such as `calc`) is a different,
unimplemented capability; asking to reanalyze one fails closed.
"""

from __future__ import annotations

from typing import Any

from labrun.canonical import digest_of
from labrun.errors import CapabilityError, IntegrityError, LabrunError
from labrun.fixture import PARSERS
from labrun.records import Checkpoint, InvocationReceipt, StepSpec, Usage
from labrun.store import Store, record_ref


def _checkpoints(store: Store, run_id: str) -> list[tuple[int, str, Checkpoint]]:
    """Checkpoint events in commit order, with their (chain-verified) records."""
    out = store.checkpoints(run_id)
    for _, digest, cp in out:
        if cp.run_id != run_id:
            raise IntegrityError(f"checkpoint {digest} belongs to {cp.run_id!r}, not {run_id!r}")
    return out


def _verify_receipt(store: Store, run_id: str, inv_id: str) -> InvocationReceipt:
    receipt = store.receipt(inv_id)
    if receipt.run_id != run_id:
        raise IntegrityError(f"invocation {inv_id} belongs to {receipt.run_id!r}, not {run_id!r}")
    store.get_record("step", receipt.spec_digest)
    for digest in (*receipt.inputs.values(), *receipt.outputs.values()):
        store.get_bytes(digest)  # verifies checksum; raises MissingObjectError/IntegrityError
    return receipt


def _verify_checkpoint(store: Store, run_id: str, cp: Checkpoint) -> None:
    for digest in cp.visible.values():
        store.get_bytes(digest)
    for inv_id in cp.frontier:
        _verify_receipt(store, run_id, inv_id)


def _normalized_final_state(store: Store, final_digest: str) -> dict[str, Any]:
    final = store.get_json(final_digest)
    return {"final": final, "digest": digest_of(final)}


def replay_run(store: Store, run_id: str) -> dict[str, Any]:
    """Reconstruct a run's history and visible state from recorded records only.

    Verifies the journal's hash chain (`Store.events` does this), that every
    checkpoint's visible artifacts and frontier receipts exist and check out,
    that visible state and the frontier only grow across checkpoints, and
    returns a report of what was reconstructed. No network, job scheduler, or
    decision provider is touched: `external_calls` is always 0.
    """
    events = store.events(run_id)
    checkpoints = _checkpoints(store, run_id)

    prev_visible: dict[str, str] | None = None
    prev_frontier: list[str] | None = None
    for _, digest, cp in checkpoints:
        _verify_checkpoint(store, run_id, cp)
        if prev_visible is not None:
            if not set(prev_visible).issubset(cp.visible):
                raise IntegrityError(f"checkpoint {digest} dropped previously visible keys")
            for k, v in prev_visible.items():
                if cp.visible[k] != v:
                    raise IntegrityError(f"checkpoint {digest} changed visible[{k!r}]")
        if prev_frontier is not None and cp.frontier[: len(prev_frontier)] != prev_frontier:
            raise IntegrityError(f"checkpoint {digest} frontier is not monotone")
        prev_visible, prev_frontier = dict(cp.visible), list(cp.frontier)

    invocations = [e.payload["invocation_id"] for e in events if e.type == "invocation.committed"]
    for inv_id in invocations:
        _verify_receipt(store, run_id, inv_id)

    final = None
    final_digest = None
    if checkpoints and "final" in checkpoints[-1][2].visible:
        final_digest = checkpoints[-1][2].visible["final"]
        final = _normalized_final_state(store, final_digest)

    return {
        "operation": "inspect-replay",
        "run_id": run_id,
        "events": len(events),
        "checkpoints": [
            {"seq": seq, "boundary": cp.boundary, "digest": digest, "phase": cp.phase}
            for seq, digest, cp in checkpoints
        ],
        "invocations": invocations,
        "final": final,
        "final_digest": final_digest,
        "external_calls": 0,
        "demonstrates": "recorded history reconstruction; not recomputation",
    }


def replay_state_at(store: Store, run_id: str, checkpoint_digest: str) -> dict[str, Any]:
    """Materialize the semantic state visible at one checkpoint.

    JSON artifacts are decoded; other (raw) artifacts come back as
    {"digest", "size", "media_type"} rather than raw bytes.
    """
    cp = store.checkpoint(checkpoint_digest)
    if cp.run_id != run_id:
        raise LabrunError(
            f"checkpoint {checkpoint_digest} belongs to {cp.run_id!r}, not {run_id!r}"
        )
    state: dict[str, Any] = {}
    for name, digest in cp.visible.items():
        desc = store.descriptor(digest)
        if desc.media_type == "application/json":
            state[name] = store.get_json(digest)
        else:
            state[name] = {"digest": digest, "size": desc.size, "media_type": desc.media_type}
    return state


def _next_reanalysis_id(store: Store, run_id: str) -> str:
    prefix = f"{run_id}.x"
    used = {i for i in store.list_record_ids("invocation") if i.startswith(prefix)}
    return f"{prefix}{len(used) + 1:03d}"


def _reanalyze_one(
    store: Store, run_id: str, original: InvocationReceipt, transform: str
) -> dict[str, Any]:
    inputs = {name: store.get_bytes(digest) for name, digest in original.inputs.items()}
    out = PARSERS[transform](inputs)
    new_outputs = {
        name: store.put_json(value, labels=["synthetic", "reanalysis-output"]).digest
        for name, value in out.items()
    }
    spec = StepSpec.new(
        operation="parse",
        inputs=original.inputs,
        config={"parser": transform},
        code_ref=f"labrun.fixture:PARSERS[{transform}]",
        determinism="pure",
    )
    spec_digest = store.put_record(spec)
    new_id = _next_reanalysis_id(store, run_id)
    receipt = InvocationReceipt.new(
        invocation_id=new_id,
        run_id=run_id,
        spec_digest=spec_digest,
        invocation_kind="derivation",
        inputs=original.inputs,
        outputs=new_outputs,
        status="succeeded",
        metadata={
            "transform": transform,
            "original_transform": original.metadata["parser"],
            "operation": "reanalyze",
        },
        usage=Usage(),
        started_at=store.clock.now(),
        finished_at=store.clock.now(),
        derived_from=original.invocation_id,
        labels=["reanalysis", "fixture-reanalysis-not-scientific-replication"],
    )
    store.put_record(receipt)
    store.append_event(
        run_id,
        actor="reanalysis",
        type="reanalysis.committed",
        payload={
            "invocation_id": new_id,
            "original_invocation_id": original.invocation_id,
            "transform": transform,
        },
        payload_refs={"receipt": record_ref("invocation", new_id)},
    )
    differences = [
        name
        for name in original.outputs.keys() | new_outputs.keys()
        if original.outputs.get(name) != new_outputs.get(name)
    ]
    return {
        "original": original.invocation_id,
        "derivation": new_id,
        "transform": transform,
        "matches": not differences,
        "original_outputs": dict(original.outputs),
        "new_outputs": new_outputs,
        "differences": sorted(differences),
    }


def reanalyze(
    store: Store, run_id: str, *, step: str = "parse", transform: str | None = None
) -> list[dict[str, Any]]:
    """Rerun a pure transform on recorded bytes and record the comparison.

    Only `step="parse"` (a pure derivation) is reanalyzable. Any other step,
    including an external execution such as `"calc"`, raises `CapabilityError`:
    reexecuting it is a different, separately declared capability.
    """
    if step != "parse":
        raise CapabilityError(
            f"step {step!r} is not a pure transform; reanalysis only reruns "
            "recorded derivations. Reexecution is a different capability."
        )
    if transform is not None and transform not in PARSERS:
        raise CapabilityError(f"unknown transform {transform!r}")
    results = []
    for e in store.events(run_id):
        if e.type != "invocation.committed":
            continue
        receipt = store.receipt(e.payload["invocation_id"])
        if receipt.invocation_kind != "derivation" or "parser" not in receipt.metadata:
            continue
        chosen = transform or receipt.metadata["parser"]
        if chosen not in PARSERS:
            raise CapabilityError(f"unknown transform {chosen!r}")
        results.append(_reanalyze_one(store, run_id, receipt, chosen))
    return results
