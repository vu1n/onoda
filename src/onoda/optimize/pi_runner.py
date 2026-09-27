"""The optimizer's `Runner` over real Pi runs (M4-007).

One trial = `run_case` (the candidate staged into a real pinned Pi process)
-> `evaluate_claims` in the separate evaluator store -> M3b's
`outcome_vector`, which also charges every RLM child run to the parent (A44).

The trace GEPA reflects on is read back from the agent store only: the main
model's responses (`agent`), each tool call's input (`agent`), and each tool's
raw result (`raw_tool`). Where SoL-Pi's reducer rewrote a result, the rewritten
text is tagged `reducer`, so the adapter drops it (the evaluator and the
optimizer never learn from reducer receipts, A15). Nothing from the evaluator
store reaches a trace but the verdict word per claim tag.

Scripted mode needs one script per case: its outputs are synthetic, so a search
over scripted trials exercises the loop, not a harness improvement (A32).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onoda.ablation.metrics import outcome_vector
from onoda.adapters.pi.contract import HarnessCandidate, Script
from onoda.adapters.pi.ingest import run_claim
from onoda.adapters.pi.run import run_case
from onoda.adapters.pi.runtime import LiveSettings
from onoda.adapters.pi.sandbox import Sandbox
from onoda.adapters.pi.task import evaluate_claims
from onoda.errors import OnodaError
from onoda.optimize.adapter import TraceStep, Trial
from onoda.science.contract import CASES_ROOT, case_manifests, load_case
from onoda.store import Store


def _text_blocks(message: Any) -> str:
    content = message.get("content", []) if isinstance(message, dict) else []
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def _content_text(data: bytes) -> str:
    """A tool result's content blocks (JSON) as plain text."""
    try:
        blocks = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return data.decode(errors="replace")
    if not isinstance(blocks, list):
        return json.dumps(blocks, sort_keys=True)
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def trace(store: Store, run_id: str) -> tuple[TraceStep, ...]:
    """What the agent did and saw in `run_id`, in journal order, tagged by source."""
    steps: list[TraceStep] = []
    for e in store.events(run_id):
        if e.type != "invocation.committed":
            continue
        r = store.receipt(e.payload["invocation_id"])
        if r.invocation_kind == "decision":
            if r.metadata.get("role") == "agent" and "message" in r.outputs:
                text = _text_blocks(store.get_json(r.outputs["message"]))
                if text:
                    steps.append(TraceStep(kind="response", source="agent", text=text))
            continue
        name = r.metadata.get("tool_name")
        if name is None:
            continue
        args = store.get_bytes(r.inputs["input"]).decode(errors="replace")
        steps.append(TraceStep(kind="tool_call", source="agent", text=f"{name} {args}"))
        if "raw" in r.outputs:
            raw = _content_text(store.get_bytes(r.outputs["raw"]))
            steps.append(TraceStep(kind="tool_result", source="raw_tool", text=raw))
            if "result" in r.outputs and r.outputs["result"] != r.outputs["raw"]:
                shown = _content_text(store.get_bytes(r.outputs["result"]))
                steps.append(TraceStep(kind="tool_result", source="reducer", text=shown))
        elif "result" in r.outputs:
            shown = _content_text(store.get_bytes(r.outputs["result"]))
            steps.append(TraceStep(kind="tool_result", source="raw_tool", text=shown))
    return tuple(steps)


@dataclass
class PiRunner:
    """`Runner` over `run_case`. Exactly one of `scripts` or `live` is set."""

    store: Store  # the agent store
    evaluator_store: Store
    work_dir: Path
    variant_id: str
    scripts: Mapping[str, Script] | None = None  # case id -> script (offline; synthetic)
    live: LiveSettings | None = None
    sandbox: Sandbox | None = None  # required when live (A38, A50)
    ladder: bytes | None = None
    synthetic_ladder: bool = False
    docs: Mapping[str, bytes] = field(default_factory=dict)
    max_requests: int | None = None
    timeout_s: float = 300
    cases_root: Path = CASES_ROOT

    def __post_init__(self) -> None:
        if (self.scripts is None) == (self.live is None):
            raise OnodaError("PiRunner needs exactly one of scripts or live")

    def script_for(self, candidate: HarnessCandidate, case_id: str) -> Script | None:
        """The scripted provider's script for this trial (None when live)."""
        if self.scripts is None:
            return None
        if case_id not in self.scripts:
            raise OnodaError(f"no script for case {case_id!r}")
        return self.scripts[case_id]

    def run(self, candidate: HarnessCandidate, case_id: str, *, trial_id: str) -> Trial:
        manifests = case_manifests(self.cases_root)
        if case_id not in manifests:
            raise OnodaError(f"unknown case {case_id!r}")
        case = load_case(manifests[case_id])
        run_case(
            self.store,
            case,
            run_id=trial_id,
            variant_id=self.variant_id,
            work_dir=self.work_dir / trial_id,
            script=self.script_for(candidate, case_id),
            live=self.live,
            docs=self.docs,
            ladder=self.ladder,
            synthetic_ladder=self.synthetic_ladder,
            max_requests=self.max_requests,
            timeout_s=self.timeout_s,
            candidate=candidate,
            sandbox=self.sandbox,
        )
        claims = [e for e in self.store.events(trial_id) if e.type == "pi.claim"]
        evaluation = evaluate_claims(
            self.evaluator_store,
            run_id=trial_id,
            case=case,
            claim=run_claim(self.store, trial_id),
            claim_ref=claims[-1].payload_refs["claim"] if claims else None,
        )
        return Trial(
            case_id=case_id,
            run_id=trial_id,
            outcome=outcome_vector(self.store, evaluation, arm=self.variant_id),
            steps=trace(self.store, trial_id),
            verdicts=dict(evaluation.assignments),
        )
