"""Per-run numbers for a container Pi run, read from its verified spool.

Everything here comes from the recorder's spool (HMAC-checked by
`ingest._read_spool`) and, for SoL-Pi's own decisions, the session log Pi
wrote next to it. Nothing is ingested into a store: a container run has no
`TaskManifest`, and its recorded session path is a container path. The
numbers are counts and sums of what was spooled, not gate metrics.

Mechanism activity, by what was recorded:

- reducer: `provider.request` lines with purpose `reducer` are calls sent to
  the reducer model; a visible tool result whose details carry SoL-Pi's
  `evidencePreservingReducer` receipt is one the reducer actually rewrote.
- compaction: `compaction` lines (`done` is a finished compaction) and
  `provider.request` lines with purpose `summary`.
- Action Fusion: allowed `edit`/`write` calls whose input has a `then_run`.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from onoda.adapters.pi.contract import (
    Compaction,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
    ToolResult,
)
from onoda.adapters.pi.ingest import _read_spool

METRICS_SCHEMA = "onoda.tb-run-metrics/1"
_PURPOSES = ("agent", "reducer", "summary")
_TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite")
# SoL-Pi's session entry types (sol-pi src: reducer config.ts, online-context-compact state.ts).
_REDUCER_ENTRY = "sol-pi-evidence-preserving-reducer-v1"


def _json(objects: dict[str, bytes], digest: str | None) -> Any:
    if digest is None:
        return None
    try:
        return json.loads(objects[digest])
    except ValueError:
        return None


def _reducer_journal(session_dir: Path) -> dict[str, int]:
    """SoL-Pi's own reducer decisions by kind (fallbacks split by reason)."""
    out: Counter[str] = Counter()
    for path in sorted(session_dir.rglob("*.jsonl")) if session_dir.is_dir() else []:
        for raw in path.read_text(errors="replace").splitlines():
            if _REDUCER_ENTRY not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            data = entry.get("data") if isinstance(entry, dict) else None
            if not isinstance(data, dict) or entry.get("customType") != _REDUCER_ENTRY:
                continue
            kind = str(data.get("kind"))
            out[f"fallback:{data.get('reason')}" if kind == "fallback" else kind] += 1
    return dict(sorted(out.items()))


def run_metrics(spool_dir: Path, key: bytes | None, session_dir: Path) -> dict[str, Any]:
    """Read and verify the spool under `spool_dir`; raises `IntegrityError` on tampering."""
    spool = _read_spool(spool_dir, key)
    requests: dict[str, int] = dict.fromkeys(_PURPOSES, 0)
    tokens = {p: dict.fromkeys(_TOKEN_KEYS, 0) for p in _PURPOSES}
    cost = dict.fromkeys(_PURPOSES, Decimal(0))
    context_bytes: dict[str, int] = dict.fromkeys(_PURPOSES, 0)
    unpriced = 0
    purpose_of: dict[int, str] = {}
    tools: Counter[str] = Counter()
    blocked: Counter[str] = Counter()
    fused = reduced = 0
    reduced_source_bytes = reduced_receipt_bytes = 0
    compactions: Counter[str] = Counter()

    for line in spool.lines:
        if isinstance(line, ProviderRequest):
            purpose_of[line.request_index] = line.purpose
            requests[line.purpose] += 1
            context_bytes[line.purpose] += len(spool.objects[line.context])
        elif isinstance(line, ProviderResponse):
            purpose = purpose_of.get(line.request_index, "agent")
            for k in _TOKEN_KEYS:
                tokens[purpose][k] += line.usage_tokens.get(k, 0)
            if line.cost_usd is None:
                unpriced += 1
            else:
                cost[purpose] += Decimal(line.cost_usd)
        elif isinstance(line, ToolCall):
            if line.decision == "blocked":
                blocked[line.tool_name] += 1
                continue
            tools[line.tool_name] += 1
            if line.tool_name in ("edit", "write"):
                args = _json(spool.objects, line.input)
                then_run = args.get("then_run") if isinstance(args, dict) else None
                if isinstance(then_run, dict) and isinstance(then_run.get("command"), str):
                    fused += 1
        elif isinstance(line, ToolResult) and line.stage == "visible":
            details = _json(spool.objects, line.details)
            receipt = (
                details.get("evidencePreservingReducer") if isinstance(details, dict) else None
            )
            if isinstance(receipt, dict):
                reduced += 1
                reduced_source_bytes += int(receipt.get("sourceBytes") or 0)
                reduced_receipt_bytes += int(receipt.get("receiptBytes") or 0)
        elif isinstance(line, Compaction):
            compactions[line.phase] += 1

    end = spool.end
    total_tokens = {k: sum(tokens[p][k] for p in _PURPOSES) for k in _TOKEN_KEYS}
    return {
        "schema": METRICS_SCHEMA,
        "spool_verified": key is not None,
        "spool_digest": spool.digest,
        "variant": spool.start.variant,
        "mechanisms": list(spool.start.mechanisms),
        "model": spool.start.model,
        "end_status": end.status if end else None,  # None: killed before spool.end
        "end_reason": end.reason if end else None,
        "requests": requests,
        "tokens": tokens,
        "tokens_total": total_tokens,
        "cost_usd": {p: str(cost[p]) for p in _PURPOSES},
        "cost_usd_total": str(sum(cost.values(), Decimal(0))),
        "unpriced_responses": unpriced,
        "context_bytes": context_bytes,
        "tool_calls": dict(sorted(tools.items())),
        "tool_calls_blocked": dict(sorted(blocked.items())),
        "reducer": {
            "requests": requests["reducer"],
            "results_reduced": reduced,
            "source_bytes": reduced_source_bytes,
            "receipt_bytes": reduced_receipt_bytes,
            "journal": _reducer_journal(session_dir),
        },
        "compaction": {
            "summary_requests": requests["summary"],
            "phases": dict(sorted(compactions.items())),
        },
        "action_fusion": {"fused_calls": fused},
        "observation_pack": {"obs_recall_calls": tools.get("obs_recall", 0)},
        "online_context_compact": {"update_plan_calls": tools.get("update_plan", 0)},
    }
