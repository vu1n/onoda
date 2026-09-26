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

from labrun.errors import BudgetExceeded
from labrun.records import Checkpoint, InvocationReceipt, Run
from labrun.store import Store


def _receipts(store: Store, invocation_ids: Iterable[str]) -> list[InvocationReceipt]:
    out = []
    for inv_id in invocation_ids:
        rec = store.get_record("invocation", inv_id)
        assert isinstance(rec, InvocationReceipt)
        out.append(rec)
    return out


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
            cp = store.get_record("checkpoint", e.payload_refs["checkpoint"].split("/", 1)[1])
            assert isinstance(cp, Checkpoint)
            return cp
    return None


def inherited_invocation_ids(store: Store, run_id: str) -> list[str]:
    run = store.get_record("run", run_id)
    assert isinstance(run, Run)
    cp = starting_checkpoint(store, run)
    if cp is None:
        return []
    own = set(own_invocation_ids(store, run_id))
    return [i for i in cp.frontier if i not in own]


def cost_views(store: Store, run_id: str) -> dict[str, Any]:
    own = [r for r in _receipts(store, own_invocation_ids(store, run_id))]
    marginal = [r for r in own if r.invocation_kind != "reuse"]
    inherited = _receipts(store, inherited_invocation_ids(store, run_id))
    return {
        "run_id": run_id,
        "marginal": summarize(marginal),
        "inherited_prefix": summarize(inherited),
        "full_path": summarize([*inherited, *marginal]),
        "reused_receipts": [r.invocation_id for r in own if r.invocation_kind == "reuse"],
    }


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
        runs = []
        for run_id in self.store.list_record_ids("run"):
            other = self.store.get_record("run", run_id)
            assert isinstance(other, Run)
            if other.quota_id == self.quota_id:
                runs.append(run_id)
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

    def reserve(self, reservation: str, units: dict[str, int], *, causes: Iterable[str] = ()):
        snap = self.snapshot()
        charged_inherited = snap["inherited"] if snap["treatment"] == "inherit-remaining" else {}
        for name, n in units.items():
            cap = self.run_caps.get(name)
            used = snap["own"].get(name, 0) + charged_inherited.get(name, 0)
            if cap is not None and used + n > cap:
                raise BudgetExceeded(f"run {self.run.run_id}: {name} {used}+{n} > {cap}")
            qcap = self.quota_caps.get(name)
            if qcap is not None:
                qused = sum(self._run_totals(r).get(name, 0) for r in self._quota_runs())
                if qused + n > qcap:
                    raise BudgetExceeded(f"quota {self.quota_id}: {name} {qused}+{n} > {qcap}")
        return self.store.append_event(
            self.run.run_id,
            actor="budget",
            type="budget.reserve",
            payload={"reservation": reservation, "units": units},
            causes=causes,
        )

    def settle(self, reservation: str, units: dict[str, int], *, causes: Iterable[str] = ()):
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
