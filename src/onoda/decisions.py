"""Decision providers: mock, and offline replay of recorded decisions (BUILD_SPEC §4).

A `DecisionRequest` is everything decision-relevant: question text and version,
evidence digests, ordered candidates, tool schema digest, and policy identity.
Its canonical digest is the replay key. Offline replay serves a recording only
for the exact same request digest; any change fails closed with
`ReplayMismatch` (T04). Several recordings of one request are distinct
invocations and replay must be told which one (T06).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from onoda.canonical import canonical_bytes, digest_of
from onoda.errors import ReplayMismatch
from onoda.records import InvocationReceipt, Usage
from onoda.store import Store

Abstention = Literal["insufficient_evidence", "no_applicable_procedure", "escalate"]


@dataclass(frozen=True)
class DecisionRequest:
    question_set: str
    question: str
    evidence: dict[str, str]
    candidates: tuple[str, ...]
    tool_schema_digest: str
    policy: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return {
            "question_set": self.question_set,
            "question": self.question,
            "evidence": dict(sorted(self.evidence.items())),
            "candidates": list(self.candidates),
            "tool_schema_digest": self.tool_schema_digest,
            "policy": dict(sorted(self.policy.items())),
        }

    def digest(self) -> str:
        return digest_of(self.to_json())


@dataclass(frozen=True)
class DecisionResult:
    action: str | None
    abstention: Abstention | None
    raw_response: bytes
    provider: dict[str, Any]
    usage: Usage = field(default_factory=Usage)
    replayed_from: str | None = None

    def normalized(self) -> dict[str, Any]:
        return {"action": self.action, "abstention": self.abstention}


class DecisionProvider(Protocol):
    name: str

    def decide(
        self,
        request: DecisionRequest,
        evidence: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> DecisionResult: ...


PolicyFn = Callable[[DecisionRequest, dict[str, Any]], "str | tuple[None, Abstention]"]


class MockPolicyProvider:
    """Deterministic policy function; no model, labelled as a mock."""

    def __init__(self, name: str, policy: PolicyFn, *, cost_usd: str | None = "0.00"):
        self.name = name
        self.policy = policy
        self.cost_usd = cost_usd

    def decide(
        self,
        request: DecisionRequest,
        evidence: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> DecisionResult:
        choice = self.policy(request, evidence)
        action, abstention = (None, choice[1]) if isinstance(choice, tuple) else (choice, None)
        if action is not None and action not in request.candidates:
            action, abstention = None, "no_applicable_procedure"
        raw = canonical_bytes(
            {
                "provider": self.name,
                "request": request.digest(),
                "action": action,
                "abstention": abstention,
            }
        )
        return DecisionResult(
            action=action,
            abstention=abstention,
            raw_response=raw,
            provider={"name": self.name, "kind": "deterministic-mock", "model": None},
            usage=Usage(
                units={"decision_calls": 1},
                simulated=True,
                cost_usd=self.cost_usd,
                price_table="fixture-prices@1",
            ),
        )


# exact digest match or ReplayMismatch; no nearest match, no live fallback
class RecordedDecisionProvider:
    """Serves recorded decisions offline by exact request digest; never falls back live."""

    name = "recorded"

    def __init__(self, store: Store, run_ids: list[str] | None = None):
        self.store = store
        self._by_request: dict[str, list[InvocationReceipt]] = {}
        for inv_id in store.list_record_ids("invocation"):
            rec = store.receipt(inv_id)
            # Replayed decisions are reuse, not recordings; indexing them would make
            # every later replay of the same request ambiguous.
            if rec.invocation_kind != "decision" or rec.derived_from is not None:
                continue
            if rec.metadata.get("replayed_from") is not None:
                continue
            if run_ids is not None and rec.run_id not in run_ids:
                continue
            self._by_request.setdefault(rec.metadata["request_digest"], []).append(rec)

    def decide(
        self,
        request: DecisionRequest,
        evidence: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> DecisionResult:
        key = request.digest()
        matches = self._by_request.get(key, [])
        if not matches:
            raise ReplayMismatch(f"no recorded decision for request {key}")
        if invocation_id is not None:
            matches = [m for m in matches if m.invocation_id == invocation_id]
            if not matches:
                raise ReplayMismatch(f"{invocation_id} is not a recording of request {key}")
        elif len(matches) > 1:
            ids = sorted(m.invocation_id for m in matches)
            raise ReplayMismatch(f"request {key} has {len(ids)} recordings {ids}; name one")
        rec = matches[0]
        decision = self.store.get_json(rec.outputs["decision"])
        return DecisionResult(
            action=decision["action"],
            abstention=decision["abstention"],
            raw_response=self.store.get_bytes(rec.outputs["raw_response"]),
            provider={
                "name": self.name,
                "kind": "offline-replay",
                "model": None,
                "recorded_provider": rec.metadata.get("provider"),
            },
            usage=Usage(units={}, simulated=True, cost_usd="0.00"),
            replayed_from=rec.invocation_id,
        )
