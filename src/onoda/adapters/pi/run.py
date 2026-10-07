"""One Pi dev-set run end to end: stage, commit, launch, ingest; and replay.

`run_case` commits every byte the agent may see (case input, pinned output,
allowed docs, ladder) and the script to the store *before* Pi starts, so the
spool's later references resolve to objects already committed. It then
launches the pinned Pi process, ingests the spool, and returns the result.
The Pi process never writes to the store (A37).

`replay_run_pi` re-runs a recorded run under its own recorded responses
(`ingest.recorded_script`) in a scratch directory, without writing to the
store, and raises `ReplayMismatch` unless the replay ends with the recorded
`spool.end` status, the same replay key for every agent request, the same
number of reducer and compaction-summary requests, and the same claim (A34).
Only scripted-mode responses are replayed; nothing falls back to a live
provider. Replay executes the recorded tool calls (bash included), so a run
that arrived through `import_bundle` is refused: import is data-only.

`run_vte_case` is the VTE task (A57-A59): it stages a `VteTaskBundle`
(structure, targets, docs, ladder; the case's references are never staged or
committed), starts an `OrcaBroker` on a Unix socket in the Pi work dir for
the run's `orca_submit` calls, launches Pi with the VTE tools, stops the
broker, commits the broker's journal and ingests the spool against it. The
journal lives outside the Pi work dir, so the Pi process cannot rewrite it.
`replay_run_pi` replays a VTE run with the broker in replay mode, serving
only the recorded outputs (A59): nothing executes, and any changed
submission is a `replay_miss` the recorded responses then fail on.
Grading a VTE run (`evaluate_vte_run`, `vte_report`) lives in `vte`.

The spool HMAC key of each launch stays in memory and is handed to ingest
(or the replay's spool check); it is never stored.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onoda.adapters.pi.contract import (
    SPOOL_EVENTS,
    VARIANTS,
    HarnessCandidate,
    Script,
    candidate_for,
    derive_child_key,
)
from onoda.adapters.pi.ingest import (
    IngestResult,
    _read_spool,
    ingest_spool,
    recorded_script,
)
from onoda.adapters.pi.runtime import Launch, LaunchResult, LiveSettings, build_launch, launch
from onoda.adapters.pi.sandbox import PiSandbox
from onoda.adapters.pi.task import check_doc_name, stage_bundle
from onoda.adapters.pi.vte import stage_vte_bundle
from onoda.adapters.pi.vte_contract import DEFAULT_MAX_EXECUTIONS, ENV_BROKER, VteTaskBundle
from onoda.budgets import child_links
from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import CapabilityError, OnodaError, ReplayMismatch
from onoda.portability import imported_runs
from onoda.records import ForkDeclaration, TaskManifest
from onoda.science.contract import CASES_ROOT, LoadedCase
from onoda.science.orca_broker import (
    DEFAULT_LIMITS,
    JOURNAL_NAME,
    BrokerJournalEntry,
    BrokerLimits,
    BrokerReplay,
    OrcaBroker,
    parse_journal,
    read_journal,
)
from onoda.science.orca_local import OrcaLocalScheduler, OrcaSettings
from onoda.science.orca_sandbox import host_job_sandbox
from onoda.science.vte import REFERENCES_SCHEMA, LoadedVteCase
from onoda.store import Store, check_safe_id

DEFAULT_INSTRUCTION = (
    "Read the ORCA input with case_input, obtain its output with orca_run, then call "
    "report_states once with the final single-point energy and every excited state the "
    "output lists, copying each value and unit exactly as printed."
)

# Context: doc://onoda/vte-grading-contract@0002#claim-from-one-cited-job — both arms are told the grading contract in the same words
VTE_INSTRUCTION = (
    "Read the molecule and its target excited states with structure, write SA-CASSCF/SC-NEVPT2 "
    "ORCA inputs and run them with orca_submit, then call report_vtes once with each target "
    "state you identified: its spin, irrep, dominant character, the SC-NEVPT2 vertical "
    "excitation energy in eV exactly as printed, and the job id whose output prints it. "
    "A value counts only if that one job prints it in its SC-NEVPT2 transition energy table "
    "with the state's irrep: run state-averaged jobs (the ground state and the excited roots "
    "of one spin together) with point-group symmetry on, and do not report differences of "
    "total energies from separate jobs (A73). A state is matched to a target by its spin, "
    "irrep and character together, so give the character you assigned from the active "
    "orbitals as an orbital pair (for example n,pi*)."
)

# The eight QUESTDB dev cases (A62); dev-only, never part of `cases/` or a split.
VTE_CASES_ROOT = CASES_ROOT.parent / "cases-dev" / "questdb"
BROKER_SOCKET = "broker.sock"  # in the Pi work dir: the outer sandbox layer sees it, bash does not


@dataclass(frozen=True)
class PiRun:
    ingest: IngestResult
    exit_code: int
    stderr_tail: str


@dataclass(frozen=True)
class ForkLaunch:
    """What makes a launch a fork (built by `adapters.pi.fork`, never by hand).

    `declaration` is committed with the task, once the launch is staged;
    `config` is recorded as the run's `config["fork"]`; `task_id` is the
    parent's task, which the fork must reproduce exactly; `broker_prefix`
    serves a VTE parent's jobs before the fork point.
    """

    declaration: ForkDeclaration
    config: dict[str, Any]
    task_id: str
    broker_prefix: BrokerReplay | None = None


def _commit_fork(store: Store, fork: ForkLaunch, task: TaskManifest, config: dict[str, Any]) -> str:
    """Commit the declaration and record the fork in `config`; refuse a task that differs."""
    if task.task_id != fork.task_id:
        raise OnodaError(f"the fork staged task {task.task_id}, not its parent's {fork.task_id}")
    config["fork"] = fork.config
    return store.put_record(fork.declaration)


def _commit_task(
    store: Store,
    case: LoadedCase,
    *,
    variant_id: str,
    docs: Mapping[str, bytes],
    ladder: bytes | None,
    synthetic_ladder: bool,
) -> TaskManifest:
    m = case.manifest
    lic = m.provenance.license
    src = {"case_id": m.case_id}
    inputs = {
        "input": store.put_bytes(
            case.input_bytes,
            media_type="text/plain",
            source=src,
            license=lic,
            labels=["task-input", "orca-input"],
        ).digest,
    }
    ((out_key, out_bytes),) = case.output_bytes.items()
    inputs["output"] = store.put_bytes(
        out_bytes,
        media_type="text/plain",
        source={**src, "output": out_key},
        license=lic,
        labels=["orca-output", "acquired"],
    ).digest
    inputs["case"] = store.put_json(m.model_dump(mode="json"), labels=["case-manifest"]).digest
    for name in sorted(docs):
        inputs[f"doc:{name}"] = store.put_bytes(
            docs[name], media_type="text/markdown", labels=["allowed-documentation"]
        ).digest
    if ladder is not None:
        inputs["ladder"] = store.put_bytes(
            ladder,
            media_type="text/markdown",
            labels=["advisory-ladder", *(["synthetic"] if synthetic_ladder else [])],
        ).digest
    # The variant is part of the task (profile_versions), so it is part of the id:
    # every arm of an ablation stages the same inputs into one store.
    task_key = sha256_digest(canonical_bytes({"inputs": inputs, "variant": variant_id}))
    task = TaskManifest.new(
        task_id=f"pi-{m.case_id}-{task_key[7:19]}",
        inputs=inputs,
        permitted_context=["task-input", "orca-output", "allowed-documentation", "advisory-ladder"],
        split=m.split,
        target={"kind": "orca-case", "case_id": m.case_id, "review": m.review.status},
        profile_versions={"orca": m.program_version, "variant": variant_id},
        synthetic=False,
    )
    store.put_record(task)
    return task


def run_case(
    store: Store,
    case: LoadedCase,
    *,
    run_id: str,
    variant_id: str,
    work_dir: Path,
    script: Script | None = None,
    live: LiveSettings | None = None,
    instruction: str = DEFAULT_INSTRUCTION,
    docs: Mapping[str, bytes] = {},
    ladder: bytes | None = None,
    synthetic_ladder: bool = False,
    max_requests: int | None = None,
    max_acquisitions: int = 1,
    reducer: tuple[str, str] | None = None,
    timeout_s: float = 300,
    candidate: HarnessCandidate | None = None,
    sandbox: PiSandbox | None = None,
    fork: ForkLaunch | None = None,
) -> PiRun:
    """Run one variant on one case and ingest it as `run_id`.

    `fork` (from `adapters.pi.fork.fork_pi_run`) makes the run a fork of a
    recorded run: its declaration is committed with the task and the run
    cites its parent.

    `sandbox` runs Pi inside the sandbox (A50; bwrap or Seatbelt, recorded as
    its `kind`); a live run needs one.

    A scripted run's `max_requests` defaults to 16. A live run's request cap
    is its own `live.max_requests` (A35); a different `max_requests` here is
    refused. `reducer` = (provider, model) names the reducer for S2b/S3
    (scripted runs default to the scripted reducer).

    `candidate` (M4) is threaded through to `build_launch`. An RLM variant
    without one runs `SEED_CANDIDATE`, recorded like any other candidate, so
    its replay never depends on the seed staying the same. Otherwise, with none
    given, every argv/env/run_info/config value stays exactly what M3 produced.
    When given, its components file is committed to the store (labelled
    `harness-candidate`); its `ladder` component, when set, replaces the
    ladder bytes staged into the bundle (recorded as `ladder_source:
    "candidate"`), and its `sol_pi.config`, when set, is recorded as
    `sol_pi_config_override`.
    """
    _check_new_run(store, run_id, variant_id)
    candidate = candidate_for(variant_id, candidate)
    max_requests = _request_cap(live, max_requests)
    work_dir = Path(work_dir)
    bundle_dir = work_dir / "bundle"
    candidate_ladder = candidate.ladder_bytes() if candidate is not None else None
    effective_ladder = candidate_ladder if candidate_ladder is not None else ladder
    stage_bundle(
        case,
        bundle_dir,
        instruction=instruction,
        docs=docs,
        ladder=effective_ladder,
        synthetic_ladder=synthetic_ladder,
    )
    prepared = build_launch(
        variant_id=variant_id,
        bundle_dir=bundle_dir,
        work_dir=work_dir / "pi",
        script=script,
        live=live,
        max_requests=max_requests,
        max_acquisitions=max_acquisitions,
        reducer=reducer,
        candidate=candidate,
        sandbox=sandbox,
        handoff=_handoff(fork, script, live),
    )
    # Refusals happen above; only a launchable run commits anything.
    task = _commit_task(
        store,
        case,
        variant_id=variant_id,
        docs=docs,
        ladder=effective_ladder,
        synthetic_ladder=synthetic_ladder,
    )
    config: dict[str, Any] = {
        "mode": "pi-scripted" if live is None else "pi-live",
        "variant": variant_id,
        "case_id": case.manifest.case_id,
        "instruction": instruction,
        "max_requests": max_requests,
        "max_acquisitions": max_acquisitions,
    }
    if sandbox is not None:
        config["sandbox"] = sandbox.kind
    if reducer is not None:
        config.update(reducer_provider=reducer[0], reducer_model=reducer[1])
    if candidate is not None:
        config["candidate"] = store.put_json(
            candidate.components, labels=["harness-candidate"]
        ).digest
        if candidate_ladder is not None:
            config["ladder_source"] = "candidate"
        sol_pi_override = candidate.sol_pi_override()
        if sol_pi_override is not None:
            config["sol_pi_config_override"] = sol_pi_override
    fork_digest = _commit_fork(store, fork, task, config) if fork is not None else None
    _record_driver(store, config, script, live)
    result = launch(prepared, timeout_s=timeout_s)
    return _ingest_launch(
        store, prepared, result, run_id=run_id, task=task, config=config, fork=fork_digest
    )


def _check_new_run(store: Store, run_id: str, variant_id: str) -> None:
    check_safe_id(run_id, "run id")
    if variant_id not in VARIANTS:
        raise OnodaError(f"unknown variant {variant_id!r}; see `onoda pi variants`")
    if store.has_record("run", run_id):
        raise OnodaError(f"run {run_id} already exists")


def _ingest_launch(
    store: Store,
    prepared: Launch,
    result: LaunchResult,
    *,
    run_id: str,
    task: TaskManifest,
    config: dict[str, Any],
    broker_journal: tuple[BrokerJournalEntry, ...] | None = None,
    fork: str | None = None,
) -> PiRun:
    """Ingest a finished launch's spool as `run_id`, against the broker journal if given."""
    if not (result.spool_dir / SPOOL_EVENTS).is_file():
        raise OnodaError(
            f"pi exited {result.exit_code} before writing a spool: {result.stderr[-2000:]}"
        )
    # A case run's ingest call stays exactly as M3 made it (no `broker_journal` keyword).
    journal = {} if broker_journal is None else {"broker_journal": broker_journal}
    if fork is not None:
        journal["fork"] = fork
    ingested = ingest_spool(
        store,
        result.spool_dir,
        run_id=run_id,
        task=task,
        config=config,
        key=result.spool_key,
        expected_session_dir=prepared.session_dir,
        **journal,
    )
    return PiRun(ingested, result.exit_code, result.stderr[-2000:])


def _record_driver(
    store: Store, config: dict[str, Any], script: Script | None, live: LiveSettings | None
) -> None:
    """Add what drove the agent to `config`: the committed script, or the live settings
    (a fork's live continuation has both: its replayed prefix is `prefix_script`)."""
    if script is not None:
        config["script" if live is None else "prefix_script"] = store.put_json(
            script.model_dump(mode="json", by_alias=True),
            labels=["pi-script", "synthetic" if live is None else "replayed"],
        ).digest
    if live is not None:
        config.update(
            provider=live.provider,
            model=live.model,
            max_cost_usd=live.max_cost_usd,
        )


def _handoff(fork: ForkLaunch | None, script: Script | None, live: LiveSettings | None) -> bool:
    """Whether this launch replays a fork's prefix and then hands off to a live model."""
    if script is not None and live is not None:
        if fork is None:
            raise OnodaError("a run takes a script or live settings, not both, unless it is a fork")
        return True
    return False


def _request_cap(live: LiveSettings | None, max_requests: int | None) -> int:
    """A live run's cap is its own setting (A35); a scripted run's defaults to 16."""
    if live is not None:
        if max_requests is not None and max_requests != live.max_requests:
            raise OnodaError(
                f"max_requests {max_requests} conflicts with the live setting {live.max_requests}"
            )
        return live.max_requests or 0  # 0 or unset: live.validate() refuses it
    return 16 if max_requests is None else max_requests


# ------------------------------------------------------------------ VTE runs


# Context: doc://onoda/evaluator-isolation@0002#references-never-agent-visible — a staged bundle holding the references is refused before anything runs
def _refuse_references(bundle_dir: Path, case: LoadedVteCase) -> None:
    """Refuse a staged bundle holding the evaluator-only references file (A58).

    Checks the pin and the schema string only, so the references' content is
    never read on the agent side: a staged file with the references' bytes, or
    any staged file naming their schema, means a doc, ladder or structure was
    pointed at them.
    """
    pin = case.manifest.references.sha256
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if sha256_digest(data) == pin or REFERENCES_SCHEMA.encode() in data:
            raise CapabilityError(
                f"staged {path.relative_to(bundle_dir)} holds the evaluator-only references of "
                f"{case.manifest.case_id} (A58); nothing was run or committed"
            )


def _commit_vte_task(
    store: Store,
    case: LoadedVteCase,
    bundle_dir: Path,
    bundle: VteTaskBundle,
    *,
    variant_id: str,
    docs: Mapping[str, bytes],
    ladder: bytes | None,
    synthetic_ladder: bool,
) -> TaskManifest:
    """Commit what the agent may see: structure, targets, case manifest, docs, ladder.

    Never the references (A58): the manifest only pins their sha256.
    """
    m = case.manifest
    lic = m.provenance.license
    src = {"case_id": m.case_id}
    inputs = {
        "structure": store.put_bytes(
            case.structure_bytes,
            media_type="chemical/x-xyz",
            source={**src, "path": m.structure.path},
            license=lic,
            labels=["task-input", "structure"],
        ).digest,
        "targets": store.put_bytes(
            (bundle_dir / "targets.json").read_bytes(),
            media_type="application/json",
            source=src,
            labels=["task-input", "vte-targets"],
        ).digest,
        "case": store.put_json(m.model_dump(mode="json"), labels=["vte-case-manifest"]).digest,
    }
    for name in sorted(docs):
        inputs[f"doc:{name}"] = store.put_bytes(
            docs[name], media_type="text/markdown", labels=["allowed-documentation"]
        ).digest
    if ladder is not None:
        inputs["ladder"] = store.put_bytes(
            ladder,
            media_type="text/markdown",
            labels=["advisory-ladder", *(["synthetic"] if synthetic_ladder else [])],
        ).digest
    task_key = sha256_digest(
        canonical_bytes(
            {"inputs": inputs, "variant": variant_id, "max_executions": bundle.max_executions}
        )
    )
    task = TaskManifest.new(
        task_id=f"pi-{m.case_id}-{task_key[7:19]}",
        inputs=inputs,
        permitted_context=["task-input", "allowed-documentation", "advisory-ladder"],
        split=m.split,
        target={"kind": "vte-case", "case_id": m.case_id, "review": m.review.status},
        profile_versions={"variant": variant_id},
        synthetic=False,
    )
    store.put_record(task)
    return task


def _launch_with_broker(prepared: Launch, broker: OrcaBroker, timeout_s: float) -> LaunchResult:
    """Serve `broker` on the launch's socket for exactly the Pi process's lifetime.

    The broker answers one submission at a time, so stopping it waits for a
    job in flight (bounded by the scheduler's wall-clock limit).
    """
    broker.serve(prepared.env[ENV_BROKER])
    try:
        return launch(prepared, timeout_s=timeout_s)
    finally:
        broker.close()


def run_vte_case(
    store: Store,
    case: LoadedVteCase,
    *,
    run_id: str,
    variant_id: str,
    work_dir: Path,
    orca: OrcaSettings,
    cache_dir: Path | None = None,
    script: Script | None = None,
    live: LiveSettings | None = None,
    instruction: str = VTE_INSTRUCTION,
    docs: Mapping[str, bytes] = {},
    ladder: bytes | None = None,
    synthetic_ladder: bool = False,
    max_requests: int | None = None,
    max_executions: int = DEFAULT_MAX_EXECUTIONS,
    reducer: tuple[str, str] | None = None,
    timeout_s: float = 3600,
    sandbox: PiSandbox | None = None,
    orca_mpi_home: Path | None = None,
    limits: BrokerLimits = DEFAULT_LIMITS,
    fork: ForkLaunch | None = None,
    report_reminder: bool = False,
) -> PiRun:
    """Run one variant on one VTE case, its ORCA jobs through a host broker; ingest it.

    `orca` are the broker's live-execution settings (`OrcaSettings`, every
    field off by default; unresolved settings fail closed before anything is
    staged). `cache_dir` is the broker's shared cache (A59), so identical jobs
    across runs are served as acquired. `max_executions` caps `orca_submit`
    calls, cache hits included, and is the same number in the bundle, the
    gateway policy and the broker. The Pi work dir is `work_dir/pi` and the
    broker socket sits in it, so `work_dir` must be short enough for AF_UNIX
    (checked before anything is committed). RLM variants are refused here.
    Request caps, `reducer` and `sandbox` are as for `run_case`.

    Every ORCA job runs in this host's job sandbox (A65; `orca_mpi_home` is
    the MPI prefix it exposes), and real ORCA is refused without one; only a
    labelled stand-in runs unconfined on a host that has none. `limits` are
    the broker's input caps. `fork` is as for `run_case`; its broker prefix
    serves the parent's jobs before the fork point, then the broker runs live.
    `report_reminder` sends the agent one message to call report_vtes when it
    stops without a claim (recorded in the run's config; replay restages it).
    """
    _check_new_run(store, run_id, variant_id)
    max_requests = _request_cap(live, max_requests)
    scheduler = OrcaLocalScheduler(orca)  # resolves the settings; fails closed
    job_sandbox = host_job_sandbox(
        scheduler.settings.binary,
        scratch=scheduler.root,
        stand_in=scheduler.settings.stand_in,
        mpi_home=orca_mpi_home,
    )
    work_dir = Path(work_dir)
    bundle_dir = work_dir / "bundle"
    bundle = stage_vte_bundle(
        case,
        bundle_dir,
        instruction=instruction,
        docs=docs,
        ladder=ladder,
        synthetic_ladder=synthetic_ladder,
        max_executions=max_executions,
        report_reminder=report_reminder,
    )
    _refuse_references(bundle_dir, case)
    prepared = build_launch(
        variant_id=variant_id,
        bundle_dir=bundle_dir,
        work_dir=work_dir / "pi",
        script=script,
        live=live,
        max_requests=max_requests,
        max_acquisitions=0,
        reducer=reducer,
        sandbox=sandbox,
        task_kind="vte",
        broker_socket=work_dir / "pi" / BROKER_SOCKET,
        handoff=_handoff(fork, script, live),
    )
    # Outside the Pi work dir: the journal is the host's record, not Pi's.
    broker = OrcaBroker(
        work_dir / "broker" / JOURNAL_NAME,
        scheduler=scheduler,
        cache_dir=cache_dir,
        prefix=fork.broker_prefix if fork is not None else None,
        max_executions=bundle.max_executions,
        job_sandbox=job_sandbox,
        limits=limits,
    )
    # Refusals happen above; only a launchable run commits anything.
    task = _commit_vte_task(
        store,
        case,
        bundle_dir,
        bundle,
        variant_id=variant_id,
        docs=docs,
        ladder=ladder,
        synthetic_ladder=synthetic_ladder,
    )
    config: dict[str, Any] = {
        "mode": "pi-scripted" if live is None else "pi-live",
        "variant": variant_id,
        "task_kind": "vte",
        "case_id": case.manifest.case_id,
        "instruction": instruction,
        "max_requests": max_requests,
        "max_executions": bundle.max_executions,
        **({"report_reminder": True} if bundle.report_reminder else {}),
        "orca": {
            "binary_sha256": broker.binary_sha256,
            "stand_in": scheduler.settings.stand_in,
            "max_wall_seconds": scheduler.settings.max_wall_seconds,
            "shared_cache": cache_dir is not None,
            "job_sandbox": job_sandbox.kind if job_sandbox is not None else None,
            "limits": dataclasses.asdict(limits),
        },
    }
    if sandbox is not None:
        config["sandbox"] = sandbox.kind
    if reducer is not None:
        config.update(reducer_provider=reducer[0], reducer_model=reducer[1])
    fork_digest = _commit_fork(store, fork, task, config) if fork is not None else None
    _record_driver(store, config, script, live)
    result = _launch_with_broker(prepared, broker, timeout_s)
    # Read once: the bytes committed are exactly the bytes ingest checks the spool against.
    journal = broker.journal_path.read_bytes()
    config["broker_journal"] = store.put_bytes(
        journal, media_type="application/jsonl", labels=["broker-journal"]
    ).digest
    return _ingest_launch(
        store,
        prepared,
        result,
        run_id=run_id,
        task=task,
        config=config,
        broker_journal=parse_journal(journal, str(broker.journal_path)),
        fork=fork_digest,
    )


def _recorded_requests(store: Store, run_id: str) -> dict[str, list[str]]:
    """Replay keys of the recorded requests by role, in request order."""
    receipts = []
    for event in store.events(run_id):
        if event.type == "invocation.committed":
            receipt = store.receipt(event.payload["invocation_id"])
            if receipt.invocation_kind == "decision":
                receipts.append(receipt)
    receipts.sort(key=lambda r: r.metadata["request_index"])
    out: dict[str, list[str]] = {"agent": [], "reducer": [], "compaction-summary": []}
    for r in receipts:
        out[r.metadata["role"]].append(r.metadata["replay_key"])
    return out


def _unkeyed_turn(script: Script) -> bool:
    turns = (*script.turns, *script.reducer_turns, *script.summary_turns)
    return any(t.expect_replay_key is None for t in turns) or any(
        _unkeyed_turn(c) for c in script.children
    )


def _check_children(store: Store, run_id: str, spool_dir: Path, key: bytes) -> None:
    """Every recorded RLM child replayed to the same end with the same agent requests.

    The child spools of a replay sit where the recording's did
    (`children/<nnnn>`, keyed from the launch key), so each is compared with
    its recorded child run, recursively; a child the replay did not spawn, or
    spawned without a spool, is a mismatch.
    """
    for index, child in child_links(store, run_id):
        if child is None:
            continue
        child_dir = spool_dir / "children" / f"{index:04d}"
        if not (child_dir / SPOOL_EVENTS).is_file():
            raise ReplayMismatch(f"replay of {run_id}: child {index:04d} was not spawned")
        child_key = derive_child_key(key, index)
        lines = _read_spool(child_dir, child_key).lines
        ends = [x for x in store.events(child) if x.type == "pi.spool-end"]
        recorded_end = ends[-1].payload["status"] if ends else None
        replayed_end = lines[-1].status if lines[-1].type == "spool.end" else None
        if replayed_end != recorded_end:
            raise ReplayMismatch(
                f"replay of {run_id}: child {index:04d} ended {replayed_end}, "
                f"the recording ended {recorded_end}"
            )
        keys = [
            ln.replay_key for ln in lines if ln.type == "provider.request" and ln.purpose == "agent"
        ]
        if keys != _recorded_requests(store, child)["agent"]:
            raise ReplayMismatch(f"replay of {run_id}: child {index:04d} request keys differ")
        _check_children(store, child, child_dir, child_key)


def _cited_jobs(store: Store, run_id: str) -> set[str]:
    """Broker job ids the run's receipts cite (one per ingested `task.execute`)."""
    jobs = set()
    for event in store.events(run_id):
        if event.type == "invocation.committed":
            job_id = store.receipt(event.payload["invocation_id"]).metadata.get("job_id")
            if isinstance(job_id, str):
                jobs.add(job_id)
    return jobs


def _replay_broker(
    store: Store, run_id: str, cfg: dict[str, Any], work_dir: Path
) -> tuple[OrcaBroker, tuple[BrokerJournalEntry, ...]]:
    """A replay-mode broker serving exactly the recorded outputs of a VTE run (A59).

    Only jobs the run cites (a `task.execute` became a receipt) are served:
    a `pi.broker-unspooled` job's output was never committed, so a replay
    that asks for it gets a `replay_miss` and fails closed as a mismatch.
    Also returns the recorded journal, which the replay's served jobs are checked against.
    """
    digest = cfg.get("broker_journal")
    if not isinstance(digest, str):
        raise CapabilityError(f"run {run_id} recorded no broker journal")
    recorded = parse_journal(store.get_bytes(digest), f"broker journal {digest}")
    cited = _cited_jobs(store, run_id)
    servable = [e for e in recorded if e.event == "reply" and e.job_id in cited]
    outputs = {e.output_sha256: store.get_bytes(e.output_sha256) for e in servable}
    # The recorded input caps; a run recorded before them (A65) replays unchecked.
    limits = cfg.get("orca", {}).get("limits")
    broker = OrcaBroker(
        work_dir / "broker" / JOURNAL_NAME,
        replay=BrokerReplay.from_journal(servable, outputs),
        max_executions=cfg["max_executions"],
        limits=BrokerLimits(**limits) if limits is not None else None,
    )
    return broker, recorded


def _jobs_served(entries: Iterable[BrokerJournalEntry]) -> list[tuple[Any, ...]]:
    """(job id, status, input, output) of every answered submission, in order."""
    return [
        (e.job_id, e.status, e.input_sha256, e.output_sha256)
        for e in entries
        if e.event == "reply" and e.job_id is not None
    ]


def recorded_staging(store: Store, run_id: str) -> dict[str, Any]:
    """The bundle a recorded run was staged with: instruction, docs and ladder, from its task."""
    run = store.run_record(run_id)
    task = store.get_record("task", run.task)
    docs = {}
    for k, d in task.inputs.items():
        if k.startswith("doc:"):
            check_doc_name(k.removeprefix("doc:"))
            docs[k.removeprefix("doc:")] = store.get_bytes(d)
    ladder = store.get_bytes(task.inputs["ladder"]) if "ladder" in task.inputs else None
    ladder_labels = store.descriptor(task.inputs["ladder"]).labels if ladder else []
    return {
        "instruction": run.config["instruction"],
        "docs": docs,
        "ladder": ladder,
        "synthetic_ladder": "synthetic" in ladder_labels,
    }


def replay_run_pi(
    store: Store,
    run_id: str,
    case: LoadedCase | LoadedVteCase,
    *,
    work_dir: Path,
    timeout_s: float = 300,
) -> dict[str, Any]:
    """Re-run a scripted Pi run under its recorded responses; raise on any drift.

    A VTE run (`case` a `LoadedVteCase`) replays with the broker in replay
    mode: it serves the recorded outputs only, so nothing executes, and the
    replay must be served the same jobs (id, status, input, output) in the
    same order.
    """
    run = store.run_record(run_id)
    cfg = run.config
    if cfg.get("mode") != "pi-scripted":
        raise CapabilityError(f"run {run_id} is {cfg.get('mode')!r}; only pi-scripted replays")
    if run_id in imported_runs(store):
        raise CapabilityError(
            f"run {run_id} was imported; replay executes its recorded tool calls, so only "
            "runs recorded in this store are replayed"
        )
    if cfg.get("case_id") != case.manifest.case_id:
        raise OnodaError(f"run {run_id} was recorded from case {cfg.get('case_id')!r}")
    vte = cfg.get("task_kind") == "vte"
    if vte != isinstance(case, LoadedVteCase):
        raise OnodaError(f"run {run_id} is {'a' if vte else 'not a'} VTE run")
    staging = recorded_staging(store, run_id)  # checks every doc name first
    ends = [e for e in store.events(run_id) if e.type == "pi.spool-end"]
    if not ends:
        raise CapabilityError(f"run {run_id} has no spool.end; an incomplete run is not replayed")
    recorded_status = ends[-1].payload["status"]
    script = recorded_script(store, run_id)
    if _unkeyed_turn(script):
        raise CapabilityError(f"run {run_id} has a recorded turn without a replay key")
    candidate = (
        HarnessCandidate(components=store.get_json(cfg["candidate"]))
        if "candidate" in cfg
        else None
    )
    recorded = _recorded_requests(store, run_id)
    work_dir = Path(work_dir)
    broker = None
    recorded_jobs: tuple[BrokerJournalEntry, ...] = ()
    if isinstance(case, LoadedVteCase):
        stage_vte_bundle(
            case,
            work_dir / "bundle",
            max_executions=cfg["max_executions"],
            report_reminder=cfg.get("report_reminder", False),
            **staging,
        )
        _refuse_references(work_dir / "bundle", case)
        broker, recorded_jobs = _replay_broker(store, run_id, cfg, work_dir)
    else:
        stage_bundle(case, work_dir / "bundle", **staging)
    prepared = build_launch(
        variant_id=cfg["variant"],
        bundle_dir=work_dir / "bundle",
        work_dir=work_dir / "pi",
        script=script,
        max_requests=cfg["max_requests"],
        max_acquisitions=cfg.get("max_acquisitions", 0),
        candidate=candidate,
        task_kind="vte" if broker is not None else "case",
        broker_socket=work_dir / "pi" / BROKER_SOCKET if broker is not None else None,
    )
    result = (
        _launch_with_broker(prepared, broker, timeout_s)
        if broker is not None
        else launch(prepared, timeout_s=timeout_s)
    )
    if not (result.spool_dir / SPOOL_EVENTS).is_file():
        raise ReplayMismatch(f"replay of {run_id} wrote no spool: {result.stderr[-2000:]}")
    lines = _read_spool(result.spool_dir, result.spool_key).lines
    end = lines[-1] if lines and lines[-1].type == "spool.end" else None
    if end is None or end.status != recorded_status:
        raise ReplayMismatch(
            f"replay of {run_id} ended {end.status if end else 'without spool.end'}"
            + (f": {end.reason}" if end and end.reason else "")
            + f"; the recording ended {recorded_status}"
        )
    requests = [ln for ln in lines if ln.type == "provider.request"]
    actual = [ln.replay_key for ln in requests if ln.purpose == "agent"]
    if actual != recorded["agent"]:
        raise ReplayMismatch(f"replay of {run_id}: request keys differ from the recording")
    for purpose, role in (("reducer", "reducer"), ("summary", "compaction-summary")):
        n = sum(ln.purpose == purpose for ln in requests)
        if n != len(recorded[role]):
            raise ReplayMismatch(
                f"replay of {run_id}: {n} {role} requests, the recording has {len(recorded[role])}"
            )
    _check_children(store, run_id, result.spool_dir, result.spool_key)
    claims = [ln.claim for ln in lines if ln.type == "task.claim"]
    recorded_claims = [e for e in store.events(run_id) if e.type == "pi.claim"]
    replayed_claim = "sha256:" + claims[-1] if claims else None
    recorded_claim = recorded_claims[-1].payload_refs["claim"] if recorded_claims else None
    if replayed_claim != recorded_claim:
        raise ReplayMismatch(f"replay of {run_id}: the claim differs from the recording")
    report = {
        "run_id": run_id,
        "requests": len(actual),
        "reducer_requests": len(recorded["reducer"]),
        "summary_requests": len(recorded["compaction-summary"]),
        "end_status": recorded_status,
        "claim": recorded_claim,
        "status": "match",
    }
    if broker is not None:
        replayed_jobs = read_journal(broker.journal_path)
        if any(e.executed_here for e in replayed_jobs):  # replay mode cannot; checked anyway
            raise ReplayMismatch(f"replay of {run_id} executed a job")
        if _jobs_served(replayed_jobs) != _jobs_served(recorded_jobs):
            raise ReplayMismatch(f"replay of {run_id}: the broker served different jobs")
        report.update(jobs=len(_jobs_served(replayed_jobs)), executions=0)
    return report
