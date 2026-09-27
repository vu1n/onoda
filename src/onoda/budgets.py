"""Budget admission and branch cost accounting (PORTABLE_RUN_SPEC §9).

Reservations and settlements are journal events ("budget.reserve",
"budget.settle"), so the ledger is portable and replayable. A quota is shared
by every run that names the same `quota.id` in its config; forks keep their
parent's quota, so creating branches cannot evade an experiment-level cap.

Cost views over receipts:
- marginal: this run's own receipts (reuse receipts excluded)
- inherited: the ancestral receipts in the run's starting checkpoint frontier
- full_path: inherited + marginal
- physical: unique invocations across a set of runs, counted once each

Monetary totals are decimal strings. Any receipt with unknown cost makes the
total unknown (None) and is counted in `unknown_cost_invocations`.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from onoda.errors import BudgetExceeded, IntegrityError
from onoda.records import Checkpoint, Event, InvocationReceipt, Run
from onoda.store import Store


def _receipts(store: Store, invocation_ids: Iterable[str]) -> list[InvocationReceipt]:
    return [store.receipt(i) for i in invocation_ids]


def summarize(receipts: Iterable[InvocationReceipt]) -> dict[str, Any]:
    units: dict[str, int] = {}
    total = Decimal(0)
    unknown = 0
    count = 0
    for rec in receipts:
        count += 1
        for name, n in rec.usage.units.items():
            units[name] = units.get(name, 0) + n
        if rec.usage.cost_usd is None:
            unknown += 1
        else:
            total += Decimal(rec.usage.cost_usd)
    return {
        "invocations": count,
        "units": dict(sorted(units.items())),
        "cost_usd": None if unknown else str(total),
        "known_cost_usd": str(total),
        "unknown_cost_invocations": unknown,
        "simulated": True,
    }


def own_invocation_ids(store: Store, run_id: str) -> list[str]:
    return [
        e.payload["invocation_id"] for e in store.events(run_id) if e.type == "invocation.committed"
    ]


def starting_checkpoint(store: Store, run: Run) -> Checkpoint | None:
    if run.parent is None:
        return None
    for e in store.events(run.run_id):
        if e.type == "checkpoint":
            return store.checkpoint(e.payload_refs["checkpoint"].split("/", 1)[1])
    return None


def inherited_invocation_ids(store: Store, run_id: str) -> list[str]:
    cp = starting_checkpoint(store, store.run_record(run_id))
    if cp is None:
        return []
    own = set(own_invocation_ids(store, run_id))
    return [i for i in cp.frontier if i not in own]


def child_links(store: Store, run_id: str) -> list[tuple[int, str | None]]:
    """(child index, child run id or None) from the run's `rlm.child` events, by index.

    The one reader of `rlm.child` links. A child whose spool was never written
    has no run (None). Ingest numbers children 0..n-1, so a gap means the
    store was altered.
    """
    found = sorted(
        (e.payload["child_index"], e.payload["child_run"])
        for e in store.events(run_id)
        if e.type == "rlm.child"
    )
    if [index for index, _ in found] != list(range(len(found))):
        raise IntegrityError(f"run {run_id} records child indices {[i for i, _ in found]}")
    return found


def descendant_runs(store: Store, run_id: str) -> list[str]:
    """Every RLM child run under `run_id`, breadth first, each once."""
    found: list[str] = []
    seen = {run_id}
    pending = [run_id]
    while pending:
        for _, child in child_links(store, pending.pop(0)):
            if child is not None and child not in seen:
                seen.add(child)
                found.append(child)
                pending.append(child)
    return found


def descendant_receipts(
    store: Store, run_id: str, *, exclude: Iterable[str] = ()
) -> list[InvocationReceipt]:
    """Every descendant run's own non-reuse receipts, each once, minus `exclude` ids.

    What the parent pays for its children (A44), for cost views and ablation
    metrics alike.
    """
    seen = set(exclude)
    out = []
    for child in descendant_runs(store, run_id):
        for rec in _receipts(store, own_invocation_ids(store, child)):
            if rec.invocation_kind != "reuse" and rec.invocation_id not in seen:
                seen.add(rec.invocation_id)
                out.append(rec)
    return out


def cost_views(store: Store, run_id: str) -> dict[str, Any]:
    """Cost views of one run; a Pi RLM run's marginal cost includes its child runs.

    An `rlm()` child is ingested as its own run and linked by the parent's
    `rlm.child` event (A44: the parent pays). Every descendant's own
    receipts are charged to the ancestor's marginal and full-path views,
    each counted once, and listed under `child_runs`/`children`; a run
    without children gets exactly the pre-M4 views.
    """
    own = _receipts(store, own_invocation_ids(store, run_id))
    marginal = [r for r in own if r.invocation_kind != "reuse"]
    inherited = _receipts(store, inherited_invocation_ids(store, run_id))
    child_runs = descendant_runs(store, run_id)
    children = descendant_receipts(store, run_id, exclude=(r.invocation_id for r in marginal))
    views: dict[str, Any] = {
        "run_id": run_id,
        "marginal": summarize([*marginal, *children]),
        "inherited_prefix": summarize(inherited),
        "full_path": summarize([*inherited, *marginal, *children]),
        "reused_receipts": [r.invocation_id for r in own if r.invocation_kind == "reuse"],
    }
    if child_runs:
        views["child_runs"] = child_runs
        views["children"] = summarize(children)
    return views


def physical_cost(store: Store, run_ids: Iterable[str]) -> dict[str, Any]:
    """Actual spending: each executed invocation once, however many branches share it."""
    seen: dict[str, InvocationReceipt] = {}
    for run_id in run_ids:
        ids = own_invocation_ids(store, run_id) + inherited_invocation_ids(store, run_id)
        for rec in _receipts(store, ids):
            if rec.invocation_kind != "reuse":
                seen.setdefault(rec.invocation_id, rec)
    return summarize(seen.values())


class Ledger:
    """Admission control for one run against its own and its quota's limits."""

    def __init__(self, store: Store, run: Run):
        self.store = store
        self.run = run
        quota = run.config.get("quota", {})
        self.quota_id: str = quota.get("id", run.quota_id)
        self.quota_caps: dict[str, int] = quota.get("caps", {})
        self.run_caps: dict[str, int] = run.config.get("limits", {})

    def _quota_runs(self) -> list[str]:
        runs = [
            run_id
            for run_id in self.store.list_record_ids("run")
            if self.store.run_record(run_id).quota_id == self.quota_id
        ]
        if self.run.run_id not in runs:
            runs.append(self.run.run_id)
        return runs

    def _run_totals(self, run_id: str) -> dict[str, int]:
        """Reserved-and-unsettled plus settled units in one run's own journal."""
        totals: dict[str, int] = {}
        open_res: dict[str, dict[str, int]] = {}
        for e in self.store.events(run_id):
            if e.type == "budget.reserve":
                open_res[e.payload["reservation"]] = e.payload["units"]
            elif e.type == "budget.settle":
                open_res.pop(e.payload["reservation"], None)
                for k, v in e.payload["units"].items():
                    totals[k] = totals.get(k, 0) + v
        for units in open_res.values():
            for k, v in units.items():
                totals[k] = totals.get(k, 0) + v
        return totals

    def inherited_units(self) -> dict[str, int]:
        return summarize(
            _receipts(self.store, inherited_invocation_ids(self.store, self.run.run_id))
        )["units"]

    def snapshot(self) -> dict[str, Any]:
        own = self._run_totals(self.run.run_id)
        inherited = self.inherited_units()
        treatment = self.run.config.get("budget_treatment", "inherit-remaining")
        return {
            "quota_id": self.quota_id,
            "quota_caps": self.quota_caps,
            "run_caps": self.run_caps,
            "treatment": treatment,
            "own": own,
            "inherited": inherited,
        }

    def reserve(
        self, reservation: str, units: dict[str, int], *, causes: Iterable[str] = ()
    ) -> Event:
        snap = self.snapshot()
        charged_inherited = snap["inherited"] if snap["treatment"] == "inherit-remaining" else {}
        quota_totals: list[dict[str, int]] | None = None
        for name, n in units.items():
            cap = self.run_caps.get(name)
            used = snap["own"].get(name, 0) + charged_inherited.get(name, 0)
            if cap is not None and used + n > cap:
                raise BudgetExceeded(f"run {self.run.run_id}: {name} {used}+{n} > {cap}")
            qcap = self.quota_caps.get(name)
            if qcap is not None:
                if quota_totals is None:
                    quota_totals = [self._run_totals(r) for r in self._quota_runs()]
                qused = sum(t.get(name, 0) for t in quota_totals)
                if qused + n > qcap:
                    raise BudgetExceeded(f"quota {self.quota_id}: {name} {qused}+{n} > {qcap}")
        return self.store.append_event(
            self.run.run_id,
            actor="budget",
            type="budget.reserve",
            payload={"reservation": reservation, "units": units},
            causes=causes,
        )

    def settle(
        self, reservation: str, units: dict[str, int], *, causes: Iterable[str] = ()
    ) -> Event:
        return self.store.append_event(
            self.run.run_id,
            actor="budget",
            type="budget.settle",
            payload={"reservation": reservation, "units": units},
            causes=causes,
        )

    def is_open(self, reservation: str) -> bool:
        state = None
        for e in self.store.events(self.run.run_id):
            if e.payload.get("reservation") == reservation:
                state = e.type
        return state == "budget.reserve"
