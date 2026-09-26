"""One Pi dev-set run end to end: stage, commit, launch, ingest; and replay.

`run_case` commits every byte the agent may see (case input, pinned output,
allowed docs, ladder) and the script to the store *before* Pi starts, so the
spool's later references resolve to objects already committed. It then
launches the pinned Pi process, ingests the spool, and returns the result.
The Pi process never writes to the store (A37).

`replay_run_pi` re-runs a recorded run under its own recorded responses
(`ingest.recorded_script`) in a scratch directory, without writing to the
store, and raises `ReplayMismatch` unless the replay settles with the same
replay key for every request and the same claim (A34). Only scripted-mode
responses are replayed; nothing falls back to a live provider.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onoda.adapters.pi.contract import (
    SCRIPTED_MODEL,
    SPOOL_EVENTS,
    VARIANTS,
    Script,
    parse_spool_line,
)
from onoda.adapters.pi.ingest import IngestResult, ingest_spool, recorded_script
from onoda.adapters.pi.runtime import LiveSettings, build_launch, launch
from onoda.adapters.pi.task import stage_bundle
from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import CapabilityError, OnodaError, ReplayMismatch
from onoda.records import TaskManifest
from onoda.science.contract import LoadedCase
from onoda.store import Store, check_safe_id

DEFAULT_INSTRUCTION = (
    "Read the ORCA input with case_input, obtain its output with orca_run, then call "
    "report_states once with the final single-point energy and every excited state the "
    "output lists, copying each value and unit exactly as printed."
)


@dataclass(frozen=True)
class PiRun:
    ingest: IngestResult
    exit_code: int
    stderr_tail: str


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
    task = TaskManifest.new(
        task_id=f"pi-{m.case_id}-{sha256_digest(canonical_bytes(inputs))[7:19]}",
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
    max_requests: int = 16,
    max_acquisitions: int = 1,
    timeout_s: float = 300,
) -> PiRun:
    """Run one variant on one case and ingest it as `run_id`."""
    check_safe_id(run_id, "run id")
    if variant_id not in VARIANTS:
        raise OnodaError(f"unknown variant {variant_id!r}; see `onoda pi variants`")
    if store.has_record("run", run_id):
        raise OnodaError(f"run {run_id} already exists")
    work_dir = Path(work_dir)
    bundle_dir = work_dir / "bundle"
    stage_bundle(
        case,
        bundle_dir,
        instruction=instruction,
        docs=docs,
        ladder=ladder,
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
    )
    # Refusals happen above; only a launchable run commits anything.
    task = _commit_task(
        store,
        case,
        variant_id=variant_id,
        docs=docs,
        ladder=ladder,
        synthetic_ladder=synthetic_ladder,
    )
    config: dict[str, Any] = {
        "mode": "pi-scripted" if script is not None else "pi-live",
        "variant": variant_id,
        "case_id": case.manifest.case_id,
        "instruction": instruction,
        "max_requests": max_requests,
        "max_acquisitions": max_acquisitions,
    }
    if script is not None:
        config["script"] = store.put_json(
            script.model_dump(mode="json", by_alias=True), labels=["pi-script", "synthetic"]
        ).digest
    else:
        assert live is not None
        config.update(
            provider=live.provider,
            model=live.model,
            max_cost_usd=live.max_cost_usd,
            live_max_requests=live.max_requests,
        )
    result = launch(prepared, timeout_s=timeout_s)
    if not (result.spool_dir / SPOOL_EVENTS).is_file():
        raise OnodaError(
            f"pi exited {result.exit_code} before writing a spool: {result.stderr[-2000:]}"
        )
    ingested = ingest_spool(store, result.spool_dir, run_id=run_id, task=task, config=config)
    return PiRun(ingested, result.exit_code, result.stderr[-2000:])


def _spool_lines(spool_dir: Path) -> list[Any]:
    lines = (spool_dir / SPOOL_EVENTS).read_text().splitlines()
    return [parse_spool_line(json.loads(line)) for line in lines if line.strip()]


def replay_run_pi(
    store: Store, run_id: str, case: LoadedCase, *, work_dir: Path, timeout_s: float = 300
) -> dict[str, Any]:
    """Re-run a scripted Pi run under its recorded responses; raise on any drift."""
    run = store.run_record(run_id)
    cfg = run.config
    if cfg.get("mode") != "pi-scripted":
        raise CapabilityError(f"run {run_id} is {cfg.get('mode')!r}; only pi-scripted replays")
    if cfg.get("case_id") != case.manifest.case_id:
        raise OnodaError(f"run {run_id} was recorded from case {cfg.get('case_id')!r}")
    task = store.get_record("task", run.task)
    docs = {
        k.removeprefix("doc:"): store.get_bytes(d)
        for k, d in task.inputs.items()
        if k.startswith("doc:")
    }
    ladder = store.get_bytes(task.inputs["ladder"]) if "ladder" in task.inputs else None
    ladder_labels = store.descriptor(task.inputs["ladder"]).labels if ladder else []
    script = recorded_script(store, run_id)
    work_dir = Path(work_dir)
    stage_bundle(
        case,
        work_dir / "bundle",
        instruction=cfg["instruction"],
        docs=docs,
        ladder=ladder,
        synthetic_ladder="synthetic" in ladder_labels,
    )
    prepared = build_launch(
        variant_id=cfg["variant"],
        bundle_dir=work_dir / "bundle",
        work_dir=work_dir / "pi",
        script=script,
        max_requests=cfg["max_requests"],
        max_acquisitions=cfg["max_acquisitions"],
    )
    result = launch(prepared, timeout_s=timeout_s)
    if not (result.spool_dir / SPOOL_EVENTS).is_file():
        raise ReplayMismatch(f"replay of {run_id} wrote no spool: {result.stderr[-2000:]}")
    lines = _spool_lines(result.spool_dir)
    end = lines[-1] if lines and lines[-1].type == "spool.end" else None
    if end is None or end.status != "settled":
        raise ReplayMismatch(
            f"replay of {run_id} ended {end.status if end else 'without spool.end'}"
            + (f": {end.reason}" if end and end.reason else "")
        )
    expected = [t.expect_replay_key for t in script.turns]
    actual = [
        ln.replay_key
        for ln in lines
        if ln.type == "provider.request" and ln.model == SCRIPTED_MODEL
    ]
    if actual != expected:
        raise ReplayMismatch(f"replay of {run_id}: request keys differ from the recording")
    claims = [ln.claim for ln in lines if ln.type == "task.claim"]
    recorded = [e for e in store.events(run_id) if e.type == "pi.claim"]
    replayed_claim = "sha256:" + claims[-1] if claims else None
    recorded_claim = recorded[-1].payload_refs["claim"] if recorded else None
    if replayed_claim != recorded_claim:
        raise ReplayMismatch(f"replay of {run_id}: the claim differs from the recording")
    return {
        "run_id": run_id,
        "requests": len(actual),
        "reducer_requests": len(script.reducer_turns),
        "claim": recorded_claim,
        "status": "match",
    }
