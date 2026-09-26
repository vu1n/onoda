"""Fork creation, invalidation and the child's visibility cut (PORTABLE_RUN_SPEC §7).

A fork is a new run whose first checkpoint is derived from a quiescent,
journaled parent checkpoint. Nothing in the parent is written: the child gets
its own Run, ForkDeclaration, "fork.created" event and first checkpoint, and
the HX-002 controller resumes it from there like any other run.

Invalidation: the changed nodes are the frontier receipts whose step the
intervention touches (decide:* for a policy, parse:* for a parser, calc:rN for
a calc config, everything for a task input). Their descendants are receipts
that consume a changed output, list a changed receipt as parent, or come after
an invalidated decision (the decision chose that path). Invalidated outputs
leave the child's visible state and frontier; the child restarts at the
earliest invalidated step.

`RunView` is the only read API a branch should use: it exposes the run's own
journal closure plus each ancestor's journal closure up to the cut, and
refuses everything else, including direct content-hash lookups.
"""

from __future__ import annotations

from typing import Any

from labrun.canonical import digest_of, is_digest
from labrun.controller import Controller, RunState
from labrun.errors import ForkError, LabrunError, MissingObjectError, VisibilityError
from labrun.fixture import PARSERS, POLICIES
from labrun.records import (
    Boundary,
    Checkpoint,
    Evaluation,
    ForkDeclaration,
    InvocationReceipt,
    ParentRef,
    Record,
    Run,
    StepSpec,
    TaskManifest,
)
from labrun.store import Store, check_safe_id, record_ref

TREATMENTS = ("inherit-remaining", "fresh-allowance")
PHASE_ORDER = {"calc": 0, "parse": 1, "decide": 2}
LEVELS = ("coarse", "fine")

# kind -> (required key, optional keys, run label)
INTERVENTIONS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "decision-policy-change": ("to_policy", ("from_policy",), "policy-intervention"),
    "parser-change": ("to_parser", ("from_parser",), "parser-intervention"),
    "task-input-change": ("params", (), "task-input-intervention"),
    "calc-config-change": ("level", ("round",), "calc-config-intervention"),
    "memory-change": ("to_release", ("from_release",), "memory-intervention"),
}
FROM_KEYS = {"from_policy": "policy", "from_parser": "parser", "from_release": "memory_release"}
FORBIDDEN_TRUE = {
    "inheritance": ("copy_original_future", "reuse_changed_node_outputs"),
    "execution": ("allow_live_execution",),
}


def fork_run(
    controller: Controller,
    parent_run_id: str,
    checkpoint_digest: str,
    *,
    new_run_id: str,
    intervention: dict[str, Any],
    reason: str,
    budget_treatment: str = "inherit-remaining",
    declaration: dict[str, Any] | None = None,
) -> RunState:
    """Create a child run from a quiescent parent checkpoint. The parent is never written."""
    store = controller.store
    check_safe_id(new_run_id, "run id")
    if store.has_record("run", new_run_id) or store.events(new_run_id):
        raise ForkError(f"run {new_run_id} already exists")
    if not store.has_record("run", parent_run_id):
        raise ForkError(f"unknown parent run {parent_run_id}")
    parent = controller.run_record(parent_run_id)
    found = [cp for _, d, cp in controller.checkpoints(parent_run_id) if d == checkpoint_digest]
    if not found:
        raise ForkError(f"checkpoint {checkpoint_digest} is not journaled in {parent_run_id}")
    cp = found[0]
    if not cp.quiescent:
        raise ForkError(f"checkpoint {checkpoint_digest} is not quiescent")
    if budget_treatment not in TREATMENTS:
        raise ForkError(f"unknown budget treatment {budget_treatment!r}")
    kind = _check_intervention(store, parent, cp, intervention)
    if declaration is not None:
        _check_declaration(
            declaration, parent, checkpoint_digest, cp, new_run_id, intervention, budget_treatment
        )

    receipts = [store.receipt(i) for i in cp.frontier]
    steps = _step_keys(store, receipts)
    invalid = _invalidated(kind, intervention, receipts, steps)
    config = {**parent.config, "budget_treatment": budget_treatment}
    task_id = parent.task
    if kind == "task-input-change":
        pdesc = store.put_json(intervention["params"], labels=["synthetic", "task-input"])
        old = store.get_record("task", parent.task)
        assert isinstance(old, TaskManifest)
        task = old.model_copy(
            update={
                "task_id": "fixture-scan-" + pdesc.digest[7:19],
                "inputs": {"params": pdesc.digest},
            }
        )
        store.put_record(task)
        task_id = task.task_id
        visible: dict[str, str] = {"params": pdesc.digest}
        frontier: list[str] = []
        phase, rnd = "calc", 1
    else:
        invalid_out = {
            d for r in receipts if r.invocation_id in invalid for d in r.outputs.values()
        }
        visible = {
            n: d
            for n, d in cp.visible.items()
            if d not in invalid_out and not (n == "final" and invalid)
        }
        frontier = [i for i in cp.frontier if i not in invalid]
        if invalid:
            phase, rnd = min(
                (_parse_step(steps[i]) for i in invalid), key=lambda s: (s[1], PHASE_ORDER[s[0]])
            )
        else:
            phase, rnd = cp.phase, cp.context.get("round", 1)
        if kind == "decision-policy-change":
            config["policy"] = intervention["to_policy"]
        elif kind == "parser-change":
            config["parser"] = intervention["to_parser"]
        elif kind == "memory-change":
            config["memory_release"] = intervention["to_release"]
        elif kind == "calc-config-change":
            config["calc_overrides"] = {f"r{rnd}": intervention["level"]}

    labels = {"synthetic", "forked", INTERVENTIONS[kind][2]}
    labels |= set(((declaration or {}).get("report") or {}).get("required_labels") or [])
    cut = {parent_run_id: cp.event_seq, **cp.ancestry_cut}
    decl = ForkDeclaration.new(
        new_run_id=new_run_id,
        parent=ParentRef(run_id=parent_run_id, checkpoint=checkpoint_digest),
        intervention=intervention,
        invalidated=[i for i in cp.frontier if i in invalid],
        visibility_cut=cut,
        budget_treatment=budget_treatment,
        reason=reason,
    )
    decl_digest = store.put_record(decl)
    run = Run.new(
        run_id=new_run_id,
        experiment_id=parent.experiment_id,
        task=task_id,
        config=config,
        parent=decl.parent,
        fork=decl_digest,
        quota_id=parent.quota_id,
        created_at=store.clock.now(),
        labels=sorted(labels),
    )
    store.put_record(run)
    refs = {
        "run": record_ref("run", new_run_id),
        "fork": record_ref("fork", decl_digest),
        "parent_checkpoint": record_ref("checkpoint", checkpoint_digest),
        "task": record_ref("task", task_id),
    }
    if declaration is not None:
        refs["declaration"] = store.put_json(declaration, labels=["fork-declaration"]).digest
    store.append_event(
        new_run_id,
        actor="controller",
        type="fork.created",
        payload={"intervention": kind, "invalidated": decl.invalidated},
        payload_refs=refs,
    )
    boundary = cp.boundary if not invalid else _boundary_before(phase, rnd)
    return controller.write_checkpoint(run, boundary, phase, rnd, visible, frontier, cut)


def _boundary_before(phase: str, rnd: int) -> Boundary:
    """The boundary a controller checkpoint would carry when entering `phase`."""
    if phase == "calc":
        return "initialized" if rnd == 1 else "decision-committed-before-submission"
    return {"parse": "job-collected", "decide": "diagnostics-committed"}[phase]


# ------------------------------------------------------------ validation


def _check_intervention(store: Store, parent: Run, cp: Checkpoint, iv: dict[str, Any]) -> str:
    kind = iv.get("kind")
    if "quota" in iv or "quota_id" in iv:
        raise ForkError("a fork cannot change its quota")
    if kind not in INTERVENTIONS:
        raise ForkError(f"unknown intervention kind {kind!r}")
    required, optional, _ = INTERVENTIONS[kind]
    extra = set(iv) - {"kind", required, *optional}
    if required not in iv or extra:
        raise ForkError(f"{kind} needs {required!r}; unexpected keys {sorted(extra)}")
    for key in optional:
        if key in FROM_KEYS and iv.get(key) is not None:
            current = parent.config.get(FROM_KEYS[key])
            if iv[key] != current:
                raise ForkError(f"{key}={iv[key]!r} but parent has {current!r}")
    if kind == "decision-policy-change" and iv["to_policy"] not in POLICIES:
        raise ForkError(f"unknown policy {iv['to_policy']!r}")
    if kind == "parser-change" and iv["to_parser"] not in PARSERS:
        raise ForkError(f"unknown parser {iv['to_parser']!r}")
    if kind == "task-input-change":
        task = store.get_record("task", parent.task)
        assert isinstance(task, TaskManifest)
        if not isinstance(iv["params"], dict) or digest_of(iv["params"]) == task.inputs["params"]:
            raise ForkError("task-input-change needs a params mapping that differs from the task")
    if kind == "memory-change":
        rel = iv["to_release"]
        if not (
            store.has_record("artifact", rel) and "memory-release" in store.descriptor(rel).labels
        ):
            raise ForkError(f"{rel} is not a committed memory release")
    if kind == "calc-config-change":
        _check_calc_change(store, cp, iv)
    return kind


def _check_calc_change(store: Store, cp: Checkpoint, iv: dict[str, Any]) -> None:
    level, rnd = iv["level"], iv.get("round", 1)
    if level not in LEVELS:
        raise ForkError(f"unknown calc level {level!r}")
    receipts = [store.receipt(i) for i in cp.frontier]
    steps = _step_keys(store, receipts)
    calc = [r for r in receipts if steps[r.invocation_id] == f"calc:r{rnd}"]
    if not calc:
        raise ForkError(f"checkpoint has no committed calc for round {rnd}")
    spec = store.get_record("step", calc[0].spec_digest)
    assert isinstance(spec, StepSpec)
    if spec.config.get("level") == level:
        raise ForkError(f"calc r{rnd} already ran at level {level!r}; nothing would change")


def _check_declaration(
    decl: dict[str, Any],
    parent: Run,
    checkpoint_digest: str,
    cp: Checkpoint,
    new_run_id: str,
    iv: dict[str, Any],
    treatment: str,
) -> None:
    """A declaration must name exactly this fork and must not ask for anything unsupported."""
    p = decl.get("parent") or {}
    checks = [
        (p.get("run_id") == parent.run_id, "parent.run_id"),
        (p.get("checkpoint_digest") == checkpoint_digest, "parent.checkpoint_digest"),
        (decl.get("new_run_id") in (None, new_run_id), "new_run_id"),
        (p.get("required_boundary") in (None, cp.boundary), "parent.required_boundary"),
        (p.get("manifest_digest") in (None, parent.record_digest()), "parent.manifest_digest"),
    ]
    div = decl.get("intervention") or {}
    checks.append((div.get("kind") == iv["kind"], "intervention.kind"))
    for key, value in div.items():
        if key in iv and value is not None and value != iv[key]:
            checks.append((False, f"intervention.{key}"))
        if key in FROM_KEYS and value is not None:
            checks.append((value == parent.config.get(FROM_KEYS[key]), f"intervention.{key}"))
    inh = decl.get("inheritance") or {}
    ex = decl.get("execution") or {}
    bud = decl.get("budgets") or {}
    checks += [
        (inh.get("require_quiescent_checkpoint") is not False, "require_quiescent_checkpoint"),
        (ex.get("mode") in (None, "fixture"), "execution.mode"),
        (bud.get("allowance") in (None, treatment), "budgets.allowance"),
        (bud.get("retain_parent_quota") is not False, "budgets.retain_parent_quota"),
    ]
    for section, keys in FORBIDDEN_TRUE.items():
        for key in keys:
            checks.append(((decl.get(section) or {}).get(key) is not True, f"{section}.{key}"))
    bad = [name for ok, name in checks if not ok]
    if bad:
        raise ForkError(f"fork declaration disagrees or asks for unsupported behaviour: {bad}")


# ---------------------------------------------------------- invalidation


def _step_keys(store: Store, receipts: list[InvocationReceipt]) -> dict[str, str]:
    """invocation id -> step key, read from each producing run's own journal."""
    out: dict[str, str] = {}
    for run_id in {r.run_id for r in receipts}:
        for e in store.events(run_id):
            if e.type == "invocation.committed":
                out[e.payload["invocation_id"]] = e.payload["step_key"]
    missing = [r.invocation_id for r in receipts if r.invocation_id not in out]
    if missing:
        raise ForkError(f"frontier receipts without a committed step: {missing}")
    return out


def _parse_step(step_key: str) -> tuple[str, int]:
    phase, _, rnd = step_key.partition(":r")
    if phase not in PHASE_ORDER or not rnd.isdigit():
        raise ForkError(f"cannot fork across unknown step {step_key!r}")
    return phase, int(rnd)


def _invalidated(
    kind: str, iv: dict[str, Any], receipts: list[InvocationReceipt], steps: dict[str, str]
) -> set[str]:
    """Changed nodes plus every data, parent or control descendant among the frontier."""

    def changed(step: str) -> bool:
        if kind == "task-input-change":
            return True
        if kind == "decision-policy-change":
            return step.startswith("decide:")
        if kind == "parser-change":
            return step.startswith("parse:")
        if kind == "calc-config-change":
            return step == f"calc:r{iv.get('round', 1)}"
        return False

    def order(inv_id: str) -> tuple[int, int]:
        phase, rnd = _parse_step(steps[inv_id])
        return rnd, PHASE_ORDER[phase]

    invalid = {r.invocation_id for r in receipts if changed(steps[r.invocation_id])}
    grew = bool(invalid)
    while grew:
        grew = False
        outs = {d for r in receipts if r.invocation_id in invalid for d in r.outputs.values()}
        decisions = [order(i) for i in invalid if steps[i].startswith("decide:")]
        for r in receipts:
            if r.invocation_id in invalid:
                continue
            if (
                outs & set(r.inputs.values())
                or invalid & set(r.parents)
                or any(d < order(r.invocation_id) for d in decisions)
            ):
                invalid.add(r.invocation_id)
                grew = True
    return invalid


# ------------------------------------------------------------------ view


def _children(record: Record) -> list[str]:
    """Refs a committed record points at; all were committed before it."""
    if isinstance(record, Run):
        refs = [record_ref("task", record.task)]
        if record.parent:
            refs.append(record_ref("checkpoint", record.parent.checkpoint))
        if record.fork:
            refs.append(record_ref("fork", record.fork))
        return refs
    if isinstance(record, (TaskManifest, StepSpec)):
        return list(record.inputs.values())
    if isinstance(record, InvocationReceipt):
        refs = [*record.inputs.values(), *record.outputs.values()]
        refs.append(record_ref("step", record.spec_digest))
        refs += [record_ref("invocation", p) for p in record.parents]
        if record.derived_from:
            refs.append(record_ref("invocation", record.derived_from))
        return refs
    if isinstance(record, Checkpoint):
        refs = [*record.visible.values(), *(record_ref("invocation", i) for i in record.frontier)]
        return refs + ([record.memory_release] if record.memory_release else [])
    if isinstance(record, ForkDeclaration):
        return [record_ref("checkpoint", record.parent.checkpoint)]
    if isinstance(record, Evaluation):
        return list(record.output_refs.values())
    return []


class RunView:
    """What one run may see: its own journal closure plus each ancestor's up to the cut.

    The view is a snapshot taken at construction. Objects are leaves: a visible
    lesson does not make the evidence it cites visible.
    """

    def __init__(self, store: Store, run_id: str):
        self.store = store
        self.run_id = run_id
        own = store.events(run_id)
        if not own:
            raise LabrunError(f"run {run_id} has no journal")
        # The first checkpoint fixes the cut; later ones carry the same map forward.
        self.cut: dict[str, int] = {}
        for e in own:
            if e.type == "checkpoint":
                cp = store.checkpoint(e.payload_refs["checkpoint"].split("/", 1)[1])
                self.cut = dict(cp.ancestry_cut)
                break
        seeds = [ref for e in own for ref in e.payload_refs.values()]
        for anc, seq in self.cut.items():
            seeds += [
                ref for e in store.events(anc) if e.seq <= seq for ref in e.payload_refs.values()
            ]
        self.refs = self._closure(seeds)

    @property
    def lineage(self) -> set[str]:
        return {self.run_id, *self.cut}

    def _closure(self, seeds: list[str]) -> frozenset[str]:
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            ref = stack.pop()
            if ref in seen:
                continue
            seen.add(ref)
            if is_digest(ref):
                seen.add(record_ref("artifact", ref))
                continue
            kind, _, record_id = ref.partition("/")
            try:
                stack += _children(self.store.get_record(kind, record_id))
            except MissingObjectError:
                continue
        return frozenset(seen)

    def can_see(self, ref: str) -> bool:
        return ref in self.refs

    def check(self, ref: str) -> None:
        if ref not in self.refs:
            raise VisibilityError(f"{ref} is outside the view of run {self.run_id}")

    def resolve(self, digest: str) -> bytes:
        self.check(digest)
        return self.store.get_bytes(digest)

    def resolve_json(self, digest: str) -> Any:
        self.check(digest)
        return self.store.get_json(digest)

    def get_record(self, kind: str, record_id: str) -> Record:
        self.check(record_ref(kind, record_id))
        return self.store.get_record(kind, record_id)

    def search(self, label: str) -> list[str]:
        """Visible artifact digests carrying `label`, sorted."""
        return [
            digest
            for digest in self.store.list_record_ids("artifact")
            if digest in self.refs and label in self.store.descriptor(digest).labels
        ]
