"""Pi-first agent adapter seam (SOL_PI_DELTA §0 Route A; BUILD_SPEC §4).

This module sketches the smallest typed boundary between the run-record core
and a research-agent harness (Pi first, Prime as an optional comparison
arm — see docs/handoff/SOL_PI_DELTA.md §0). It does not install or call Pi,
and it invents no Pi SDK method (AGENTS.md rule 10): every place a real
adapter would attach is described in docs/ADAPTER_SEAM.md and marked
"to verify against pinned 0.85.1" there, not assumed here.

`AgentAdapter` externalizes exactly what a portable semantic checkpoint
needs (PORTABLE_RUN_SPEC §checkpoint): what the model was shown
(`render_context`), what it proposed before any side effect
(`propose_action`, routed through the gateway — BUILD_SPEC §4), and an
observable record of the underlying call (`capture`). Nothing here is a
persistent kernel, host cache, or daemon; those stay inside a concrete
adapter's implementation and must still be externalized at every checkpoint
boundary (AGENTS.md rule 6).

`CallRecord` deliberately mirrors `onoda.records.InvocationReceipt` /
`Usage` field-for-field so a real gateway can commit one directly; this
module never writes to the store itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from onoda.canonical import digest_of
from onoda.records import Digest, Usage


def _json_safe(value: Any) -> Any:
    """Best-effort, deterministic reduction to a canonicalizable value.

    Used only to compute a stable digest over caller-supplied context; it
    never feeds a canonical store record directly. Floats and other
    non-canonicalizable types are stringified rather than dropped, so the
    digest still changes when they change.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return repr(value)


@dataclass(frozen=True)
class ToolSpec:
    """One tool the adapter may offer the model, by name and JSON schema."""

    name: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class RenderedContext:
    """What the adapter is about to show the model — a recorded artifact.

    This is the comparison unit a cross-harness semantic fork needs: two
    adapters (Pi, Prime) rendering the same checkpoint state should each
    produce a `RenderedContext` that is meaningful at this seam even though
    their internal prompt construction differs completely.
    """

    rendered_digest: Digest
    messages: tuple[Mapping[str, Any], ...]
    tool_specs: tuple[ToolSpec, ...] = field(default_factory=tuple)
    truncated: bool = False
    source_checkpoint: str | None = None


@dataclass(frozen=True)
class ProposedAction:
    """A model-proposed action, not yet executed.

    Every side effect (a solver submission, a file write) is routed through
    the gateway before it happens (BUILD_SPEC §4); this value is what the
    gateway receives, not evidence that anything ran.
    """

    action_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    raw_model_output: str


@dataclass(frozen=True)
class CallRecord:
    """Observable record of one underlying provider call.

    Field names match `onoda.records.InvocationReceipt` / `Usage` so a
    gateway can commit this directly as evidence; nothing here depends on
    unobservable model-internal reasoning (AGENTS.md rule 13).
    """

    request_body: Mapping[str, Any]
    response: Mapping[str, Any]
    model_id: str
    usage: Usage
    request_id: str | None
    adapter_name: str
    adapter_version: str


@runtime_checkable
class AgentAdapter(Protocol):
    """Harness-neutral seam a research-agent adapter implements.

    `name` and `version` identify the adapter and its pinned upstream
    revision (see provenance/sources.json), not a model. See
    docs/ADAPTER_SEAM.md for how Pi's real extension points would attach
    and for the explicit list of what is unverified against pinned 0.85.1.
    """

    name: str
    version: str

    def render_context(self, checkpoint_state: Mapping[str, Any]) -> RenderedContext:
        """Externalize what the model would be shown for this checkpoint."""
        ...

    def propose_action(
        self, rendered: RenderedContext, tools: Sequence[ToolSpec]
    ) -> ProposedAction:
        """Propose one action given rendered context and offered tools.

        Must not perform the action; execution happens only after the
        gateway records the proposal (BUILD_SPEC §4).
        """
        ...

    def capture(self, call: ProposedAction) -> CallRecord:
        """Return the observable call record for an already-executed action."""
        ...


class DeterministicMockAdapter:
    """Offline, deterministic `AgentAdapter` for tests and M0/M1 fixtures.

    Every method is a pure function of its inputs — same `checkpoint_state`
    and `tools` always produce the same `RenderedContext` / `ProposedAction`
    / `CallRecord`. No randomness, no wall clock, no network, no real model
    call: this is a mock, and its outputs must never be presented as a real
    provider response (AGENTS.md rule 11).
    """

    name = "deterministic-mock"
    version = "0.1.0"

    def render_context(self, checkpoint_state: Mapping[str, Any]) -> RenderedContext:
        safe_state = _json_safe(dict(checkpoint_state))
        messages = (
            {"role": "system", "content": f"{self.name}/{self.version}"},
            {"role": "user", "content": safe_state},
        )
        rendered_digest = digest_of({"messages": [dict(m) for m in messages]})
        checkpoint_id = safe_state.get("checkpoint_id") if isinstance(safe_state, dict) else None
        return RenderedContext(
            rendered_digest=rendered_digest,
            messages=messages,
            tool_specs=(),
            truncated=False,
            source_checkpoint=checkpoint_id if isinstance(checkpoint_id, str) else None,
        )

    def propose_action(
        self, rendered: RenderedContext, tools: Sequence[ToolSpec]
    ) -> ProposedAction:
        tool_name = tools[0].name if tools else "noop"
        return ProposedAction(
            action_id=rendered.rendered_digest,
            tool_name=tool_name,
            arguments={},
            raw_model_output=f"mock-action:{tool_name}",
        )

    def capture(self, call: ProposedAction) -> CallRecord:
        response = {"tool_name": call.tool_name, "arguments": dict(call.arguments)}
        return CallRecord(
            request_body={
                "action_id": call.action_id,
                "raw_model_output": call.raw_model_output,
            },
            response=response,
            model_id=f"{self.name}/{self.version}",
            usage=Usage(units={"mock_units": 0}, measured=True, simulated=True),
            request_id=f"mock-{call.action_id}",
            adapter_name=self.name,
            adapter_version=self.version,
        )
