"""Record a fixed-input ORCA case as a run, parse it, and evaluate it.

A case run has the same journal shape as a fixture run, so inspect, export,
import, and offline replay apply unchanged; its checkpoints declare that
fixture resume, fork, and reanalysis do not apply. Phases:

    run.created -> initialized
    -> acquire (authentic outputs produced elsewhere) | execute (local ORCA)
    -> job-collected -> parse (orca-parser@3) -> diagnostics-committed
    -> finalized (visible["final"] is the observation)

Acquired outputs are recorded as a `read` invocation with the upstream
provenance and license; they were not executed here and say so. Live
execution goes through `OrcaLocalScheduler`, which refuses to run unless an
operator enabled it and resolved every setting.

Evaluation reads the observation from the agent store and writes only to the
evaluator store. A cross-case reference (e.g. the same input under another
ORCA version) is parsed by the evaluator from the pinned case bytes and
published with evaluator visibility, so it never enters the agent store.

Every step is idempotent against the journal: rerunning `record_case_run`
with the same run id continues an interrupted run, and a live job is
reconciled with the scheduler by its idempotency key before any resubmission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import CapabilityError, IntegrityError, OnodaError, SubmissionUncertain
from onoda.records import (
    Checkpoint,
    Evaluation,
    InvocationReceipt,
    Run,
    StepSpec,
    TaskManifest,
    Usage,
)
from onoda.science.contract import LoadedCase, OrcaObservation
from onoda.science.evaluator import evaluate_observation, publish_case_reference
from onoda.science.orca_local import OrcaLocalScheduler
from onoda.science.orca_parser import PARSER_ID, parse_orca_output
from onoda.store import Store, check_safe_id, record_ref

CASE_CAPABILITIES = {
    "inspect": "supported",
    "reanalyze": "unsupported: onoda reanalyze knows fixture parsers only",
    "fork": "unsupported: a case run has no decision step to fork at",
    "semantic_restart": "unsupported: rerun `onoda case ingest` with the same run id",
    "native_resume": "unsupported",
    "reexecute": "requires-operator-enabled-orca",
}
UNKNOWN_COST = Usage(units={}, measured=False, simulated=False, cost_usd=None)
ACQUIRE, EXECUTE, PARSE = "acquire:orca-output", "execute:orca", f"parse:{PARSER_ID}"


@dataclass(frozen=True)
class CaseRun:
    run_id: str
    case_id: str
    mode: str
    log: str
    observation: str
    checkpoint: str


class _Journal:
    """Commit-before-visible helpers for one case run, idempotent per step.

    Every step first looks for its own committed event, so rerunning
    `record_case_run` after a crash continues the journal instead of
    repeating work or writing a second copy of a checkpoint.
    """

    def __init__(self, store: Store, run: Run):
        self.store = store
        self.run = run

    def events(self, type: str, **match: Any):
        return [
            e
            for e in self.store.events(self.run.run_id)
            if e.type == type and all(e.payload.get(k) == v for k, v in match.items())
        ]

    def event(self, type: str, **kwargs: Any):
        return self.store.append_event(self.run.run_id, actor="case-runner", type=type, **kwargs)

    def committed(self, step_key: str) -> InvocationReceipt | None:
        found = self.events("invocation.committed", step_key=step_key)
        return self.store.receipt(found[-1].payload["invocation_id"]) if found else None

    def invocation(
        self,
        step_key: str,
        spec: StepSpec,
        kind: str,
        inputs: dict[str, str],
        outputs: dict[str, str],
        *,
        status: str = "succeeded",
        parents: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        labels: list[str] | None = None,
        usage: Usage = UNKNOWN_COST,
    ) -> InvocationReceipt:
        spec_id = self.store.put_record(spec)
        n = len(self.events("invocation.committed")) + 1
        inv_id = f"{self.run.run_id}.i{n:03d}"
        if self.store.has_record("invocation", inv_id):
            # Committed just before a crash, not yet journaled: journal it as is.
            receipt = self.store.receipt(inv_id)
            if receipt.spec_digest != spec_id:
                raise OnodaError(f"orphan receipt {inv_id} belongs to another step")
        else:
            now = self.store.clock.now()
            receipt = InvocationReceipt.new(
                invocation_id=inv_id,
                run_id=self.run.run_id,
                spec_digest=spec_id,
                invocation_kind=kind,
                inputs=inputs,
                outputs=outputs,
                parents=parents or [],
                status=status,
                metadata=metadata or {},
                usage=usage,
                started_at=now,
                finished_at=now,
                labels=labels or [],
            )
            self.store.put_record(receipt)
        self.event(
            "invocation.committed",
            payload={"invocation_id": inv_id, "step_key": step_key},
            payload_refs={"receipt": record_ref("invocation", inv_id)},
        )
        return receipt

    def checkpoint(
        self, boundary: str, phase: str, visible: dict[str, str], frontier: list[str]
    ) -> str:
        done = self.events("checkpoint", boundary=boundary)
        if done:
            return done[-1].payload_refs["checkpoint"].split("/", 1)[1]
        events = self.store.events(self.run.run_id)
        cp = Checkpoint.new(
            run_id=self.run.run_id,
            boundary=boundary,
            event_seq=events[-1].seq if events else 0,
            frontier=frontier,
            visible=visible,
            phase=phase,
            policy={"parser": PARSER_ID},
            context={"case_id": self.run.config["case_id"]},
            quiescent=True,
            capabilities=CASE_CAPABILITIES,
        )
        digest = self.store.put_record(cp)
        self.event(
            "checkpoint",
            payload={"boundary": boundary, "phase": phase},
            payload_refs={"checkpoint": record_ref("checkpoint", digest)},
        )
        return digest


def _manifest_json(case: LoadedCase) -> dict[str, Any]:
    return case.manifest.model_dump(mode="json")


def _start(store: Store, case: LoadedCase, run_id: str, mode: str) -> tuple[_Journal, str]:
    """Create the run, or reopen an unfinished one recorded from the same case."""
    check_safe_id(run_id, "run id")
    m = case.manifest
    if store.has_record("run", run_id):
        run = store.run_record(run_id)
        task = store.get_record("task", run.task)
        if run.config.get("case_id") != m.case_id or run.config.get("mode") != mode:
            raise OnodaError(f"run {run_id} already exists for a different case or mode")
        if task.inputs.get("case") != sha256_digest(canonical_bytes(_manifest_json(case))):
            raise OnodaError(f"run {run_id} was recorded from a different case manifest")
        j = _Journal(store, run)
        if j.events("checkpoint", boundary="finalized"):
            raise OnodaError(f"run {run_id} already exists and is finalized")
        if not j.events("run.created"):
            _announce(j, task)  # records committed, event lost in the crash
        return j, task.inputs["input"]

    inp = store.put_bytes(
        case.input_bytes,
        media_type="text/plain",
        source={"case_id": m.case_id, "path": m.input.path},
        license=m.provenance.license,
        labels=["task-input", "orca-input"],
    )
    # The manifest is pinned to the run so evaluation grades against the
    # request the run was recorded with, not whatever the case file says later.
    manifest = store.put_json(_manifest_json(case), labels=["case-manifest"])
    task = TaskManifest.new(
        task_id=f"case-{m.case_id}-{manifest.digest[7:19]}",
        inputs={"input": inp.digest, "case": manifest.digest},
        permitted_context=["task-input"],
        split=m.split,
        target={"kind": "orca-case", "case_id": m.case_id, "review": m.review.status},
        profile_versions={"orca": m.program_version},
        synthetic=False,
    )
    store.put_record(task)
    run = Run.new(
        run_id=run_id,
        experiment_id="orca-smoke",
        task=task.task_id,
        config={
            "mode": mode,
            "case_id": m.case_id,
            "parser": PARSER_ID,
            "allow_live_execution": mode == "case-live",
        },
        quota_id="orca-smoke-unbudgeted",
        created_at=store.clock.now(),
        labels=[mode, *m.labels] if mode == "case-acquired" else [mode],
    )
    store.put_record(run)
    j = _Journal(store, run)
    _announce(j, task)
    return j, inp.digest


def _announce(j: _Journal, task: TaskManifest) -> None:
    j.event(
        "run.created",
        payload_refs={
            "run": record_ref("run", j.run.run_id),
            "task": record_ref("task", task.task_id),
        },
    )


# Context: doc://onoda/case-data-provenance@0002#pinned-acquired-cases — outputs we did not run are acquired (executed_here: false), never executions
def _acquire(j: _Journal, case: LoadedCase, inp: str) -> tuple[InvocationReceipt, bool]:
    if done := j.committed(ACQUIRE):
        return done, False
    m = case.manifest
    prov = m.provenance.model_dump(mode="json")
    log = j.store.put_bytes(
        case.output_bytes["log"],
        media_type="text/plain",
        encoding="utf-8",
        source={"case_id": m.case_id, **prov},
        license=m.provenance.license,
        labels=["job-output", *m.labels],
    )
    spec = StepSpec.new(
        operation=ACQUIRE,
        inputs={"input": inp},
        config={"case_id": m.case_id, "program": m.program, "program_version": m.program_version},
        code_ref="onoda.science.runner:_acquire",
        determinism="external",
    )
    receipt = j.invocation(
        ACQUIRE,
        spec,
        "read",
        {"input": inp},
        {"log": log.digest},
        metadata={"provenance": prov, "executed_here": False},
        labels=list(m.labels),
    )
    return receipt, False


def _execute(
    j: _Journal, case: LoadedCase, inp: str, scheduler: OrcaLocalScheduler
) -> tuple[InvocationReceipt, bool]:
    """Submit once per run, reconciling with the scheduler after a crash."""
    if done := j.committed(EXECUTE):
        return done, bool(done.metadata.get("stand_in"))
    # The run record's digest ties the key to this store's run, so another
    # store reusing the run id on a shared work root never adopts this job.
    key = f"{j.run.run_id}:{j.run.record_digest()[7:19]}:execute"
    spec = StepSpec.new(
        operation=EXECUTE,
        inputs={"input": inp},
        config={"case_id": case.manifest.case_id},
        code_ref="onoda.science.orca_local:OrcaLocalScheduler",
        determinism="external",
    )
    submitted = j.events("job.submitted", key=key)
    if submitted:
        job_id = submitted[-1].payload["job_id"]
    else:
        intents = j.events("job.intent", key=key)
        job_id = None
        if intents:
            try:
                job_id = scheduler.lookup(key)
            except SubmissionUncertain:
                j.event("job.submission-uncertain", payload={"key": key})
                raise
            intent = intents[-1]
        else:
            intent = j.event("job.intent", payload={"step_key": EXECUTE, "key": key})
        reconciled = job_id is not None
        if job_id is None:
            job_id = scheduler.submit(key, "orca", {"input": case.input_bytes}, {})
        j.event(
            "job.submitted",
            payload={"key": key, "job_id": job_id, "reconciled": reconciled},
            causes=[intent.event_id],
        )
    meta = scheduler.job_metadata(job_id)
    status = "succeeded" if scheduler.status(job_id) == "completed" else "failed"
    outputs = {
        name: j.store.put_bytes(
            data, media_type="text/plain", labels=["job-output", *meta["labels"]]
        ).digest
        for name, data in sorted(scheduler.collect(job_id).items())
    }
    receipt = j.invocation(
        EXECUTE,
        spec,
        "execution",
        {"input": inp},
        outputs,
        status=status,
        metadata={"job_id": job_id, "idempotency_key": key, **meta},
        labels=list(meta["labels"]),
    )
    return receipt, bool(meta["stand_in"])


def record_case_run(
    store: Store,
    case: LoadedCase,
    run_id: str,
    *,
    scheduler: OrcaLocalScheduler | None = None,
) -> CaseRun:
    """Record, parse, and finalize one case run in the agent store.

    Without a scheduler the pinned authentic outputs are acquired; with one,
    the pinned input is executed locally (the scheduler enforces opt-in).
    Calling it again with the same run id continues an unfinished run.
    """
    mode = "case-live" if scheduler is not None else "case-acquired"
    j, inp = _start(store, case, run_id, mode)
    j.checkpoint("initialized", "collect", {"input": inp}, [])
    if scheduler is None:
        produced, synthetic = _acquire(j, case, inp)
    else:
        produced, synthetic = _execute(j, case, inp, scheduler)
    log = produced.outputs.get("log")
    if log is None:
        raise CapabilityError(f"run {run_id}: execution produced no log to parse")
    j.checkpoint("job-collected", "parse", {"input": inp, "log": log}, [produced.invocation_id])

    parse = j.committed(PARSE)
    if parse is None:
        observation = parse_orca_output(store.get_bytes(log), synthetic=synthetic)
        obs = store.put_json(
            observation.to_json(),
            labels=["observation", PARSER_ID, *(["synthetic"] if synthetic else [])],
        )
        parse = j.invocation(
            PARSE,
            StepSpec.new(
                operation=PARSE,
                inputs={"log": log},
                code_ref="onoda.science.orca_parser:parse_orca_output",
                determinism="pure",
            ),
            "derivation",
            {"log": log},
            {"observation": obs.digest},
            parents=[produced.invocation_id],
            usage=Usage(units={}, measured=False, simulated=False, cost_usd="0.00"),
        )
    observation_digest = parse.outputs["observation"]
    visible = {"input": inp, "log": log, "observation": observation_digest}
    frontier = [produced.invocation_id, parse.invocation_id]
    j.checkpoint("diagnostics-committed", "finalize", visible, frontier)
    cp = j.checkpoint("finalized", "done", {**visible, "final": observation_digest}, frontier)
    return CaseRun(run_id, case.manifest.case_id, mode, log, observation_digest, cp)


def _finalized(store: Store, run_id: str) -> Checkpoint:
    for event in reversed(store.events(run_id)):
        if event.type == "checkpoint" and event.payload.get("boundary") == "finalized":
            return store.checkpoint(event.payload_refs["checkpoint"].split("/", 1)[1])
    raise CapabilityError(f"run {run_id} has no finalized checkpoint")


def final_observation(store: Store, run_id: str) -> tuple[str, OrcaObservation]:
    """The observation digest from the run's finalized checkpoint."""
    digest = _finalized(store, run_id).visible["final"]
    return digest, OrcaObservation.model_validate(store.get_json(digest))


def publish_reference_case(evaluator_store: Store, reference: LoadedCase) -> str:
    """Parse a reference case's pinned output and publish it evaluator-only."""
    m = reference.manifest
    observation = parse_orca_output(reference.output_bytes["log"])
    return publish_case_reference(
        evaluator_store,
        case_id=m.case_id,
        observation=observation,
        reference_version=f"case:{m.case_id}@{m.outputs['log'].sha256[7:19]}",
        provenance=m.provenance.model_dump(mode="json"),
    )


def evaluate_case_run(
    agent_store: Store,
    evaluator_store: Store,
    run_id: str,
    case: LoadedCase,
    *,
    policy: str,
    reference: LoadedCase | None = None,
) -> Evaluation:
    """Grade a finalized case run against the trusted, pinned case.

    The agent store may hold imported records, so nothing it says about the
    case is taken on trust: the manifest pinned at ingest must equal `case`'s
    (loaded from the repository by the caller), an acquired log must be the
    pinned upstream bytes, and the stored observation must equal a fresh
    parse of the stored log. Any mismatch refuses the evaluation.
    """
    cp = _finalized(agent_store, run_id)
    run = agent_store.run_record(run_id)
    task = agent_store.get_record("task", run.task)
    pinned = sha256_digest(canonical_bytes(_manifest_json(case)))
    if run.config.get("case_id") != case.manifest.case_id or task.inputs.get("case") != pinned:
        raise IntegrityError(f"run {run_id} was not recorded from case {case.manifest.case_id}")
    produced = agent_store.receipt(cp.frontier[0])
    log = cp.visible["log"]
    if produced.invocation_kind == "read" and log != case.manifest.outputs["log"].sha256:
        raise IntegrityError(f"run {run_id}: acquired log is not the pinned upstream output")
    synthetic = produced.invocation_kind == "execution" and bool(produced.metadata.get("stand_in"))
    fresh = parse_orca_output(agent_store.get_bytes(log), synthetic=synthetic)
    if agent_store.get_json(cp.visible["final"]) != fresh.to_json():
        raise IntegrityError(f"run {run_id}: stored observation differs from a fresh parse")
    ref = publish_reference_case(evaluator_store, reference) if reference else None
    return evaluate_observation(
        agent_store,
        evaluator_store,
        run_id=run_id,
        observation_digest=cp.visible["final"],
        case=case.manifest,
        policy=policy,
        reference=ref,
        receipt_status=produced.status,
        case_digest=pinned,
    )
