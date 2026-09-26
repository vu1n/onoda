"""SoL-Pi context mechanisms as recorded fixtures (SOL_PI_DELTA §2, §4).

Four mechanisms, each a thin layer over `Store` so every effect is a committed
artifact or a journal event, never hidden agent-side state:

- `ObservationPack` backs handles with store digests; paged recall and aging
  are recorded reads, and its `state()` is exactly what a checkpoint holds.
- `record_compaction`/`compaction_history` make each compaction trigger a
  replayable journal entry instead of a silent context rewrite.
- `reduce_log` runs a deterministic fidelity verifier over a reducer's receipt
  and falls back to the raw log on any failure; verification proves the
  receipt didn't lie about the log, not that it kept everything relevant.
- `decompose_fused_call` turns an allowed batch of idempotent local checks
  into individually recorded events, and refuses to fuse anything that
  submits, cancels, or affects budget.

`activation_counts` reads all of the above back from one run's journal.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from labrun.canonical import canonical_bytes, sha256_digest
from labrun.clock import Clock
from labrun.errors import CanonicalizationError, CapabilityError
from labrun.records import Event, InvocationReceipt, StepSpec, Usage
from labrun.store import Store

_EXCERPT_BYTES = 256

# --------------------------------------------------------------------- packs


class ObservationPack:
    """Archives large tool outputs and ages them into excerpts over requests.

    A "request" is one turn of the harness; `render` is called once per turn
    with that turn's index. An observation is shown in full for the first
    `full_requests` calls to `render` whose index is at or after its arrival
    (age 0..full_requests-1), then only as {handle, size, head, tail}.
    Outputs at or below `threshold_bytes` are never archived and always
    render in full. A `handle` is the artifact's store digest, so it resolves
    in any store holding the same object.
    """

    SCHEMA = "labrun.context.observation-pack/1"

    def __init__(
        self,
        store: Store,
        run_id: str,
        *,
        threshold_bytes: int = 10240,
        full_requests: int = 2,
    ):
        self.store = store
        self.run_id = run_id
        self.threshold_bytes = threshold_bytes
        self.full_requests = full_requests
        self._current_request = 0
        self._observations: dict[str, dict[str, Any]] = {}

    def observe(self, name: str, data: bytes, media_type: str) -> str:
        """Archive `data` and return its handle (the store digest)."""
        desc = self.store.put_bytes(data, media_type=media_type, labels=["observation"])
        archived = len(data) > self.threshold_bytes
        self._observations[name] = {
            "handle": desc.digest,
            "size": desc.size,
            "media_type": media_type,
            "arrival_request": self._current_request,
            "archived": archived,
        }
        if archived:
            self.store.append_event(
                self.run_id,
                actor="context.observation-pack",
                type="context.observe",
                payload={
                    "name": name,
                    "size": desc.size,
                    "arrival_request": self._current_request,
                },
                payload_refs={"observation": desc.digest},
            )
        return desc.digest

    def render(self, request_index: int) -> list[dict[str, Any]]:
        """Rendered context for `request_index`: full text or an excerpt."""
        self._current_request = request_index
        out: list[dict[str, Any]] = []
        for name, obs in self._observations.items():
            age = request_index - obs["arrival_request"]
            entry: dict[str, Any] = {
                "name": name,
                "handle": obs["handle"],
                "size": obs["size"],
                "arrival_request": obs["arrival_request"],
                "age": age,
            }
            if not obs["archived"] or age < self.full_requests:
                entry["full"] = True
                entry["data"] = self.store.get_bytes(obs["handle"])
            else:
                entry["full"] = False
                data = self.store.get_bytes(obs["handle"])
                entry["head"] = data[:_EXCERPT_BYTES]
                entry["tail"] = data[-_EXCERPT_BYTES:] if len(data) > _EXCERPT_BYTES else b""
            out.append(entry)
        return out

    def page(self, handle: str, start: int, end: int) -> bytes:
        """Read an exact slice of an archived output; records the read."""
        data = self.store.get_bytes(handle)
        chunk = data[start:end]
        self.store.append_event(
            self.run_id,
            actor="context.observation-pack",
            type="context.page",
            payload={
                "handle": handle,
                "start": start,
                "end": end,
                "slice_digest": sha256_digest(chunk),
            },
        )
        return chunk

    def state(self) -> dict[str, Any]:
        """JSON-serializable snapshot; this is what a checkpoint stores."""
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "threshold_bytes": self.threshold_bytes,
            "full_requests": self.full_requests,
            "current_request": self._current_request,
            "observations": {
                name: {
                    "handle": obs["handle"],
                    "size": obs["size"],
                    "media_type": obs["media_type"],
                    "arrival_request": obs["arrival_request"],
                    "archived": obs["archived"],
                    "age": self._current_request - obs["arrival_request"],
                }
                for name, obs in self._observations.items()
            },
        }

    @classmethod
    def from_state(cls, store: Store, run_id: str, state: dict[str, Any]) -> ObservationPack:
        """Restore a pack from `state()`. Handles resolve against `store`."""
        pack = cls(
            store,
            run_id,
            threshold_bytes=state["threshold_bytes"],
            full_requests=state["full_requests"],
        )
        pack._current_request = state["current_request"]
        pack._observations = {
            name: {
                "handle": obs["handle"],
                "size": obs["size"],
                "media_type": obs["media_type"],
                "arrival_request": obs["arrival_request"],
                "archived": obs["archived"],
            }
            for name, obs in state["observations"].items()
        }
        return pack


# ------------------------------------------------------------------ compaction


def record_compaction(
    store: Store,
    run_id: str,
    *,
    trigger_inputs: dict[str, Any],
    summary: bytes,
    replaced_refs: list[str],
    boundary: str | None,
) -> Event:
    """Store `summary` as a derived interpretation and journal the trigger."""
    desc = store.put_bytes(summary, media_type="text/plain", labels=["derived-interpretation"])
    return store.append_event(
        run_id,
        actor="context.compaction",
        type="context.compaction",
        payload={
            "trigger_inputs": trigger_inputs,
            "replaced_refs": list(replaced_refs),
            "boundary": boundary,
        },
        payload_refs={"summary": desc.digest},
    )


def compaction_history(store: Store, run_id: str) -> list[dict[str, Any]]:
    """Replay every recorded compaction trigger, in journal order."""
    out = []
    for event in store.events(run_id):
        if event.type != "context.compaction":
            continue
        out.append(
            {
                "event_id": event.event_id,
                "seq": event.seq,
                "trigger_inputs": event.payload["trigger_inputs"],
                "replaced_refs": event.payload["replaced_refs"],
                "boundary": event.payload["boundary"],
                "summary_handle": event.payload_refs["summary"],
            }
        )
    return out


# --------------------------------------------------------------------- reducer

REQUIRED_RECEIPT_KEYS = ("source_digest", "exit_status", "quotes", "summary")


@dataclass(frozen=True)
class ReducerResult:
    """Outcome of `reduce_log`: a verified receipt, or a recorded fallback."""

    success: bool
    digest: str  # receipt artifact digest on success, raw log digest on fallback
    receipt: dict[str, Any] | None = None
    invocation: InvocationReceipt | None = None
    reason: str | None = None


def _verify_receipt(receipt: Any, log: bytes, exit_status: int) -> str | None:
    """Deterministic fidelity check. Returns a failure reason, or None."""
    if not isinstance(receipt, dict):
        return "receipt is not an object"
    for key in REQUIRED_RECEIPT_KEYS:
        if key not in receipt:
            return f"receipt missing required key {key!r}"
    if not isinstance(receipt["source_digest"], str):
        return "source_digest is not a string"
    if not isinstance(receipt["exit_status"], int) or isinstance(receipt["exit_status"], bool):
        return "exit_status is not an int"
    quotes = receipt["quotes"]
    if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
        return "quotes is not a list of strings"
    if not isinstance(receipt["summary"], str):
        return "summary is not a string"
    if receipt["source_digest"] != sha256_digest(log):
        return "source_digest does not match the log"
    if receipt["exit_status"] != exit_status:
        return "exit_status does not match the observed status"
    text = log.decode("utf-8", errors="replace")
    for quote in quotes:
        if quote not in text:
            return f"quote not found verbatim in log: {quote!r}"
    try:
        receipt_size = len(canonical_bytes(receipt))
    except CanonicalizationError as exc:
        return f"receipt is not canonicalizable: {exc}"
    if receipt_size >= len(log):
        return "receipt is not smaller than the source log"
    return None


def reduce_log(
    store: Store,
    run_id: str,
    log: bytes,
    reducer: Callable[[bytes], dict[str, Any]],
    *,
    exit_status: int,
    invocation_id: str,
    clock: Clock,
) -> ReducerResult:
    """Run `reducer` over `log` and verify its receipt deterministically.

    On success the receipt is stored labelled "derived-interpretation" (never
    "parser-output") and a derivation `InvocationReceipt` is written. On any
    verification failure a "context.reducer-fallback" event is appended and
    the raw log's digest is returned instead. Verification proves the receipt
    didn't misquote or misdate the log; it says nothing about whether the
    receipt kept every diagnostic the log contained (see
    `required_diagnostics_present`).
    """
    log_desc = store.put_bytes(log, media_type="text/plain", labels=["log"])
    receipt = reducer(log)
    failure = _verify_receipt(receipt, log, exit_status)
    if failure is not None:
        store.append_event(
            run_id,
            actor="context.reducer",
            type="context.reducer-fallback",
            payload={
                "reason": failure,
                "invocation_id": invocation_id,
                "exit_status": exit_status,
            },
            payload_refs={"log": log_desc.digest},
        )
        return ReducerResult(success=False, digest=log_desc.digest, reason=failure)

    receipt_desc = store.put_json(receipt, labels=["derived-interpretation"])
    reducer_id = getattr(reducer, "__name__", reducer.__class__.__name__)
    step = StepSpec.new(
        operation="context.reduce_log",
        inputs={"log": log_desc.digest},
        code_ref=f"labrun.context.reduce_log@{reducer_id}",
        determinism="nondeterministic",
    )
    spec_digest = store.put_record(step)
    now = clock.now()
    invocation = InvocationReceipt.new(
        invocation_id=invocation_id,
        run_id=run_id,
        spec_digest=spec_digest,
        invocation_kind="derivation",
        inputs={"log": log_desc.digest},
        outputs={"receipt": receipt_desc.digest},
        status="succeeded",
        metadata={"reducer_id": reducer_id},
        usage=Usage(
            units={"reducer_tokens": max(1, receipt_desc.size // 4)},
            measured=False,
            simulated=True,
        ),
        started_at=now,
        finished_at=now,
        labels=["reducer-receipt"],
    )
    store.put_record(invocation)
    return ReducerResult(
        success=True, digest=receipt_desc.digest, receipt=receipt, invocation=invocation
    )


def required_diagnostics_present(
    receipt: dict[str, Any], required: list[str]
) -> dict[str, list[str]]:
    """Check whether each string in `required` survives verbatim in the receipt.

    A passing verifier only proves fidelity (no misquote); it does not prove
    completeness. This reports what a receipt dropped so that gap is visible.
    """
    haystack = receipt.get("summary", "") + "\n" + "\n".join(receipt.get("quotes", []))
    present = [item for item in required if item in haystack]
    missing = [item for item in required if item not in haystack]
    return {"present": present, "missing": missing}


# ---------------------------------------------------------------------- fusion

FUSABLE_OPERATIONS = frozenset({"write_input", "validate_input", "read_file"})
_UNFUSABLE_OPERATIONS = frozenset({"submit", "cancel"})


def decompose_fused_call(store: Store, run_id: str, steps: list[dict[str, Any]]) -> list[Event]:
    """Decompose an allowed batch of idempotent local checks into events.

    Raises `CapabilityError` before recording anything if any step is
    "submit", "cancel", declares `budget_impact: True`, or is otherwise not
    in the allowlist. Allowed steps each become their own "context.fused-step"
    event, in order, sharing one fusion id.
    """
    for step in steps:
        operation = step.get("operation")
        if operation in _UNFUSABLE_OPERATIONS:
            raise CapabilityError(
                f"cannot fuse {operation!r}: submit/cancel must be individually gated"
            )
        if step.get("budget_impact"):
            raise CapabilityError(f"cannot fuse {operation!r}: step declares budget_impact")
        if operation not in FUSABLE_OPERATIONS:
            raise CapabilityError(
                f"cannot fuse {operation!r}: not in the idempotent local-check allowlist"
            )

    fusion_id = uuid.uuid4().hex
    events: list[Event] = []
    for index, step in enumerate(steps):
        payload = dict(step)
        payload["fusion_id"] = fusion_id
        payload["step_index"] = index
        event = store.append_event(
            run_id,
            actor="context.fusion",
            type="context.fused-step",
            payload=payload,
            causes=[events[-1].event_id] if events else [],
        )
        events.append(event)
    return events


# ------------------------------------------------------------------ activation


def activation_counts(store: Store, run_id: str) -> dict[str, int]:
    """Per-mechanism activation counts from the run's journal."""
    counts: Counter[str] = Counter()
    for event in store.events(run_id):
        if event.type.startswith("context."):
            counts[event.type] += 1
    return dict(counts)
