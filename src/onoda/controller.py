"""Trusted controller: phase machine, checkpoints, crash-safe side effects.

A run's state is never held in memory between calls. `state()` rebuilds it
from the latest journaled checkpoint, and every phase is idempotent against
the journal: a step whose `invocation.committed` event exists is not rerun,
and a job whose intent exists is reconciled with the scheduler before any
resubmission (PORTABLE_RUN_SPEC §10). That is what makes `resume` and a
freshly forked run use the same code path.

Fixture workflow (one round per calculation level):

    initialized -> calc -> job-collected -> parse -> diagnostics-committed
    -> decide -> decision-committed-before-submission
    -> (refine: calc of round r+1) | finalize -> finalized

Checkpoints support semantic restart only; native session resume is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from onoda.budgets import Ledger
from onoda.canonical import digest_of
from onoda.decisions import DecisionProvider, RecordedDecisionProvider
from onoda.errors import CapabilityError, OnodaError, SubmissionUncertain
from onoda.fixture import DEFAULT_PARAMS, PARSERS, POLICIES, check_mapping, decision_request
from onoda.jobs import MockScheduler
from onoda.records import (
    Boundary,
    Checkpoint,
    Event,
    InvocationReceipt,
    Run,
    StepSpec,
    TaskManifest,
    Usage,
)
from onoda.store import Store, check_safe_id, record_ref

CAPABILITIES = {
    "inspect": "supported",
    "semantic_restart": "supported",
    "native_resume": "unsupported",
    "reexecute": "supported-fixture-only",
}
KNOWN_PROVIDER_KINDS = ("deterministic-mock", "offline-replay")
FIXTURE_PRICES = {"fixture-calc": "0.40", "fixture-sample": None, "parse": "0.00"}


@dataclass(frozen=True)
class RunState:
    run_id: str
    phase: str
    round: int
    boundary: Boundary
    checkpoint: str
    visible: dict[str, str]
    frontier: list[str]
    ancestry_cut: dict[str, int]
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.phase == "done"


class Controller:
    def __init__(
        self,
        store: Store,
        scheduler: MockScheduler,
        *,
        providers: dict[str, DecisionProvider] | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.scheduler = scheduler
        self.providers = providers or {}
        self.crash_hook = crash_hook

    # ------------------------------------------------------------ lifecycle

    def start_fixture_run(
        self,
        run_id: str,
        *,
        params: dict[str, Any] | None = None,
        policy: str = "mock-policy-v1",
        parser: str = "fixture-parser@1",
        decision_provider: str = "mock",
        max_rounds: int = 2,
        limits: dict[str, int] | None = None,
        quota: dict[str, Any] | None = None,
        experiment_id: str = "portable-fixture-demo",
        allow_live_execution: bool = False,
    ) -> RunState:
        check_safe_id(run_id, "run id")
        if allow_live_execution:
            raise CapabilityError("live execution is not implemented; only fixture mode runs")
        if self.store.has_record("run", run_id):
            raise OnodaError(f"run {run_id} already exists")
        params = params if params is not None else DEFAULT_PARAMS
        pdesc = self.store.put_json(params, labels=["synthetic", "task-input"])
        task = TaskManifest.new(
            task_id="fixture-scan-" + pdesc.digest[7:19],
            inputs={"params": pdesc.digest},
            permitted_context=["task-input"],
            split="development",
            target={"kind": "synthetic-min-point"},
            profile_versions={"fixture": "1"},
            synthetic=True,
        )
        self.store.put_record(task)
        quota = quota or {"id": f"{experiment_id}-quota", "caps": {"fixture_steps": 64}}
        run = Run.new(
            run_id=run_id,
            experiment_id=experiment_id,
            task=task.task_id,
            config={
                "mode": "fixture",
                "allow_live_execution": False,
                "policy": policy,
                "parser": parser,
                "decision_provider": decision_provider,
                "max_rounds": max_rounds,
                "limits": limits if limits is not None else {"fixture_steps": 24},
                "quota": quota,
                "budget_treatment": "inherit-remaining",
            },
            quota_id=quota["id"],
            created_at=self.store.clock.now(),
            labels=["synthetic"],
        )
        self.store.put_record(run)
        self.store.append_event(
            run_id,
            actor="controller",
            type="run.created",
            payload_refs={
                "run": record_ref("run", run_id),
                "task": record_ref("task", task.task_id),
            },
        )
        return self.write_checkpoint(
            run,
            "initialized",
            phase="calc",
            round=1,
            visible={"params": pdesc.digest},
            frontier=[],
            ancestry_cut={},
        )

    def run_fixture(self, run_id: str, **kwargs: Any) -> RunState:
        self.start_fixture_run(run_id, **kwargs)
        return self.advance(run_id)

    # Context: doc://onoda/record-integrity@0002#immutable-single-writer-records — semantic restart only; no native resume of live process state
    def resume(self, run_id: str, *, mode: str = "semantic") -> RunState:
        """Semantic restart from the latest checkpoint. Native resume is not supported."""
        if mode != "semantic":
            raise CapabilityError(
                f"{mode!r} resume unsupported: checkpoints hold semantic state only"
            )
        self.store.recover()
        latest = self.checkpoints(run_id)
        if latest and latest[-1][2].capabilities.get("semantic_restart") != "supported":
            raise CapabilityError(f"run {run_id}'s checkpoints do not support semantic restart")
        return self.advance(run_id)

    def advance(self, run_id: str, *, until: Boundary | None = None) -> RunState:
        st = self.state(run_id)
        while not st.done:
            st = self._step(st)
            if until is not None and st.boundary == until:
                break
        return st

    # ---------------------------------------------------------------- state

    def run_record(self, run_id: str) -> Run:
        return self.store.run_record(run_id)

    def checkpoints(self, run_id: str) -> list[tuple[int, str, Checkpoint]]:
        return self.store.checkpoints(run_id)

    def state(self, run_id: str) -> RunState:
        cps = self.checkpoints(run_id)
        if not cps:
            raise OnodaError(f"run {run_id} has no checkpoint")
        _, digest, cp = cps[-1]
        return RunState(
            run_id=run_id,
            phase=cp.phase,
            round=cp.context.get("round", 1),
            boundary=cp.boundary,
            checkpoint=digest,
            visible=dict(cp.visible),
            frontier=list(cp.frontier),
            ancestry_cut=dict(cp.ancestry_cut),
            config=self.run_record(run_id).config,
        )

    # ---------------------------------------------------------------- phases

    def _step(self, st: RunState) -> RunState:
        run = self.run_record(st.run_id)
        r = st.round
        if st.phase == "calc":
            default = "coarse" if r == 1 else "fine"
            level = run.config.get("calc_overrides", {}).get(f"r{r}", default)
            rec = self.run_job(
                run.run_id,
                f"calc:r{r}",
                "fixture-calc",
                {"params": st.visible["params"]},
                {"level": level},
            )
            visible = {
                **st.visible,
                f"log.r{r}": rec.outputs["log"],
                f"wavefunction.r{r}": rec.outputs["wavefunction"],
            }
            return self.write_checkpoint(
                run,
                "job-collected",
                "parse",
                r,
                visible,
                [*st.frontier, rec.invocation_id],
                st.ancestry_cut,
            )
        if st.phase == "parse":
            rec = self._parse(run, st)
            visible = {
                **st.visible,
                f"observation.r{r}": rec.outputs["observation"],
                f"mapping.r{r}": rec.outputs["mapping"],
            }
            return self.write_checkpoint(
                run,
                "diagnostics-committed",
                "decide",
                r,
                visible,
                [*st.frontier, rec.invocation_id],
                st.ancestry_cut,
            )
        if st.phase == "decide":
            rec = self._decide(run, st)
            decision = self.store.get_json(rec.outputs["decision"])
            refine = decision["action"] == "refine" and r < run.config["max_rounds"]
            visible = {**st.visible, f"decision.r{r}": rec.outputs["decision"]}
            return self.write_checkpoint(
                run,
                "decision-committed-before-submission",
                "calc" if refine else "finalize",
                r + 1 if refine else r,
                visible,
                [*st.frontier, rec.invocation_id],
                st.ancestry_cut,
            )
        if st.phase == "finalize":
            final = self.store.put_json(self._final_state(run, st), labels=["synthetic", "final"])
            visible = {**st.visible, "final": final.digest}
            return self.write_checkpoint(
                run, "finalized", "done", r, visible, st.frontier, st.ancestry_cut
            )
        raise OnodaError(f"run {st.run_id} is in unknown phase {st.phase!r}")

    def _final_state(self, run: Run, st: RunState) -> dict[str, Any]:
        decisions = [
            self.store.get_json(st.visible[f"decision.r{i}"])
            for i in range(1, st.round + 1)
            if f"decision.r{i}" in st.visible
        ]
        actions = [d["action"] or d["abstention"] for d in decisions]
        obs = self.store.get_json(st.visible[f"observation.r{st.round}"])
        return {
            "synthetic": True,
            "task": run.task,
            "rounds": st.round,
            "actions": actions,
            "final_observation": obs,
        }

    def _parse(self, run: Run, st: RunState) -> InvocationReceipt:
        r = st.round
        step_key = f"parse:r{r}"
        done = self._committed(run.run_id, step_key) or self._recover_orphan(run, step_key)
        if done:
            return done
        inv_id = self._begin_step(run, step_key)
        parser = run.config["parser"]
        inputs = {"log": st.visible[f"log.r{r}"], "wavefunction": st.visible[f"wavefunction.r{r}"]}
        spec = StepSpec.new(
            operation="parse",
            inputs=inputs,
            config={"parser": parser},
            code_ref=f"onoda.fixture:PARSERS[{parser}]",
            determinism="pure",
        )
        out = PARSERS[parser]({k: self.store.get_bytes(d) for k, d in inputs.items()})
        outputs = {
            name: self.store.put_json(value, labels=["synthetic", "parser-output"]).digest
            for name, value in out.items()
        }
        return self._commit_invocation(
            run,
            step_key,
            spec,
            "derivation",
            inputs,
            outputs,
            parents=[self._producer(st, inputs["log"])],
            metadata={"parser": parser},
            invocation_id=inv_id,
            usage=Usage(
                units={"fixture_steps": 1},
                cost_usd=FIXTURE_PRICES["parse"],
                price_table="fixture-prices@1",
            ),
        )

    def _decide(self, run: Run, st: RunState) -> InvocationReceipt:
        r = st.round
        step_key = f"decide:r{r}"
        done = self._committed(run.run_id, step_key) or self._recover_orphan(run, step_key)
        if done:
            return done
        inv_id = self._begin_step(run, step_key)
        mapping = self.store.get_json(st.visible[f"mapping.r{r}"])
        check_mapping(mapping, st.visible[f"wavefunction.r{r}"])
        evidence_refs = {
            "observation": st.visible[f"observation.r{r}"],
            "mapping": st.visible[f"mapping.r{r}"],
        }
        request = decision_request(evidence_refs, run.config["policy"])
        req = self.store.put_json(request.to_json(), labels=["decision-request"])
        provider = self._provider(run)
        ledger = Ledger(self.store, run)
        reservation = f"{run.run_id}:{step_key}"
        if not ledger.is_open(reservation):
            ledger.reserve(reservation, {"fixture_steps": 1})
        result = provider.decide(
            request, {k: self.store.get_json(d) for k, d in evidence_refs.items()}
        )
        raw = self.store.put_bytes(
            result.raw_response, media_type="application/json", labels=["decision-response"]
        )
        decision = self.store.put_json(
            {**result.normalized(), "request_digest": request.digest()}, labels=["decision"]
        )
        spec = StepSpec.new(
            operation="decide",
            inputs={"request": req.digest, **evidence_refs},
            config={"policy": run.config["policy"], "provider": provider.name},
            code_ref=f"onoda.decisions:{provider.name}",
            determinism="pure",
        )
        # A replayed answer is reuse of the original receipt, not a new decision.
        kind = "reuse" if result.replayed_from else "decision"
        return self._commit_invocation(
            run,
            step_key,
            spec,
            kind,
            {"request": req.digest, **evidence_refs},
            {"decision": decision.digest, "raw_response": raw.digest},
            parents=[self._producer(st, evidence_refs["observation"])],
            metadata={
                "request_digest": request.digest(),
                "provider": result.provider,
                "replayed_from": result.replayed_from,
                "provider_state": "known"
                if result.provider.get("kind") in KNOWN_PROVIDER_KINDS
                else "unknown",
            },
            usage=result.usage.model_copy(
                update={"units": {**result.usage.units, "fixture_steps": 1}}
            ),
            invocation_id=inv_id,
            reservation=reservation,
        )

    def _provider(self, run: Run) -> DecisionProvider:
        name = run.config["decision_provider"]
        if name in self.providers:
            return self.providers[name]
        if name == "mock":
            return POLICIES[run.config["policy"]]
        if name == "recorded":
            return RecordedDecisionProvider(self.store)
        raise CapabilityError(f"decision provider {name!r} is not available offline")

    # ---------------------------------------------------------- side effects

    def run_job(
        self,
        run_id: str,
        step_key: str,
        program: str,
        inputs: dict[str, str],
        config: dict[str, Any],
    ) -> InvocationReceipt:
        """Submit, reconcile and collect one job exactly once per (run, step_key)."""
        run = self.run_record(run_id)
        done = self._committed(run_id, step_key)
        if done:
            return done
        key = f"{run_id}:{step_key}"
        events = self.store.events(run_id)
        intent = _find(events, "job.intent", step_key=step_key)
        resuming = intent is not None
        if intent is None:
            spec = StepSpec.new(
                operation=f"job:{program}",
                inputs=inputs,
                config=config,
                code_ref=f"onoda.fixture:PROGRAMS[{program}]",
                determinism="external",
            )
            spec_id = self.store.put_record(spec)
            inv_id = self._next_invocation_id(run_id)
            Ledger(self.store, run).reserve(key, {"fixture_steps": 1})
            intent = self.store.append_event(
                run_id,
                actor="controller",
                type="job.intent",
                payload={
                    "step_key": step_key,
                    "key": key,
                    "invocation_id": inv_id,
                    "program": program,
                },
                payload_refs={"spec": record_ref("step", spec_id)},
            )
        submitted = _find(self.store.events(run_id), "job.submitted", key=key)
        if submitted is None:
            job_id = None
            if resuming:
                try:
                    job_id = self.scheduler.lookup(key)
                except SubmissionUncertain:
                    self.store.append_event(
                        run_id,
                        actor="controller",
                        type="job.submission-uncertain",
                        payload={"key": key},
                        causes=[intent.event_id],
                    )
                    raise
            reconciled = job_id is not None
            if job_id is None:
                job_id = self.scheduler.submit(
                    key, program, {k: self.store.get_bytes(d) for k, d in inputs.items()}, config
                )
            submitted = self.store.append_event(
                run_id,
                actor="gateway",
                type="job.submitted",
                payload={"key": key, "job_id": job_id, "reconciled": reconciled},
                causes=[intent.event_id],
            )
        job_id = submitted.payload["job_id"]
        outputs = {
            name: self.store.put_bytes(
                data, media_type="text/plain", labels=["synthetic", "job-output"]
            ).digest
            for name, data in sorted(self.scheduler.collect(job_id).items())
        }
        if self.crash_hook:
            self.crash_hook("job.outputs-committed")
        spec_id = intent.payload_refs["spec"].split("/", 1)[1]
        spec = self.store.get_record("step", spec_id)
        assert isinstance(spec, StepSpec)
        return self._commit_invocation(
            run,
            step_key,
            spec,
            "execution",
            inputs,
            outputs,
            parents=[],
            metadata={
                "job_id": job_id,
                "idempotency_key": key,
                "program": program,
                "scheduler": "mock",
            },
            usage=Usage(
                units={"fixture_steps": 1},
                cost_usd=FIXTURE_PRICES.get(program),
                price_table="fixture-prices@1",
            ),
            invocation_id=intent.payload["invocation_id"],
            reservation=key,
        )

    def _commit_invocation(
        self,
        run: Run,
        step_key: str,
        spec: StepSpec,
        kind: str,
        inputs: dict[str, str],
        outputs: dict[str, str],
        *,
        parents: list[str | None],
        metadata: dict[str, Any],
        usage: Usage,
        invocation_id: str | None = None,
        reservation: str | None = None,
    ) -> InvocationReceipt:
        spec_id = self.store.put_record(spec)
        ledger = Ledger(self.store, run)
        if reservation is None:
            reservation = f"{run.run_id}:{step_key}"
            if not ledger.is_open(reservation):
                ledger.reserve(reservation, {"fixture_steps": 1})
        inv_id = invocation_id or self._next_invocation_id(run.run_id)
        if self.store.has_record("invocation", inv_id):
            receipt = self.store.receipt(inv_id)  # committed before a crash
        else:
            now = self.store.clock.now()
            receipt = InvocationReceipt.new(
                invocation_id=inv_id,
                run_id=run.run_id,
                spec_digest=spec_id,
                invocation_kind=kind,
                inputs=inputs,
                outputs=outputs,
                parents=[p for p in parents if p],
                status="succeeded",
                metadata=metadata,
                usage=usage,
                started_at=now,
                finished_at=now,
                labels=["synthetic", "fixture"],
            )
            self.store.put_record(receipt)
        self._journal_commit(run, step_key, inv_id, reservation)
        return receipt

    def _journal_commit(self, run: Run, step_key: str, inv_id: str, reservation: str) -> None:
        self.store.append_event(
            run.run_id,
            actor="controller",
            type="invocation.committed",
            payload={"invocation_id": inv_id, "step_key": step_key},
            payload_refs={"receipt": record_ref("invocation", inv_id)},
        )
        Ledger(self.store, run).settle(reservation, {"fixture_steps": 1})

    def _begin_step(self, run: Run, step_key: str) -> str:
        """Allocate a step's invocation id durably before any work, once per step."""
        intent = _find(self.store.events(run.run_id), "step.intent", step_key=step_key)
        if intent is not None:
            return intent.payload["invocation_id"]
        inv_id = self._next_invocation_id(run.run_id)
        self.store.append_event(
            run.run_id,
            actor="controller",
            type="step.intent",
            payload={"step_key": step_key, "invocation_id": inv_id},
        )
        return inv_id

    def _recover_orphan(self, run: Run, step_key: str) -> InvocationReceipt | None:
        """A receipt committed just before a crash is journaled, not recomputed."""
        intent = _find(self.store.events(run.run_id), "step.intent", step_key=step_key)
        if intent is None or not self.store.has_record(
            "invocation", intent.payload["invocation_id"]
        ):
            return None
        inv_id = intent.payload["invocation_id"]
        self._journal_commit(run, step_key, inv_id, f"{run.run_id}:{step_key}")
        return self.store.receipt(inv_id)

    def _committed(self, run_id: str, step_key: str) -> InvocationReceipt | None:
        ev = _find(self.store.events(run_id), "invocation.committed", step_key=step_key)
        if ev is None:
            return None
        return self.store.receipt(ev.payload["invocation_id"])

    def _producer(self, st: RunState, digest: str) -> str | None:
        for inv_id in reversed(st.frontier):
            if digest in self.store.receipt(inv_id).outputs.values():
                return inv_id
        return None

    def _next_invocation_id(self, run_id: str) -> str:
        prefix = f"{run_id}.i"
        used = {i for i in self.store.list_record_ids("invocation") if i.startswith(prefix)}
        used |= {
            e.payload["invocation_id"]
            for e in self.store.events(run_id)
            if e.type in ("job.intent", "step.intent")
        }
        numbers = [int(i[len(prefix) :]) for i in used if i[len(prefix) :].isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1:03d}"

    def write_checkpoint(
        self,
        run: Run,
        boundary: Boundary,
        phase: str,
        round: int,
        visible: dict[str, str],
        frontier: list[str],
        ancestry_cut: dict[str, int],
    ) -> RunState:
        events = self.store.events(run.run_id)
        cp = Checkpoint.new(
            run_id=run.run_id,
            boundary=boundary,
            event_seq=events[-1].seq if events else 0,
            frontier=frontier,
            visible=visible,
            phase=phase,
            policy={k: run.config[k] for k in ("policy", "parser", "decision_provider")},
            memory_release=run.config.get("memory_release"),
            context={"round": round, "adapter": "deterministic-mock", "rendered_context": None},
            budget=Ledger(self.store, run).snapshot(),
            jobs=[],
            quiescent=True,
            ancestry_cut=ancestry_cut,
            capabilities=CAPABILITIES,
        )
        digest = self.store.put_record(cp)
        self.store.append_event(
            run.run_id,
            actor="controller",
            type="checkpoint",
            payload={"boundary": boundary, "phase": phase},
            payload_refs={"checkpoint": record_ref("checkpoint", digest)},
        )
        return RunState(
            run.run_id, phase, round, boundary, digest, visible, frontier, ancestry_cut, run.config
        )


def _find(events: list[Event], type: str, **match: Any) -> Event | None:
    for e in reversed(events):
        if e.type == type and all(e.payload.get(k) == v for k, v in match.items()):
            return e
    return None


def normalized_final_state(store: Store, state: RunState) -> dict[str, Any]:
    """The run's final fixture state, free of ids and timestamps, with its digest."""
    final = store.get_json(state.visible["final"])
    return {"final": final, "digest": digest_of(final)}
