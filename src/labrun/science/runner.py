"""Record a fixed-input ORCA case as a run, parse it, and evaluate it.

A case run has the same journal shape as a fixture run, so inspect, export,
and the M1 evaluator plumbing apply unchanged:

    run.created -> initialized
    -> acquire (authentic outputs produced elsewhere) | execute (local ORCA)
    -> job-collected -> parse (orca-parser@1) -> diagnostics-committed
    -> finalized (visible["final"] is the observation)

Acquired outputs are recorded as a `read` invocation with the upstream
provenance and license; they were not executed here and say so. Live
execution goes through `OrcaLocalScheduler`, which refuses to run unless an
operator enabled it and resolved every setting.

Evaluation reads the observation from the agent store and writes only to the
evaluator store. A cross-case reference (e.g. the same input under another
ORCA version) is parsed by the evaluator from the pinned case bytes and
published with evaluator visibility, so it never enters the agent store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from labrun.errors import CapabilityError, LabrunError
from labrun.records import (
    Checkpoint,
    Evaluation,
    InvocationReceipt,
    Run,
    StepSpec,
    TaskManifest,
    Usage,
)
from labrun.science.contract import LoadedCase, OrcaObservation
from labrun.science.evaluator import evaluate_observation, publish_case_reference
from labrun.science.orca_local import OrcaLocalScheduler
from labrun.science.orca_parser import PARSER_ID, parse_orca_output
from labrun.store import Store, check_safe_id, record_ref

CASE_CAPABILITIES = {
    "inspect": "supported",
    "semantic_restart": "unsupported",
    "native_resume": "unsupported",
    "reexecute": "requires-operator-enabled-orca",
}
UNKNOWN_COST = Usage(units={}, measured=False, simulated=False, cost_usd=None)


@dataclass(frozen=True)
class CaseRun:
    run_id: str
    case_id: str
    mode: str
    log: str
    observation: str
    checkpoint: str


class _Journal:
    """Commit-before-visible helpers for one case run."""

    def __init__(self, store: Store, run: Run):
        self.store = store
        self.run = run
        self.n = 0

    def event(self, type: str, **kwargs: Any):
        return self.store.append_event(self.run.run_id, actor="case-runner", type=type, **kwargs)

    def invocation(
        self,
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
        self.n += 1
        now = self.store.clock.now()
        receipt = InvocationReceipt.new(
            invocation_id=f"{self.run.run_id}.i{self.n:03d}",
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
            payload={"invocation_id": receipt.invocation_id, "step_key": spec.operation},
            payload_refs={"receipt": record_ref("invocation", receipt.invocation_id)},
        )
        return receipt

    def checkpoint(
        self, boundary: str, phase: str, visible: dict[str, str], frontier: list[str]
    ) -> str:
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


def _start(store: Store, case: LoadedCase, run_id: str, mode: str) -> tuple[_Journal, str]:
    check_safe_id(run_id, "run id")
    if store.has_record("run", run_id):
        raise LabrunError(f"run {run_id} already exists")
    m = case.manifest
    inp = store.put_bytes(
        case.input_bytes,
        media_type="text/plain",
        source={"case_id": m.case_id, "path": m.input.path},
        license=m.provenance.license,
        labels=["task-input", "orca-input"],
    )
    task = TaskManifest.new(
        task_id=f"case-{m.case_id}",
        inputs={"input": inp.digest},
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
    j.event(
        "run.created",
        payload_refs={"run": record_ref("run", run_id), "task": record_ref("task", task.task_id)},
    )
    j.checkpoint("initialized", "collect", {"input": inp.digest}, [])
    return j, inp.digest


def _acquire(j: _Journal, case: LoadedCase, inp: str) -> tuple[InvocationReceipt, bytes]:
    m = case.manifest
    prov = m.provenance.model_dump(mode="json")
    log_bytes = case.output_bytes["log"]
    log = j.store.put_bytes(
        log_bytes,
        media_type="text/plain",
        encoding="utf-8",
        source={"case_id": m.case_id, **prov},
        license=m.provenance.license,
        labels=["job-output", *m.labels],
    )
    spec = StepSpec.new(
        operation="acquire:orca-output",
        inputs={"input": inp},
        config={"case_id": m.case_id, "program": m.program, "program_version": m.program_version},
        code_ref="labrun.science.runner:_acquire",
        determinism="external",
    )
    receipt = j.invocation(
        spec,
        "read",
        {"input": inp},
        {"log": log.digest},
        metadata={"provenance": prov, "executed_here": False},
        labels=list(m.labels),
    )
    return receipt, log_bytes


def _execute(
    j: _Journal, case: LoadedCase, inp: str, scheduler: OrcaLocalScheduler
) -> tuple[InvocationReceipt, bytes]:
    m = case.manifest
    key = f"{j.run.run_id}:execute"
    spec = StepSpec.new(
        operation="execute:orca",
        inputs={"input": inp},
        config={"case_id": m.case_id},
        code_ref="labrun.science.orca_local:OrcaLocalScheduler",
        determinism="external",
    )
    intent = j.event("job.intent", payload={"step_key": spec.operation, "key": key})
    job_id = scheduler.submit(key, "orca", {"input": case.input_bytes}, {})
    j.event("job.submitted", payload={"key": key, "job_id": job_id}, causes=[intent.event_id])
    meta = scheduler.job_metadata(job_id)
    status = "succeeded" if scheduler.status(job_id) == "completed" else "failed"
    outputs = {}
    log_bytes = b""
    for name, data in sorted(scheduler.collect(job_id).items()):
        outputs[name] = j.store.put_bytes(
            data, media_type="text/plain", labels=["job-output", *meta.get("labels", [])]
        ).digest
        if name == "log":
            log_bytes = data
    receipt = j.invocation(
        spec,
        "execution",
        {"input": inp},
        outputs,
        status=status,
        metadata={"job_id": job_id, "idempotency_key": key, **meta},
        labels=list(meta.get("labels", [])),
        usage=UNKNOWN_COST,
    )
    return receipt, log_bytes


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
    """
    mode = "case-live" if scheduler is not None else "case-acquired"
    j, inp = _start(store, case, run_id, mode)
    if scheduler is None:
        produced, log_bytes = _acquire(j, case, inp)
    else:
        produced, log_bytes = _execute(j, case, inp, scheduler)
    log = produced.outputs.get("log")
    if log is None:
        raise CapabilityError(f"run {run_id}: execution produced no log to parse")
    synthetic = "stand-in" in produced.labels
    j.checkpoint("job-collected", "parse", {"input": inp, "log": log}, [produced.invocation_id])

    observation = parse_orca_output(log_bytes, synthetic=synthetic)
    obs = store.put_json(
        observation.to_json(),
        labels=["observation", PARSER_ID, *(["synthetic"] if synthetic else [])],
    )
    parse = j.invocation(
        StepSpec.new(
            operation=f"parse:{PARSER_ID}",
            inputs={"log": log},
            code_ref="labrun.science.orca_parser:parse_orca_output",
            determinism="pure",
        ),
        "derivation",
        {"log": log},
        {"observation": obs.digest},
        parents=[produced.invocation_id],
        usage=Usage(units={}, measured=False, simulated=False, cost_usd="0.00"),
    )
    visible = {"input": inp, "log": log, "observation": obs.digest}
    frontier = [produced.invocation_id, parse.invocation_id]
    j.checkpoint("diagnostics-committed", "finalize", visible, frontier)
    cp = j.checkpoint("finalized", "done", {**visible, "final": obs.digest}, frontier)
    return CaseRun(run_id, case.manifest.case_id, mode, log, obs.digest, cp)


def final_observation(store: Store, run_id: str) -> tuple[str, OrcaObservation]:
    """The observation digest from the run's finalized checkpoint."""
    for event in reversed(store.events(run_id)):
        if event.type == "checkpoint" and event.payload.get("boundary") == "finalized":
            cp = store.checkpoint(event.payload_refs["checkpoint"].split("/", 1)[1])
            digest = cp.visible["final"]
            return digest, OrcaObservation.model_validate(store.get_json(digest))
    raise CapabilityError(f"run {run_id} has no finalized checkpoint")


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
    digest, _ = final_observation(agent_store, run_id)
    ref = publish_reference_case(evaluator_store, reference) if reference else None
    return evaluate_observation(
        agent_store,
        evaluator_store,
        run_id=run_id,
        observation_digest=digest,
        case=case.manifest,
        policy=policy,
        reference=ref,
    )
