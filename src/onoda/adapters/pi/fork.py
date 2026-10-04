"""Fork a recorded Pi run at one of its checkpoints (A36).

A Pi fork is a new Pi run that reproduces its parent up to a checkpoint and
then continues with one thing changed: who answers the agent's next request.
Nothing in the parent is written, and the child inherits no receipt: it
re-runs the parent's prefix itself, under the parent's recorded responses,
and records every step as its own.

The fork point is the parent's first agent request after the checkpoint
(`resume_request`). Every provider response the parent got before it (agent,
reducer and compaction-summary turns) becomes the prefix of the child's
script. A scripted parent's turns are keyed with its replay keys, so the
child's scripted provider fails closed (`replay-mismatch`) the moment its
context drifts from the parent's. A live parent's keys hash its provider's
request body, which a scripted provider never sees, so its prefix is served
unkeyed. Either way, after the run every prefix tool call must have shown
the child the result it showed the parent (`check_prefix`, with each run's
work dir normalized), or the fork is refused. A checkpoint inside an agent turn resumes at the
end of that turn: the turn's remaining tool calls are part of the prefix.
After the prefix the continuation answers: a script (offline, synthetic),
or a live model (`live`). A live continuation is a handoff: the scripted
provider serves the prefix, then switches the session to the live model, and
the run is a live run (sandbox, credential, its own explicit request and cost
caps, on top of the prefix, which is served at no cost). Its prefix requests
are recorded as `replayed`, answered by the scripted provider.
For a VTE parent, the broker serves the parent's jobs that the prefix's tool
calls submitted, as a replay, and runs the continuation's jobs live.

The child is recorded with a `ForkDeclaration` (intervention
`decision-policy-change`, nothing invalidated, the parent's checkpoint as its
visibility cut), a `Run.parent`, a `fork.created` event and that cut on every
checkpoint, and `config["fork"]` naming the parent, the checkpoint and the
prefix. A fork whose prefix diverged is still recorded, then refused with
`ReplayMismatch`.

Not yet forkable: a live continuation of a variant with a reducer, or one
whose prefix ends with a compaction summary (the handoff happens at a turn
end, before the next summary); RLM runs (their children's scripts would have to be split
at the fork point too), imported runs (the prefix re-executes the parent's
tool calls; import is data-only) and a finalized checkpoint (nothing follows
it).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from onoda.adapters.pi.contract import VARIANTS, HarnessCandidate, Script, ScriptTurn
from onoda.adapters.pi.ingest import _turn
from onoda.adapters.pi.run import (
    ForkLaunch,
    PiRun,
    _cited_jobs,
    recorded_staging,
    run_case,
    run_vte_case,
)
from onoda.adapters.pi.runtime import LiveSettings
from onoda.adapters.pi.sandbox import PiSandbox
from onoda.errors import ForkError, ReplayMismatch
from onoda.portability import imported_runs
from onoda.records import Checkpoint, ForkDeclaration, InvocationReceipt, ParentRef, Run
from onoda.science.contract import LoadedCase
from onoda.science.orca_broker import BrokerReplay, parse_journal
from onoda.science.orca_local import OrcaSettings
from onoda.science.vte import LoadedVteCase
from onoda.store import Store, check_safe_id, record_ref

FORK_SCHEMA = "onoda.pi-fork/1"
PI_MODES = ("pi-scripted", "pi-live")
ROLES = {"agent": "turns", "reducer": "reducer_turns", "compaction-summary": "summary_turns"}
BudgetTreatment = Literal["inherit-remaining", "fresh-allowance"]


@dataclass(frozen=True)
class ForkPoint:
    """Where a fork of `parent` resumes, and the recorded responses that get it there."""

    parent: Run
    checkpoint: str
    cut: dict[str, int]
    resume_request: int  # the parent's first agent request after the checkpoint
    prefix: Script  # every parent response before `resume_request`, keyed
    tool_calls: frozenset[str]  # tool call ids the prefix's agent turns propose
    last_role: str | None = None  # the role of the prefix's last request

    def counts(self) -> dict[str, int]:
        return {
            "agent": len(self.prefix.turns),
            "reducer": len(self.prefix.reducer_turns),
            "compaction-summary": len(self.prefix.summary_turns),
        }


def _decisions(store: Store, run_id: str) -> list[tuple[int, InvocationReceipt]]:
    """(event seq, receipt) for every provider-request receipt, in journal order."""
    out = []
    for event in store.events(run_id):
        if event.type == "invocation.committed":
            receipt = store.receipt(event.payload["invocation_id"])
            if receipt.invocation_kind == "decision":
                out.append((event.seq, receipt))
    return out


# Context: doc://onoda/record-integrity@0002#immutable-single-writer-records — fork only at a journaled quiescent checkpoint; the child re-runs the prefix, it inherits nothing
def fork_point(store: Store, parent_run_id: str, checkpoint: str) -> ForkPoint:
    """Check that `parent_run_id` can be forked at `checkpoint`; find the fork point."""
    if not store.has_record("run", parent_run_id):
        raise ForkError(f"unknown parent run {parent_run_id}")
    parent = store.run_record(parent_run_id)
    cfg = parent.config
    if cfg.get("mode") not in PI_MODES:
        raise ForkError(f"run {parent_run_id} is not a Pi run; see `onoda fork`")
    if "parent_run" in cfg or VARIANTS[cfg["variant"]].rlm:
        raise ForkError(f"run {parent_run_id} is an RLM run; RLM runs cannot be forked yet")
    if parent_run_id in imported_runs(store):
        raise ForkError(
            f"run {parent_run_id} was imported; a fork re-executes its recorded tool calls, "
            "so only runs recorded in this store are forked"
        )
    journaled = [
        e
        for e in store.events(parent_run_id)
        if e.type == "checkpoint"
        and e.payload_refs.get("checkpoint") == record_ref("checkpoint", checkpoint)
    ]
    if not journaled:
        raise ForkError(f"checkpoint {checkpoint} is not journaled in {parent_run_id}")
    cp = store.get_record("checkpoint", checkpoint)
    assert isinstance(cp, Checkpoint)
    if not cp.quiescent:
        raise ForkError(f"checkpoint {checkpoint} is not quiescent")
    if cp.boundary == "finalized":
        raise ForkError(f"checkpoint {checkpoint} is the run's end; nothing follows it to change")
    decisions = _decisions(store, parent_run_id)
    after = [
        r.metadata["request_index"]
        for seq, r in decisions
        if seq > journaled[0].seq and r.metadata["role"] == "agent"
    ]
    if not after:
        raise ForkError(f"no agent request follows checkpoint {checkpoint}; nothing to change")
    resume = min(after)
    # A live run's replay keys hash the provider's own request body, which a
    # scripted provider never sees: its prefix is served unkeyed and checked
    # by its tool results instead (`check_prefix`).
    keyed = cfg["mode"] == "pi-scripted"
    turns: dict[str, list[ScriptTurn]] = {role: [] for role in ROLES}
    calls: set[str] = set()
    last_role = None
    for _, r in sorted(decisions, key=lambda d: d[1].metadata["request_index"]):
        if r.metadata["request_index"] >= resume:
            break
        if "message" not in r.outputs or r.metadata.get("stop_reason") in ("error", "aborted"):
            raise ForkError(
                f"request {r.metadata['request_index']} of {parent_run_id} got no usable "
                "response before the fork point; its prefix cannot be replayed"
            )
        if keyed and not r.metadata.get("replay_key"):
            raise ForkError(f"request {r.metadata['request_index']} has no replay key")
        message = store.get_json(r.outputs["message"])
        key = r.metadata["replay_key"] if keyed else None
        turns[r.metadata["role"]].append(_turn(message, key, r.usage.cost_usd))
        last_role = r.metadata["role"]
        if r.metadata["role"] == "agent":
            calls |= {b["id"] for b in message.get("content") or [] if b.get("type") == "toolCall"}
    prefix = Script.model_validate(
        {
            "source": f"fork:{parent_run_id}@{checkpoint}",
            **{ROLES[role]: t for role, t in turns.items()},
        }
    )
    return ForkPoint(
        parent=parent,
        checkpoint=checkpoint,
        cut={parent_run_id: cp.event_seq, **cp.ancestry_cut},
        resume_request=resume,
        prefix=prefix,
        tool_calls=frozenset(calls),
        last_role=last_role,
    )


def _tool_results(store: Store, run_id: str, calls: frozenset[str]) -> dict[str, str | None]:
    """tool call id -> its visible result digest (None if none was recorded), for `calls`."""
    out: dict[str, str | None] = {}
    for event in store.events(run_id):
        if event.type != "invocation.committed":
            continue
        r = store.receipt(event.payload["invocation_id"])
        call = r.metadata.get("tool_call_id")
        if call in calls and "tool_name" in r.metadata:
            out[call] = r.outputs.get("result")
    return out


def _work_root(store: Store, run: Run) -> Path:
    """The work dir a run was launched under (`<work>/pi/sessions` is in its spool.start)."""
    first = store.get_bytes(run.config["spool"]).split(b"\n", 1)[0]
    return Path(json.loads(first)["session_dir"]).parent.parent


def _normalized(data: bytes, roots: tuple[Path, ...]) -> bytes:
    for root in roots:
        for form in sorted({str(root.resolve()), str(root)}, key=len, reverse=True):
            data = data.replace(form.encode(), b"$WORK")
    return data


def check_prefix(store: Store, point: ForkPoint, child_run_id: str) -> None:
    """Raise `ReplayMismatch` unless every prefix tool call showed the child what it showed
    the parent (compared with each run's work dir replaced by `$WORK`)."""
    parent = _tool_results(store, point.parent.run_id, point.tool_calls)
    child = _tool_results(store, child_run_id, point.tool_calls)
    roots = (_work_root(store, point.parent), _work_root(store, store.run_record(child_run_id)))
    for call, want in sorted(parent.items()):
        got = child.get(call)
        same = (want is None) == (got is None) and (
            want is None
            or got == want
            or _normalized(store.get_bytes(want), roots) == _normalized(store.get_bytes(got), roots)
        )
        if call not in child or not same:
            raise ReplayMismatch(
                f"fork {child_run_id}: tool call {call} showed a different result than in "
                f"{point.parent.run_id}"
            )


def _policy(run: Run) -> str:
    """Who answered the agent's requests in `run`: a model, or the committed script."""
    cfg = run.config
    if cfg["mode"] == "pi-live":
        return f"{cfg['provider']}/{cfg['model']}"
    return f"script:{cfg['script']}"


def _broker_prefix(store: Store, point: ForkPoint) -> BrokerReplay:
    """The parent's jobs that the prefix's tool calls got, as the child's broker replays them."""
    cfg = point.parent.config
    journal = parse_journal(store.get_bytes(cfg["broker_journal"]), "parent broker journal")
    cited = _cited_jobs(store, point.parent.run_id)
    jobs = [
        e
        for e in journal
        if e.event == "reply"
        and e.status != "refused"
        and e.job_id in cited
        and e.tool_call_id in point.tool_calls
    ]
    outputs = {e.output_sha256: store.get_bytes(e.output_sha256) for e in jobs}
    return BrokerReplay.from_journal(jobs, outputs)


def _join(prefix: Script, continuation: Script) -> Script:
    if continuation.children:
        raise ForkError("a fork continuation cannot script RLM children")
    return Script.model_validate(
        {
            "source": f"{prefix.source}+{continuation.source}",
            "turns": (*prefix.turns, *continuation.turns),
            "reducer_turns": (*prefix.reducer_turns, *continuation.reducer_turns),
            "summary_turns": (*prefix.summary_turns, *continuation.summary_turns),
        }
    )


def _free(prefix: Script) -> Script:
    """`prefix` with no recorded costs: replaying it before a live continuation costs nothing."""
    return Script.model_validate(
        {
            "source": prefix.source,
            **{
                field: [t.model_copy(update={"usage_cost": None}) for t in getattr(prefix, field)]
                for field in ROLES.values()
            },
        }
    )


def fork_pi_run(
    store: Store,
    parent_run_id: str,
    checkpoint: str,
    case: LoadedCase | LoadedVteCase,
    *,
    new_run_id: str,
    continuation: Script | None = None,
    live: LiveSettings | None = None,
    reason: str,
    work_dir: Path,
    budget_treatment: BudgetTreatment | None = None,
    orca: OrcaSettings | None = None,
    cache_dir: Path | None = None,
    sandbox: PiSandbox | None = None,
    timeout_s: float = 300,
) -> PiRun:
    """Fork `parent_run_id` at `checkpoint`: replay its prefix, then continue with
    `continuation` (a script) or `live` (a real model; exactly one).

    The child stages the parent's own task (case, instruction, docs, ladder,
    variant, candidate) and must reproduce its task id.
    `budget_treatment` "inherit-remaining" (a script's default) keeps the
    parent's request and job caps (the prefix re-counts against them);
    "fresh-allowance" adds the parent's caps again on top of what the prefix
    uses. A live continuation is always a fresh allowance: its requests and
    cost are capped by `live`'s own explicit settings, on top of the prefix.
    A VTE parent needs `orca` settings on the same ORCA binary (or stand-in)
    the parent ran.
    """
    check_safe_id(new_run_id, "run id")
    if store.has_record("run", new_run_id):
        raise ForkError(f"run {new_run_id} already exists")
    if (continuation is None) == (live is None):
        raise ForkError("a fork continues with a script or a live model (exactly one)")
    if budget_treatment is None:
        budget_treatment = "fresh-allowance" if live is not None else "inherit-remaining"
    if live is not None and budget_treatment != "fresh-allowance":
        raise ForkError("a live continuation has its own explicit caps: a fresh allowance")
    if budget_treatment not in ("inherit-remaining", "fresh-allowance"):
        raise ForkError(f"unknown budget treatment {budget_treatment!r}")
    if not reason.strip():
        raise ForkError("a fork needs a reason")
    point = fork_point(store, parent_run_id, checkpoint)
    cfg = point.parent.config
    if cfg["case_id"] != case.manifest.case_id:
        raise ForkError(f"run {parent_run_id} was recorded from case {cfg['case_id']!r}")
    vte = cfg.get("task_kind") == "vte"
    if vte != isinstance(case, LoadedVteCase):
        raise ForkError(f"run {parent_run_id} is {'a' if vte else 'not a'} VTE run")
    if live is not None:
        if point.last_role == "compaction-summary":
            raise ForkError(
                f"the prefix of {parent_run_id} ends with a compaction summary; a live "
                "continuation hands off at a turn end, so fork at an earlier checkpoint"
            )
        script = _free(point.prefix)
        to_policy = f"{live.provider}/{live.model}"
    else:
        assert continuation is not None
        script = _join(point.prefix, continuation)
        script_digest = store.put_json(
            continuation.model_dump(mode="json", by_alias=True), labels=["pi-script", "synthetic"]
        ).digest
        to_policy = f"script:{script_digest}"
    intervention = {
        "kind": "decision-policy-change",
        "from_policy": _policy(point.parent),
        "to_policy": to_policy,
    }
    declaration = ForkDeclaration.new(
        new_run_id=new_run_id,
        parent=ParentRef(run_id=parent_run_id, checkpoint=checkpoint),
        intervention=intervention,
        invalidated=[],
        visibility_cut=point.cut,
        budget_treatment=budget_treatment,
        context_translation="replay-prefix",
        reason=reason,
    )
    fresh = budget_treatment == "fresh-allowance"
    fork_config: dict[str, Any] = {
        "schema": FORK_SCHEMA,
        "parent": parent_run_id,
        "checkpoint": checkpoint,
        "resume_request": point.resume_request,
        "prefix": point.counts(),
    }
    if live is not None:
        if not live.max_requests or live.max_requests <= 0:
            live.validate()  # names every missing setting
        # The live model's own cap, on top of the replayed prefix.
        live = dataclasses.replace(live, max_requests=live.max_requests + point.resume_request)
        max_requests = live.max_requests
        fork_config["handoff"] = {"provider": live.provider, "model": live.model}
    else:
        max_requests = cfg["max_requests"] + (point.resume_request if fresh else 0)
        fork_config["continuation"] = script_digest
    staging = recorded_staging(store, parent_run_id)
    # The continuation is scripted, so its reducer is the scripted one, as in any scripted run.
    reducer = None
    common: dict[str, Any] = {
        "run_id": new_run_id,
        "variant_id": cfg["variant"],
        "work_dir": Path(work_dir),
        "script": script,
        "live": live,
        "max_requests": max_requests,
        "reducer": reducer,
        "timeout_s": timeout_s,
        "sandbox": sandbox,
        **staging,
    }
    if vte:
        if orca is None:
            raise ForkError(f"run {parent_run_id} is a VTE run; its fork needs ORCA settings")
        prefix_jobs = _broker_prefix(store, point)
        served = sum(len(q) for q in prefix_jobs.jobs.values())
        fork_config["prefix"]["jobs"] = served
        launch = ForkLaunch(declaration, fork_config, point.parent.task, prefix_jobs)
        result = run_vte_case(
            store,
            case,
            orca=orca,
            cache_dir=cache_dir,
            max_executions=cfg["max_executions"] + (served if fresh else 0),
            fork=launch,
            **common,
        )
    else:
        candidate = (
            HarnessCandidate(components=store.get_json(cfg["candidate"]))
            if "candidate" in cfg
            else None
        )
        launch = ForkLaunch(declaration, fork_config, point.parent.task)
        result = run_case(
            store,
            case,
            candidate=candidate,
            max_acquisitions=cfg.get("max_acquisitions", 1),
            fork=launch,
            **common,
        )
    ends = [e for e in store.events(new_run_id) if e.type == "pi.spool-end"]
    if ends and ends[-1].payload["status"] == "replay-mismatch":
        raise ReplayMismatch(
            f"fork {new_run_id} diverged from {parent_run_id} before the fork point; "
            "it is recorded, but it does not continue the parent"
        )
    check_prefix(store, point, new_run_id)
    return result
