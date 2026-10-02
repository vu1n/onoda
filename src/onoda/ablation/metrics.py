"""ABL-002: one run's `OutcomeVector`, read from the agent store alone (SOL_PI_DELTA §3).

`outcome_vector` never opens the evaluator store: the caller already ran
`onoda.adapters.pi.task.evaluate_claims` and hands the resulting `Evaluation`
in. Capability metrics are copied straight from it; efficiency, activation,
and the A15 reducer probe are all read back from the run's own events and
invocation receipts, so nothing here re-derives what ingest already recorded.
Efficiency also counts every RLM child run's receipts: the parent pays (A44).

The A15 probe (`reducer_probe`) is a pure function over one tool result's raw
and reduced JSON content blobs: it parses the raw text as ORCA output with
the same deterministic parser the evaluator uses, collects the distinct
source-line texts its evidence points at, and counts how many are absent from
what the model actually saw. It never reads or grades the reducer's own
receipt (A15, CLAUDE.md); a receipt that is not ORCA output at all (a bash
log, say) simply carries no evidence lines to lose.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from decimal import Decimal

from onoda.ablation.contract import ACTIVATION_METRICS, CAPABILITY_METRICS, OutcomeVector
from onoda.adapters.pi.contract import CTX_MECHANISMS, VARIANTS, mechanisms_on
from onoda.adapters.pi.task import CLAIM_EVALUATOR
from onoda.budgets import descendant_receipts
from onoda.canonical import digest_of
from onoda.errors import OnodaError
from onoda.records import Evaluation, Event, InvocationReceipt
from onoda.science.contract import OrcaObservation
from onoda.science.orca_parser import parse_orca_output
from onoda.store import Store

_ROLE_BUCKET = {"agent": "agent", "reducer": "reducer", "compaction-summary": "summary"}


def outcome_vector(store: Store, evaluation: Evaluation, *, arm: str) -> OutcomeVector:
    """The `OutcomeVector` of the run `evaluation` graded, checked against `arm`.

    Refuses (`OnodaError`) before reading anything else if the run's own
    recorded variant differs from `arm`, or `evaluation` was not written by
    the claim evaluator (`onoda.adapters.pi.task.evaluate_claims`). An RLM
    ablation's run (A55) is recorded as S1r with the arm's mechanisms as its
    `sol_pi.config` override, and counts as that arm.
    """
    run_id = evaluation.run_id
    run = store.run_record(run_id)
    recorded_variant = run.config.get("variant")
    if recorded_variant != arm and not _rlm_run_of(run.config, arm):
        raise OnodaError(
            f"run {run_id} was recorded under variant {recorded_variant!r}, not arm {arm!r}"
        )
    if evaluation.evaluator != CLAIM_EVALUATOR:
        raise OnodaError(
            f"evaluation {evaluation.evaluation_id} is from evaluator {evaluation.evaluator!r}, "
            f"not the claim evaluator {CLAIM_EVALUATOR!r}"
        )

    events = store.events(run_id)
    receipts = [
        store.receipt(e.payload["invocation_id"])
        for e in events
        if e.type == "invocation.committed"
    ]
    decisions = [r for r in receipts if r.invocation_kind == "decision"]
    model = next((r.metadata["model"] for r in decisions if r.metadata.get("role") == "agent"), "")
    ends = [e for e in events if e.type == "pi.spool-end"]
    checkpoints = store.checkpoints(run_id)
    sol_pi_config = checkpoints[-1][2].context.get(CTX_MECHANISMS) if checkpoints else None

    return OutcomeVector(
        run_id=run_id,
        arm=arm,
        case_id=run.config.get("case_id", ""),
        model=model,
        synthetic=run.config.get("mode") == "pi-scripted",
        status=ends[-1].payload["status"] if ends else None,
        capability={k: evaluation.metrics[k] for k in CAPABILITY_METRICS},
        efficiency=_efficiency(store, *_with_children(store, run_id, decisions, receipts)),
        activation=_activation(events),
        reducer_probe=_reducer_probe_totals(store, events),
        mechanism_config=digest_of(sol_pi_config) if sol_pi_config is not None else None,
        evaluation_id=evaluation.evaluation_id,
    )


def _rlm_run_of(config: dict, arm: str) -> bool:
    """Whether a run recorded under an RLM variant ran exactly `arm`'s mechanisms."""
    variant = VARIANTS.get(config.get("variant", ""))
    if variant is None or not variant.rlm or arm not in VARIANTS or VARIANTS[arm].rlm:
        return False
    override = config.get("sol_pi_config_override")
    ran = variant.mechanisms if override is None else mechanisms_on(override)
    return set(ran) == set(VARIANTS[arm].mechanisms)


def _with_children(
    store: Store,
    run_id: str,
    decisions: list[InvocationReceipt],
    receipts: list[InvocationReceipt],
) -> tuple[list[InvocationReceipt], list[InvocationReceipt]]:
    """Add every RLM child run's receipts: the parent pays for its children (A44)."""
    children = descendant_receipts(store, run_id)
    return (
        [*decisions, *(r for r in children if r.invocation_kind == "decision")],
        [*receipts, *children],
    )


def _efficiency(
    store: Store, decisions: list[InvocationReceipt], receipts: list[InvocationReceipt]
) -> dict[str, str | None]:
    """EFFICIENCY_METRICS: payload bytes and request counts by role, tokens, cost, acquisitions."""
    context_bytes: dict[str, int] = defaultdict(int)
    requests: dict[str, int] = defaultdict(int)
    # A total is only known when every response reported its part: an unknown
    # cost is never zero, so one unpriced reducer call makes the run's cost unknown.
    tokens_total: int | None = 0
    cost_total: Decimal | None = Decimal(0)
    responses = 0
    for r in decisions:
        bucket = _ROLE_BUCKET.get(r.metadata.get("role"))
        if bucket is None:
            continue
        requests[bucket] += 1
        context_bytes[bucket] += store.descriptor(r.inputs["context"]).size
        if "message" not in r.outputs:  # no response: nothing was billed or counted
            continue
        responses += 1
        tokens = r.usage.units.get("totalTokens")
        tokens_total = None if tokens is None or tokens_total is None else tokens_total + tokens
        cost = r.usage.cost_usd
        cost_total = None if cost is None or cost_total is None else cost_total + Decimal(cost)
    if responses == 0:
        tokens_total = cost_total = None
    acquisitions = sum(1 for r in receipts if "acquired" in r.labels)
    return {
        "context_bytes.total": str(sum(context_bytes.values())),
        "context_bytes.agent": str(context_bytes["agent"]),
        "context_bytes.reducer": str(context_bytes["reducer"]),
        "context_bytes.summary": str(context_bytes["summary"]),
        "requests.agent": str(requests["agent"]),
        "requests.reducer": str(requests["reducer"]),
        "requests.summary": str(requests["summary"]),
        "tokens.total": str(tokens_total) if tokens_total is not None else None,
        "cost_usd": str(cost_total) if cost_total is not None else None,
        "acquisitions": str(acquisitions),
    }


def _activation(events: list[Event]) -> dict[str, str]:
    """ACTIVATION_METRICS: each mechanism's own event type, counted from the run's journal."""
    counts = Counter(e.type for e in events)
    out = {m: str(counts[event_type]) for m, event_type in ACTIVATION_METRICS.items()}
    fused = ACTIVATION_METRICS["actionFusion"]
    out["actionFusion"] = str(len({e.payload["fusion_id"] for e in events if e.type == fused}))
    return out


def _reducer_probe_totals(store: Store, events: list[Event]) -> dict[str, str]:
    """REDUCER_PROBE_METRICS, summed over every `context.reduced` event in the run."""
    lines_total = lost_total = 0
    for e in events:
        if e.type != "context.reduced":
            continue
        probe = reducer_probe(
            store.get_bytes(e.payload_refs["raw"]), store.get_bytes(e.payload_refs["visible"])
        )
        lines_total += int(probe["reducer.evidence_lines"])
        lost_total += int(probe["reducer.evidence_lost"])
    return {
        "reducer.evidence_lines": str(lines_total),
        "reducer.evidence_lost": str(lost_total),
    }


def _content_text(data: bytes) -> str:
    """Text of a tool result's JSON content blob: its text blocks joined, in order."""
    blocks = json.loads(data)
    if isinstance(blocks, list):
        return "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict))
    return data.decode("utf-8", errors="replace")


def _evidence_texts(observation: OrcaObservation) -> set[str]:
    """Every distinct source-line text the parser's evidence points at."""
    texts: set[str] = set()
    if observation.final_energy is not None:
        texts.add(observation.final_energy.evidence.text)
    for state in observation.excited_states:
        for q in (state.energy_au, state.energy_ev, state.energy_cm):
            if q is not None:
                texts.add(q.evidence.text)
    for ev in observation.warnings:
        texts.add(ev.text)
    for p in observation.problems:
        if p.evidence is not None:
            texts.add(p.evidence.text)
    return texts


def reducer_probe(raw: bytes, visible: bytes) -> dict[str, str]:
    """A15: the deterministic parser's evidence lines in `raw` absent from `visible`.

    `raw` and `visible` are one tool result's raw and reduced JSON content
    blobs (a list of `{"type": "text", "text": ...}` blocks, as Pi's tool
    results carry them). Parses `raw`'s text as ORCA output with the same
    parser the evaluator uses and checks, for each distinct evidence line it
    finds, whether that exact text still appears in `visible`'s text. Never
    reads the reducer's receipt as anything but text to search; a raw result
    that is not ORCA output (e.g. a bash log) yields no evidence lines.
    """
    observation = parse_orca_output(_content_text(raw).encode())
    lines = _evidence_texts(observation)
    visible_text = _content_text(visible)
    lost = sum(1 for line in lines if line not in visible_text)
    return {
        "reducer.evidence_lines": str(len(lines)),
        "reducer.evidence_lost": str(lost),
    }
