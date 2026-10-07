"""`onoda` command line (PORTABLE_RUN_SPEC §14). Every command prints JSON.

The mock scheduler stands in for an external system, so its state lives beside
the store (`<store>.scheduler/`), never inside it, and is never exported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import yaml

from onoda.errors import OnodaError
from onoda.store import Store

if TYPE_CHECKING:
    from pydantic import BaseModel

_M = TypeVar("_M", bound="BaseModel")


def _store(path: str, *, evaluator: bool = False) -> Store:
    return Store(path, allowed_visibility=("evaluator",) if evaluator else ("agent",))


def _controller(store_path: str):
    from onoda.controller import Controller
    from onoda.fixture import PROGRAMS
    from onoda.jobs import MockScheduler

    store = _store(store_path)
    scheduler = MockScheduler(Path(f"{Path(store_path).resolve()}.scheduler"), PROGRAMS)
    return Controller(store, scheduler)


def _fixture_only(store: Store, run_id: str, command: str) -> None:
    """Fixture commands read fixture-shaped records; a case run has none."""
    if store.run_record(run_id).config.get("case_id") is not None:
        raise OnodaError(
            f"run {run_id} is an ORCA case run; `onoda {command}` handles fixture runs only "
            "(see `onoda case --help`, and `onoda pi fork` to fork a Pi run)"
        )


def _state_json(st) -> dict[str, Any]:
    return {
        "run_id": st.run_id,
        "phase": st.phase,
        "round": st.round,
        "boundary": st.boundary,
        "checkpoint": st.checkpoint,
        "final": st.visible.get("final"),
    }


def cmd_fixture_run(a) -> Any:
    c = _controller(a.store)
    return _state_json(c.run_fixture(a.run_id, policy=a.policy, parser=a.parser))


def cmd_checkpoint_list(a) -> Any:
    c = _controller(a.store)
    return [
        {
            "seq": seq,
            "checkpoint": digest,
            "boundary": cp.boundary,
            "next_phase": cp.phase,
            "quiescent": cp.quiescent,
            "capabilities": cp.capabilities,
        }
        for seq, digest, cp in c.checkpoints(a.run_id)
    ]


def cmd_export(a) -> Any:
    from onoda.portability import export_bundle

    evaluator = _store(a.evaluator_store, evaluator=True) if a.evaluator_store else None
    manifest = export_bundle(
        _store(a.store), a.run_ids, Path(a.out), profile=a.profile, evaluator_store=evaluator
    )
    return {
        "out": a.out,
        "profile": manifest["profile"],
        "roots": manifest["roots"],
        "files": len(manifest["inventory"]),
    }


def cmd_import(a) -> Any:
    from onoda.portability import import_bundle

    report = import_bundle(Path(a.bundle), _store(a.store))
    return {
        "bundle_digest": report.bundle_digest,
        "profile": report.profile,
        "roots": report.roots,
        "objects": report.objects,
        "records": report.records,
        "omissions": report.omissions,
        "exact_replay": report.exact_replay,
    }


def cmd_inspect(a) -> Any:
    from onoda.portability import inspect_capabilities
    from onoda.replay import replay_run

    store = _store(a.store)
    out: dict[str, Any] = {"run_id": a.run_id}
    if a.capabilities:
        out["capabilities"] = inspect_capabilities(store, a.run_id)
    else:
        out["history"] = replay_run(store, a.run_id)
    return out


def cmd_replay(a) -> Any:
    from onoda.replay import replay_run

    if not a.offline:
        raise OnodaError("replay is offline-only; pass --offline to acknowledge")
    return replay_run(_store(a.store), a.run_id)


def cmd_reanalyze(a) -> Any:
    from onoda.replay import reanalyze

    store = _store(a.store)
    _fixture_only(store, a.run_id, "reanalyze")
    return reanalyze(store, a.run_id, step=a.step, transform=a.transform)


def cmd_fork(a) -> Any:
    from onoda.forks import INTERVENTIONS, fork_run

    _fixture_only(_store(a.store), a.run_id, "fork")
    declaration = yaml.safe_load(Path(a.declaration).read_text())
    iv = declaration.get("intervention") or {}
    kind = iv.get("kind")
    required, optional, _ = INTERVENTIONS.get(kind, ("", (), ""))
    intervention = {"kind": kind}
    intervention |= {k: iv[k] for k in (required, *optional) if iv.get(k) is not None}
    st = fork_run(
        _controller(a.store),
        a.run_id,
        a.checkpoint,
        new_run_id=a.new_run,
        intervention=intervention,
        reason=declaration.get("reason", "cli fork"),
        declaration=declaration,
    )
    return _state_json(st)


def cmd_resume(a) -> Any:
    if a.mode != "fixture":
        raise OnodaError(f"resume mode {a.mode!r} unavailable: only fixture mode runs offline")
    _fixture_only(_store(a.store), a.run_id, "resume")
    return _state_json(_controller(a.store).resume(a.run_id))


def cmd_compare(a) -> Any:
    from onoda.budgets import cost_views, physical_cost

    store = _store(a.store)
    runs = []
    for run_id in a.run_ids:
        _fixture_only(store, run_id, "compare")
        run = store.run_record(run_id)
        final = None
        for e in reversed(store.events(run_id)):
            if e.type == "checkpoint" and e.payload.get("boundary") == "finalized":
                cp = store.checkpoint(e.payload_refs["checkpoint"].split("/", 1)[1])
                final = store.get_json(cp.visible["final"])
                break
        entry: dict[str, Any] = {
            "run_id": run_id,
            "parent": run.parent.model_dump() if run.parent else None,
            "policy": run.config.get("policy"),
            "actions": final["actions"] if final else None,
        }
        if a.include_cost_lineage:
            entry["costs"] = cost_views(store, run_id)
        runs.append(entry)
    forked = any(r["parent"] for r in runs)
    out: dict[str, Any] = {
        "comparison_kind": "conditional-branch-not-independent-full-run"
        if forked
        else "independent-runs",
        "labels": ["synthetic"],
        "runs": runs,
    }
    if a.include_cost_lineage:
        out["physical_cost"] = physical_cost(store, a.run_ids)
    return out


def cmd_evaluate(a) -> Any:
    from onoda.evaluation import evaluate_run, publish_reference

    store = _store(a.store)
    _fixture_only(store, a.run_id, "evaluate")
    evaluator = _store(a.evaluator_store, evaluator=True)
    task = store.get_record("task", store.run_record(a.run_id).task)
    params = store.get_json(task.inputs["params"])
    reference = publish_reference(evaluator, params)
    ev = evaluate_run(store, evaluator, a.run_id, grader=a.grader, reference=reference)
    return {
        "evaluation_id": ev.evaluation_id,
        "evaluator": ev.evaluator,
        "evaluator_version": ev.evaluator_version,
        "assignments": ev.assignments,
        "unresolved": ev.unresolved,
        "metrics": ev.metrics,
    }


def _case(case_id: str, cases_root: str | None):
    from onoda.science.contract import CASES_ROOT, case_manifests, load_case

    manifests = case_manifests(Path(cases_root) if cases_root else CASES_ROOT)
    if case_id not in manifests:
        raise OnodaError(f"unknown case {case_id!r}; see `onoda case list`")
    return load_case(manifests[case_id])


def cmd_case_list(a) -> Any:
    from onoda.science.contract import CASES_ROOT, case_manifests, load_case

    out = []
    for path in case_manifests(Path(a.cases_root) if a.cases_root else CASES_ROOT).values():
        m = load_case(path).manifest
        out.append(
            {
                "case_id": m.case_id,
                "program": f"{m.program} {m.program_version}",
                "method": m.method,
                "review": m.review.status,
                "executed_here": m.provenance.executed_here,
                "source": f"{m.provenance.repository}@{m.provenance.commit[:12]}",
            }
        )
    return out


def cmd_case_ingest(a) -> Any:
    from onoda.science.runner import record_case_run

    case = _case(a.case_id, a.cases_root)
    scheduler = None
    if a.execute:
        from onoda.science.orca_local import OrcaLocalScheduler, OrcaSettings

        scheduler = OrcaLocalScheduler(
            OrcaSettings(
                enabled=a.enable_live_execution,
                binary=a.orca_binary,
                work_root=a.work_root,
                max_wall_seconds=a.max_wall_seconds,
                stand_in=a.stand_in,
            )
        )
    cr = record_case_run(_store(a.store), case, a.run_id, scheduler=scheduler)
    obs = _store(a.store).get_json(cr.observation)
    return {
        "run_id": cr.run_id,
        "case_id": cr.case_id,
        "mode": cr.mode,
        "observation": cr.observation,
        "termination": obs["termination"],
        "scf": obs["scf"],
        "excited_states_status": obs["excited_states_status"],
        "states": len(obs["excited_states"]),
        "problems": obs["problems"],
    }


def cmd_case_evaluate(a) -> Any:
    from onoda.science.runner import evaluate_case_run

    store = _store(a.store)
    case_id = store.run_record(a.run_id).config.get("case_id")
    if case_id is None:
        raise OnodaError(f"run {a.run_id} is not a case run")
    ev = evaluate_case_run(
        store,
        _store(a.evaluator_store, evaluator=True),
        a.run_id,
        _case(case_id, a.cases_root),
        policy=a.policy,
        reference=_case(a.reference_case, a.cases_root) if a.reference_case else None,
    )
    return {
        "evaluation_id": ev.evaluation_id,
        "evaluator": ev.evaluator,
        "evaluator_version": ev.evaluator_version,
        "reference_version": ev.reference_version,
        "assignments": ev.assignments,
        "unresolved": ev.unresolved,
        "metrics": ev.metrics,
    }


def cmd_pi_doctor(a) -> Any:
    from onoda.adapters.pi.runtime import doctor

    report = doctor()
    if not report["ok"]:
        raise OnodaError("pinned Pi runtime is not usable: " + "; ".join(report["problems"]))
    return report


def cmd_pi_variants(a) -> Any:
    from onoda.adapters.pi.contract import VARIANTS

    return [
        {
            "id": v.id,
            "question": v.question,
            "ladder": v.ladder,
            "rlm": v.rlm,
            "mechanisms": list(v.mechanisms),
        }
        for v in VARIANTS.values()
    ]


def _live_settings(a):
    from onoda.adapters.pi.runtime import LiveSettings

    if not a.live:
        return None
    return LiveSettings(
        provider=a.provider,
        model=a.model,
        credential_env=a.credential_env,
        max_requests=a.max_requests,
        max_cost_usd=a.max_cost_usd,
    )


def _pi_run_summary(pi) -> dict[str, Any]:
    r = pi.ingest
    return {
        "run_id": r.run_id,
        "status": r.status,
        "complete": r.complete,
        "invocations": len(r.invocations),
        "claim": r.claim,
        "observation_pack": r.observation_pack,
        "pi_exit_code": pi.exit_code,
    }


def cmd_pi_run(a) -> Any:
    from onoda.adapters.pi.contract import HarnessCandidate, Script
    from onoda.adapters.pi.run import DEFAULT_INSTRUCTION, run_case
    from onoda.adapters.pi.runtime import detect_sandbox

    if bool(a.script) == bool(a.live):
        raise OnodaError("give exactly one of --script (offline) or --live")
    if a.max_requests is not None and a.max_requests <= 0:
        raise OnodaError("--max-requests must be greater than 0")
    if a.max_acquisitions < 0:
        raise OnodaError("--max-acquisitions must be 0 or more")
    if bool(a.reducer_provider) != bool(a.reducer_model):
        raise OnodaError("give both --reducer-provider and --reducer-model, or neither")
    script = Script.model_validate_json(Path(a.script).read_text()) if a.script else None
    live = _live_settings(a)
    pi = run_case(
        _store(a.store),
        _case(a.case_id, a.cases_root),
        run_id=a.run_id,
        variant_id=a.variant,
        work_dir=Path(a.work_dir),
        script=script,
        live=live,
        instruction=a.instruction or DEFAULT_INSTRUCTION,
        docs={Path(d).name: Path(d).read_bytes() for d in a.doc},
        ladder=Path(a.ladder).read_bytes() if a.ladder else None,
        synthetic_ladder=a.synthetic_ladder,
        # A live run's request cap is its own explicit setting (A35); never defaulted.
        max_requests=a.max_requests,
        max_acquisitions=a.max_acquisitions,
        reducer=(a.reducer_provider, a.reducer_model) if a.reducer_provider else None,
        timeout_s=a.timeout,
        candidate=(
            HarnessCandidate.model_validate_json(Path(a.candidate).read_text())
            if a.candidate
            else None
        ),
        sandbox=detect_sandbox() if (a.live or a.sandbox) else None,
    )
    return _pi_run_summary(pi)


def _vte_case(case_id: str, cases_root: str | None):
    from onoda.adapters.pi.run import VTE_CASES_ROOT
    from onoda.science.vte import load_vte_case, vte_case_manifests

    manifests = vte_case_manifests(Path(cases_root) if cases_root else VTE_CASES_ROOT)
    if case_id not in manifests:
        raise OnodaError(f"unknown VTE case {case_id!r} under {cases_root or VTE_CASES_ROOT}")
    return load_vte_case(manifests[case_id])


def cmd_pi_run_vte(a) -> Any:
    from onoda.adapters.pi.contract import Script
    from onoda.adapters.pi.run import VTE_INSTRUCTION, run_vte_case
    from onoda.adapters.pi.runtime import detect_sandbox
    from onoda.science.orca_broker import BrokerLimits
    from onoda.science.orca_local import OrcaSettings

    if bool(a.script) == bool(a.live):
        raise OnodaError("give exactly one of --script (offline) or --live")
    if a.max_requests is not None and a.max_requests <= 0:
        raise OnodaError("--max-requests must be greater than 0")
    if a.max_executions <= 0:
        raise OnodaError("--max-executions must be greater than 0")
    if bool(a.reducer_provider) != bool(a.reducer_model):
        raise OnodaError("give both --reducer-provider and --reducer-model, or neither")
    live = _live_settings(a)
    pi = run_vte_case(
        _store(a.store),
        _vte_case(a.case_id, a.cases_root),
        run_id=a.run_id,
        variant_id=a.variant,
        work_dir=Path(a.work_dir),
        # Every field defaults off; OrcaLocalScheduler refuses unresolved settings.
        orca=OrcaSettings(
            enabled=a.enable_live_execution,
            binary=a.orca_binary,
            work_root=a.orca_work_root,
            max_wall_seconds=a.max_wall_seconds,
            stand_in=a.stand_in,
        ),
        cache_dir=Path(a.orca_cache) if a.orca_cache else None,
        script=Script.model_validate_json(Path(a.script).read_text()) if a.script else None,
        live=live,
        instruction=a.instruction or VTE_INSTRUCTION,
        docs={Path(d).name: Path(d).read_bytes() for d in a.doc},
        ladder=Path(a.ladder).read_bytes() if a.ladder else None,
        synthetic_ladder=a.synthetic_ladder,
        max_requests=a.max_requests,
        max_executions=a.max_executions,
        reducer=(a.reducer_provider, a.reducer_model) if a.reducer_provider else None,
        timeout_s=a.timeout,
        sandbox=detect_sandbox() if (a.live or a.sandbox) else None,
        orca_mpi_home=Path(a.orca_mpi_home) if a.orca_mpi_home else None,
        report_reminder=a.report_reminder,
        limits=BrokerLimits(
            max_nprocs=a.orca_max_nprocs,
            max_maxcore_mb=a.orca_max_maxcore_mb,
            max_output_bytes=a.orca_max_output_mib << 20,
        ),
    )
    return _pi_run_summary(pi)


def cmd_pi_fork(a) -> Any:
    from onoda.adapters.pi.contract import Script
    from onoda.adapters.pi.fork import fork_pi_run
    from onoda.adapters.pi.runtime import detect_sandbox
    from onoda.science.orca_local import OrcaSettings

    if bool(a.script) == bool(a.live):
        raise OnodaError("give exactly one of --script (offline) or --live")
    if a.live and a.fresh_allowance:
        raise OnodaError("a --live fork always gets its own caps; drop --fresh-allowance")
    store = _store(a.store)
    cfg = store.run_record(a.run_id).config
    vte = cfg.get("task_kind") == "vte"
    live = _live_settings(a)
    pi = fork_pi_run(
        store,
        a.run_id,
        a.checkpoint,
        _vte_case(cfg.get("case_id"), a.cases_root)
        if vte
        else _case(cfg.get("case_id"), a.cases_root),
        new_run_id=a.new_run,
        continuation=Script.model_validate_json(Path(a.script).read_text()) if a.script else None,
        live=live,
        reason=a.reason,
        work_dir=Path(a.work_dir),
        budget_treatment="fresh-allowance" if a.fresh_allowance or live else "inherit-remaining",
        # Every field defaults off; OrcaLocalScheduler refuses unresolved settings.
        orca=OrcaSettings(
            enabled=a.enable_live_execution,
            binary=a.orca_binary,
            work_root=a.orca_work_root,
            max_wall_seconds=a.max_wall_seconds,
            stand_in=a.stand_in,
        )
        if vte
        else None,
        cache_dir=Path(a.orca_cache) if a.orca_cache else None,
        sandbox=detect_sandbox() if a.sandbox or live else None,
        timeout_s=a.timeout,
    )
    return {**_pi_run_summary(pi), "parent": a.run_id, "checkpoint": a.checkpoint}


def cmd_pi_evaluate_vte(a) -> Any:
    from onoda.adapters.pi.vte import evaluate_vte_run

    store = _store(a.store)
    run = store.run_record(a.run_id)
    if run.config.get("task_kind") != "vte":
        raise OnodaError(f"run {a.run_id} is not a VTE run; see `onoda pi evaluate`")
    ev = evaluate_vte_run(
        store,
        _store(a.evaluator_store, evaluator=True),
        a.run_id,
        _vte_case(run.config["case_id"], a.cases_root),
    )
    m = ev.metrics
    return {
        "evaluation_id": ev.evaluation_id,
        "evaluator": ev.evaluator,
        "reference_version": ev.reference_version,
        "coverage": m["coverage"],
        "coverage_total": m["coverage.total"],
        "mae_ev": m["mae_ev"],
        "false_success_claims": m["false_success_claims"],
        "assignments": ev.assignments,
        "synthetic": m["synthetic"] == "true",
    }


def cmd_vte_audit(a) -> Any:
    from onoda.adapters.pi.vte_audit import audit_runs
    from onoda.science.vte import load_vte_references

    if (a.store is None) == (a.run_stores is None):
        raise OnodaError("give exactly one of --store and --run-stores")

    def store_for(run_id: str) -> Store:
        return _store(a.store or str(Path(a.run_stores) / run_id / "store"))

    def case_for(case_id: str):
        case = _vte_case(case_id, a.cases_root)
        return case, load_vte_references(case)

    report = audit_runs(((store_for(r), r) for r in a.run_ids), case_for)
    return report.as_dict()


def cmd_vte_report(a) -> Any:
    from onoda.adapters.pi.vte import vte_report

    if a.arm and len(a.arm) != 2:
        raise OnodaError("--arm names exactly two arms, or none")
    from onoda.adapters.pi.vte_shadow import shadow_report

    if a.run_stores:  # one store pair per run (`run.sh` with PARALLEL > 1)
        if a.store or a.evaluator_store:
            raise OnodaError("--run-stores replaces --store and --evaluator-store")
        from onoda.adapters.pi.vte import vte_report_runs

        root = Path(a.run_stores)
        runs = [
            (_store(root / r / "store"), _store(root / r / "evaluator", evaluator=True), r)
            for r in a.run_ids
        ]
        return vte_report_runs(runs, arms=tuple(a.arm or ()))
    if not (a.store and a.evaluator_store):
        raise OnodaError("give --store and --evaluator-store, or --run-stores")
    store, evaluator_store = _store(a.store), _store(a.evaluator_store, evaluator=True)
    report = vte_report(store, evaluator_store, a.run_ids, arms=tuple(a.arm or ()))
    shadow = shadow_report(store, evaluator_store, a.run_ids)
    if shadow["runs"]:
        report["shadow"] = shadow
    from onoda.adapters.pi.vte_memory_shadow import memory_shadow_report

    memory = memory_shadow_report(store, evaluator_store, a.run_ids)
    if memory["arms"]:
        report["memory_shadow"] = memory
    return report


def cmd_vte_memory_shadow(a) -> Any:
    from onoda.adapters.pi import vte_memory_shadow as ms
    from onoda.adapters.pi.contract import REPO_ROOT

    store, evaluator_store = _store(a.store), _store(a.evaluator_store, evaluator=True)
    memory_dir = Path(a.memory_dir or REPO_ROOT / "experiments/questdb/memory")
    tiers = {p.name: p.read_text() for p in sorted(memory_dir.glob("memory-*.md"))}
    if not tiers:
        raise OnodaError(f"no memory-*.md tiers in {memory_dir}")
    seed_runs = a.seed_run or []
    ms.check_order(store, seed_runs, a.run_ids)
    kypp = ms.Kypp(a.kypp, Path(a.kypp_db))
    lessons = ms.seed(kypp, store, evaluator_store, tiers, seed_runs)
    out = []
    for run_id in a.run_ids:
        ev = ms.memory_shadow(
            store, evaluator_store, run_id, kypp,
            tiers=tiers, lessons=lessons, seed_runs=seed_runs, limit=a.limit,
        )  # fmt: skip
        out.append({"evaluation": ev.evaluation_id, **ev.metrics})
    return {"seed_lessons": len(lessons), "runs": out}


def cmd_vte_shadow(a) -> Any:
    from onoda.adapters.pi.vte_shadow import shadow_run
    from onoda.systemone import LiveSystemOne

    model = LiveSystemOne(a.provider, model=a.model, enabled=a.enable_live_decisions)
    store = _store(a.store)
    case_id = store.run_record(a.run_id).config.get("case_id")
    ev = shadow_run(
        store,
        _store(a.evaluator_store, evaluator=True),
        a.run_id,
        _vte_case(case_id, a.cases_root),
        model,
        ladder=_ladder(a.ladder),
        instructions=json.loads(Path(a.instructions).read_text()) if a.instructions else None,
    )
    return {"evaluation": ev.evaluation_id, **ev.metrics, "cost_usd": ev.usage.cost_usd}


def _ladder(path: str | None) -> bytes:
    from onoda.adapters.pi.contract import REPO_ROOT

    return Path(path or REPO_ROOT / "experiments/questdb/ladder-from-paper.md").read_bytes()


def cmd_vte_shadow_questions(a) -> Any:
    from onoda.adapters.pi.vte_shadow import questions

    Path(a.out).write_text(json.dumps(questions(_ladder(a.ladder)), indent=2, sort_keys=True))
    return {"questions": a.out}


def cmd_vte_shadow_dataset(a) -> Any:
    from onoda.adapters.pi.vte_shadow import shadow_examples
    from onoda.decision_tune import dump_dataset

    store = _store(a.store)
    runs = [
        (rid, _vte_case(store.run_record(rid).config.get("case_id"), a.cases_root))
        for rid in a.run_ids
    ]
    examples = shadow_examples(store, _store(a.evaluator_store, evaluator=True), runs)
    Path(a.out).write_text(dump_dataset(examples))
    labels = [e.labels["ready"] for e in examples]
    return {"dataset": a.out, "examples": len(examples), "ready_yes": labels.count("yes")}


def cmd_decide_tune(a) -> Any:
    from decimal import Decimal

    from onoda.decision_tune import Spend, load_dataset, tune
    from onoda.optimize.reflection import LiveReflection, ReflectionSettings
    from onoda.systemone import LiveSystemOne

    model = LiveSystemOne(a.provider, model=a.model, enabled=a.enable_live_decisions)
    reflect = LiveReflection(
        ReflectionSettings(
            provider="openrouter",
            model=a.reflection_model,
            credential_env=a.reflection_credential_env,
            max_tokens=a.reflection_max_tokens,
            max_cost_usd=a.reflection_max_cost_usd,
        )
    )
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    result = tune(
        questions=json.loads(Path(a.questions).read_text()),
        examples=load_dataset(a.dataset),
        model=model,
        reflect=reflect,
        reflection_cost=lambda: (reflect.last_receipt or {}).get("cost_usd"),
        spend=Spend(max_decision_calls=a.max_decision_calls, max_cost_usd=Decimal(a.max_cost_usd)),
        cache_dir=out / "cache",
        max_reflections=a.max_reflections,
        val_fraction=Decimal(a.val_fraction),
        minibatch_size=a.minibatch_size,
        seed=a.seed,
    )
    (out / "result.json").write_text(json.dumps(result.as_json(), indent=2, sort_keys=True))
    (out / "best-instructions.json").write_text(json.dumps(result.best, indent=2, sort_keys=True))
    return {
        "result": str(out / "result.json"),
        "validation_score": result.scores,
        "stop_reason": result.stop_reason,
        **result.spend,
    }


def cmd_pi_replay(a) -> Any:
    from onoda.adapters.pi.run import replay_run_pi

    store = _store(a.store)
    cfg = store.run_record(a.run_id).config
    case_id = cfg.get("case_id")
    vte = cfg.get("task_kind") == "vte"
    return replay_run_pi(
        store,
        a.run_id,
        _vte_case(case_id, a.cases_root) if vte else _case(case_id, a.cases_root),
        work_dir=Path(a.work_dir),
        timeout_s=a.timeout,
    )


def cmd_pi_evaluate(a) -> Any:
    from onoda.adapters.pi.ingest import run_claim
    from onoda.adapters.pi.task import evaluate_claims

    store = _store(a.store)
    run = store.run_record(a.run_id)
    if run.config.get("mode") not in ("pi-scripted", "pi-live"):
        raise OnodaError(f"run {a.run_id} is not a Pi run")
    claims = [e for e in store.events(a.run_id) if e.type == "pi.claim"]
    ev = evaluate_claims(
        _store(a.evaluator_store, evaluator=True),
        run_id=a.run_id,
        case=_case(run.config["case_id"], a.cases_root),
        claim=run_claim(store, a.run_id),
        claim_ref=claims[-1].payload_refs["claim"] if claims else None,
    )
    return {
        "evaluation_id": ev.evaluation_id,
        "evaluator": ev.evaluator,
        "reference_version": ev.reference_version,
        "metrics": ev.metrics,
        "unresolved": ev.unresolved,
        "synthetic_run": "synthetic" in run.labels,
    }


def _read_model(cls: type[_M], path: str) -> _M:
    text = Path(path).read_text()
    data = yaml.safe_load(text) if path.endswith((".yaml", ".yml")) else json.loads(text)
    try:
        return cls.model_validate(data)
    except ValueError as exc:
        raise OnodaError(f"{path}: {exc}") from exc


def cmd_optimize_seed(a) -> Any:
    from onoda.adapters.pi.contract import SEED_CANDIDATE, SEED_PROVENANCE

    return {
        "components": SEED_CANDIDATE.components,
        "digest": SEED_CANDIDATE.digest(),
        "provenance": SEED_PROVENANCE,
    }


def cmd_optimize_split(a) -> Any:
    from onoda.optimize.contract import SplitAssignment
    from onoda.science.contract import CASES_ROOT, case_manifests, load_case

    manifests = case_manifests(Path(a.cases_root) if a.cases_root else CASES_ROOT)
    ids = a.case or sorted(manifests)
    for cid in ids:
        if cid not in manifests:
            raise OnodaError(f"unknown case {cid!r}; see `onoda case list`")
    reviewed = all(load_case(manifests[c]).manifest.review.status == "reviewed" for c in ids)
    split = SplitAssignment.derive(
        ids, seed=a.seed, final_count=a.final_count, exploratory=not reviewed
    )
    return split.model_dump(mode="json", by_alias=True)


def cmd_optimize_run(a) -> Any:
    from onoda.adapters.pi.contract import SEED_CANDIDATE, HarnessCandidate
    from onoda.optimize.contract import GateSpec, SplitAssignment
    from onoda.optimize.loop import run_optimization
    from onoda.optimize.pi_runner import PiRunner
    from onoda.science.contract import CASES_ROOT, case_manifests, load_case

    if a.live and (a.scripts or a.reflection_script):
        raise OnodaError("--live takes no --scripts or --reflection-script")
    if not a.live and not (a.scripts and a.reflection_script):
        raise OnodaError("give --scripts and --reflection-script (offline, synthetic) or --live")
    gates = _read_model(GateSpec, a.gates)  # refuses unset tolerances (A47)
    split = _read_model(SplitAssignment, a.split)
    cases_root = Path(a.cases_root) if a.cases_root else CASES_ROOT
    manifests = case_manifests(cases_root)
    for cid in (*split.acceptance, *split.final):
        if cid not in manifests:
            raise OnodaError(f"split names unknown case {cid!r}")
    pending = [
        c for c in split.acceptance if load_case(manifests[c]).manifest.review.status != "reviewed"
    ]
    if pending and not split.exploratory:
        raise OnodaError(
            f"cases {', '.join(pending)} are not reviewed; the split must be exploratory (A48)"
        )
    if a.live:
        live, reflect, spend, sandbox = _live_search(a)
        scripts = None
    else:
        live = spend = sandbox = None
        scripts, reflect = _scripted_search(a, split.acceptance)

    seed = _read_model(HarnessCandidate, a.seed) if a.seed else SEED_CANDIDATE
    runner = PiRunner(
        store=_store(a.store),
        evaluator_store=_store(a.evaluator_store, evaluator=True),
        work_dir=Path(a.work_dir) / "trials",
        variant_id=a.variant,
        scripts=scripts,
        live=live,
        sandbox=sandbox,
        ladder=Path(a.ladder).read_bytes() if a.ladder else None,
        synthetic_ladder=a.synthetic_ladder,
        max_requests=a.max_requests,
        timeout_s=a.timeout,
        cases_root=cases_root,
    )
    result = run_optimization(
        runner=runner,
        seed=seed,
        split=split,
        gates=gates,
        reflect=reflect,
        store=_store(a.optimizer_store),
        search_id=a.search_id,
        run_dir=Path(a.work_dir) / "gepa",
        max_trials=a.max_trials,
        max_reflections=a.max_reflections,
        minibatch_size=a.minibatch_size,
        repeats=a.repeats,
        spend=spend,
    )
    return {
        "search_id": result.search_id,
        # scripted trials and a scripted reflection are synthetic (A32)
        "evidence": "live" if a.live else "synthetic",
        "exploratory_split": split.exploratory,
        "seed": result.seed,
        "accepted": [r.candidate for r in result.accepted],
        "non_dominated": sorted(result.non_dominated),
        "stop_reason": result.stop_reason,
        "trials": result.trials,
        "reflections": result.reflections,
        **({"spent_usd": result.spent_usd} if a.live else {}),
    }


def _scripted_search(a, acceptance) -> tuple[dict[str, Any], Any]:
    from onoda.adapters.pi.contract import Script

    scripts = {}
    for cid in acceptance:
        path = Path(a.scripts) / f"{cid}.json"
        if not path.is_file():
            raise OnodaError(f"no script for acceptance case {cid!r} at {path}")
        scripts[cid] = Script.model_validate_json(path.read_text())
    replies = json.loads(Path(a.reflection_script).read_text())
    if not (isinstance(replies, list) and all(isinstance(r, str) for r in replies)):
        raise OnodaError("--reflection-script must be a JSON list of strings (synthetic)")
    served = iter(replies)

    def reflect(prompt: str) -> str:
        try:
            return next(served)
        except StopIteration:
            raise OnodaError("the reflection script is exhausted") from None

    return scripts, reflect


def _live_search(a) -> tuple[Any, Any, Any, Any]:
    """Live agent settings, reflection, spend cap and sandbox; every flag explicit (A35, A51)."""
    from decimal import Decimal

    from onoda.adapters.pi.contract import DECIMAL_RE
    from onoda.adapters.pi.runtime import LiveSettings, detect_sandbox
    from onoda.optimize.adapter import SpendCap
    from onoda.optimize.reflection import LiveReflection, ReflectionSettings

    live = LiveSettings(
        provider=a.provider,
        model=a.model,
        credential_env=a.credential_env,
        max_requests=a.max_requests,
        max_cost_usd=a.max_cost_usd,
    )
    live.validate()
    reflect = LiveReflection(
        ReflectionSettings(
            provider=a.reflection_provider,
            model=a.reflection_model,
            credential_env=a.reflection_credential_env,
            max_tokens=a.reflection_max_tokens,
            max_cost_usd=a.reflection_max_cost_usd,
        )
    )
    if not a.search_max_cost_usd or not DECIMAL_RE.match(a.search_max_cost_usd):
        raise OnodaError("--live needs --search-max-cost-usd (decimal USD)")
    assert live.max_cost_usd is not None and a.reflection_max_cost_usd is not None
    spend = SpendCap(
        max_cost_usd=Decimal(a.search_max_cost_usd),
        trial_reserve=Decimal(live.max_cost_usd),
        reflection_reserve=Decimal(a.reflection_max_cost_usd),
    )
    return live, reflect, spend, detect_sandbox()


def cmd_optimize_compare(a) -> Any:
    """Gate check of --candidate against --baseline (default: the seed) on the acceptance slice."""
    from decimal import Decimal

    from onoda.adapters.pi.contract import DECIMAL_RE, SEED_CANDIDATE, HarnessCandidate
    from onoda.adapters.pi.runtime import LiveSettings, detect_sandbox
    from onoda.optimize.compare import compare_candidates
    from onoda.optimize.contract import GateSpec, SplitAssignment
    from onoda.optimize.pi_runner import PiRunner
    from onoda.science.contract import CASES_ROOT

    if not a.live:
        raise OnodaError("compare runs live only (a scripted check would compare the script)")
    gates = _read_model(GateSpec, a.gates)
    split = _read_model(SplitAssignment, a.split)
    baseline = _read_model(HarnessCandidate, a.baseline) if a.baseline else SEED_CANDIDATE
    candidate = _read_model(HarnessCandidate, a.candidate)
    live = LiveSettings(
        provider=a.provider,
        model=a.model,
        credential_env=a.credential_env,
        max_requests=a.max_requests,
        max_cost_usd=a.max_cost_usd,
    )
    live.validate()
    if not a.compare_max_cost_usd or not DECIMAL_RE.match(a.compare_max_cost_usd):
        raise OnodaError("--live needs --compare-max-cost-usd (decimal USD)")
    assert live.max_cost_usd is not None
    evaluator_store = _store(a.evaluator_store, evaluator=True)
    runner = PiRunner(
        store=_store(a.store),
        evaluator_store=evaluator_store,
        work_dir=Path(a.work_dir) / a.check_id,
        variant_id=a.variant,
        live=live,
        sandbox=detect_sandbox(),
        ladder=Path(a.ladder).read_bytes() if a.ladder else None,
        synthetic_ladder=a.synthetic_ladder,
        max_requests=a.max_requests,
        timeout_s=a.timeout,
        cases_root=Path(a.cases_root) if a.cases_root else CASES_ROOT,
    )
    check = compare_candidates(
        runner,
        check_id=a.check_id,
        baseline=baseline,
        candidate=candidate,
        cases=split.acceptance,  # never the final slice
        gates=gates,
        repeats=a.repeats,
        max_cost_usd=Decimal(a.compare_max_cost_usd),
        run_reserve_usd=Decimal(live.max_cost_usd),
    )
    check["exploratory_split"] = split.exploratory
    digest = evaluator_store.put_json(
        check, visibility="evaluator", labels=["promotion-check"]
    ).digest
    return {"check": digest, **check}


def cmd_optimize_status(a) -> Any:
    from onoda.optimize.contract import CandidateRecord

    store = _store(a.optimizer_store)
    events = store.events(a.search_id)
    if not events:
        raise OnodaError(f"no search {a.search_id!r} in {a.optimizer_store}")
    candidates = []
    for e in events:
        if e.type == "optimizer.candidate":
            rec = CandidateRecord.model_validate(store.get_json(e.payload_refs["record"]))
            candidates.append(
                {
                    "iteration": rec.iteration,
                    "candidate": rec.candidate,
                    "origin": rec.origin,
                    "decision": rec.decision,
                    "reasons": list(rec.reasons),
                }
            )
    finals = [e.payload_refs["final"] for e in events if e.type == "optimizer.final"]
    return {
        "search_id": a.search_id,
        "trials": sum(e.type == "optimizer.trial" for e in events),
        "reflections": sum(e.type == "optimizer.reflection" for e in events),
        "candidates": candidates,
        "final": store.get_json(finals[-1]) if finals else None,
    }


def cmd_ablation_run(a) -> Any:
    from decimal import Decimal

    from onoda.ablation.run import run_ablation
    from onoda.adapters.pi.contract import DECIMAL_RE
    from onoda.adapters.pi.runtime import LiveSettings, detect_sandbox
    from onoda.science.contract import CASES_ROOT

    if bool(a.reducer_provider) != bool(a.reducer_model):
        raise OnodaError("give both --reducer-provider and --reducer-model, or neither")
    live = sandbox = cap = None
    if a.live:
        # Every live setting explicit (A35); nothing defaults.
        live = LiveSettings(
            provider=a.provider,
            model=a.model,
            credential_env=a.credential_env,
            max_requests=a.max_requests,
            max_cost_usd=a.max_cost_usd,
        )
        live.validate()
        if not a.ablation_max_cost_usd or not DECIMAL_RE.match(a.ablation_max_cost_usd):
            raise OnodaError("--live needs --ablation-max-cost-usd (decimal USD)")
        cap = Decimal(a.ablation_max_cost_usd)
        sandbox = detect_sandbox()
    digest, report = run_ablation(
        a.spec,
        store=_store(a.store),
        evaluator_store=_store(a.evaluator_store, evaluator=True),
        work_dir=Path(a.work_dir),
        cases_root=Path(a.cases_root) if a.cases_root else CASES_ROOT,
        timeout_s=a.timeout,
        live=live,
        reducer=(a.reducer_provider, a.reducer_model) if a.reducer_provider else None,
        sandbox=sandbox,
        max_cost_usd=cap,
    )
    if a.full:
        return {"report": digest, **report}
    return {
        "report": digest,
        "ablation_id": report["ablation_id"],
        "evidence": report["evidence"],
        "caveat": report["caveat"],
        "verdicts": {c["arm"]: c["verdict"] for c in report["comparisons"]},
        "non_dominated": report["non_dominated"],
        "rounds": report["rounds"],
        "stopped": report["stopped"],
        "spent_usd": report["spent_usd"],
    }


def cmd_ablation_show(a) -> Any:
    store = _store(a.evaluator_store, evaluator=True)
    if "ablation-report" not in store.descriptor(a.report).labels:
        raise OnodaError(f"{a.report} is not an ablation report")
    return store.get_json(a.report)


def build_parser() -> argparse.ArgumentParser:
    from onoda.adapters.pi.vte_contract import DEFAULT_MAX_EXECUTIONS
    from onoda.science.orca_broker import DEFAULT_LIMITS

    p = argparse.ArgumentParser(prog="onoda", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    fx = sub.add_parser("fixture").add_subparsers(dest="fixture_command", required=True)
    run = fx.add_parser("run", help="execute the synthetic fixture workflow")
    run.add_argument("--run-id", required=True)
    run.add_argument("--store", required=True)
    run.add_argument("--policy", default="mock-policy-v1")
    run.add_argument("--parser", default="fixture-parser@1")
    run.set_defaults(func=cmd_fixture_run)

    ck = sub.add_parser("checkpoint").add_subparsers(dest="checkpoint_command", required=True)
    ls = ck.add_parser("list")
    ls.add_argument("run_id")
    ls.add_argument("--store", required=True)
    ls.set_defaults(func=cmd_checkpoint_list)

    ex = sub.add_parser("export")
    ex.add_argument("run_ids", nargs="+")
    ex.add_argument("--store", required=True)
    ex.add_argument(
        "--profile", default="agent-visible", choices=["agent-visible", "evaluator", "redacted"]
    )
    ex.add_argument("--evaluator-store")
    ex.add_argument("--out", required=True)
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser("import")
    im.add_argument("bundle")
    im.add_argument("--store", required=True)
    im.set_defaults(func=cmd_import)

    ins = sub.add_parser("inspect")
    ins.add_argument("run_id")
    ins.add_argument("--store", required=True)
    ins.add_argument("--capabilities", action="store_true")
    ins.set_defaults(func=cmd_inspect)

    rp = sub.add_parser("replay")
    rp.add_argument("run_id")
    rp.add_argument("--store", required=True)
    rp.add_argument("--offline", action="store_true")
    rp.set_defaults(func=cmd_replay)

    ra = sub.add_parser("reanalyze")
    ra.add_argument("run_id")
    ra.add_argument("--store", required=True)
    ra.add_argument("--step", default="parse")
    ra.add_argument("--transform")
    ra.set_defaults(func=cmd_reanalyze)

    fk = sub.add_parser("fork")
    fk.add_argument("run_id")
    fk.add_argument("--checkpoint", required=True)
    fk.add_argument("--declaration", required=True)
    fk.add_argument("--new-run", required=True)
    fk.add_argument("--store", required=True)
    fk.set_defaults(func=cmd_fork)

    rs = sub.add_parser("resume")
    rs.add_argument("run_id")
    rs.add_argument("--store", required=True)
    rs.add_argument("--mode", default="fixture")
    rs.set_defaults(func=cmd_resume)

    cm = sub.add_parser("compare")
    cm.add_argument("run_ids", nargs="+")
    cm.add_argument("--store", required=True)
    cm.add_argument("--include-cost-lineage", action="store_true")
    cm.set_defaults(func=cmd_compare)

    ev = sub.add_parser("evaluate")
    ev.add_argument("run_id")
    ev.add_argument("--store", required=True)
    ev.add_argument("--evaluator-store", required=True)
    ev.add_argument("--grader", default="fixture-grader@1")
    ev.set_defaults(func=cmd_evaluate)

    cs = sub.add_parser("case", help="fixed-input ORCA smoke cases (M2)")
    cs = cs.add_subparsers(dest="case_command", required=True)
    cl = cs.add_parser("list", help="list pinned cases and their review status")
    cl.add_argument("--cases-root")
    cl.set_defaults(func=cmd_case_list)
    ci = cs.add_parser("ingest", help="record a case run: acquire pinned outputs, or --execute")
    ci.add_argument("case_id")
    ci.add_argument("--run-id", required=True)
    ci.add_argument("--store", required=True)
    ci.add_argument("--cases-root")
    ci.add_argument("--execute", action="store_true", help="run the input on a local ORCA")
    ci.add_argument("--enable-live-execution", action="store_true")
    ci.add_argument("--orca-binary")
    ci.add_argument("--work-root")
    ci.add_argument("--max-wall-seconds", type=int)
    ci.add_argument(
        "--stand-in", action="store_true", help="the binary is a test stand-in, not ORCA"
    )
    ci.set_defaults(func=cmd_case_ingest)
    ce = cs.add_parser("evaluate", help="grade a case run in a separate evaluator store")
    ce.add_argument("run_id")
    ce.add_argument("--store", required=True)
    ce.add_argument("--evaluator-store", required=True)
    ce.add_argument("--policy", default="orca-reexecution@1")
    ce.add_argument("--reference-case")
    ce.add_argument("--cases-root")
    ce.set_defaults(func=cmd_case_evaluate)

    pi = sub.add_parser("pi", help="Pi agent runs: variants S0-S3 and the RLM arm S1r")
    pi = pi.add_subparsers(dest="pi_command", required=True)
    pi.add_parser("doctor", help="check the pinned Pi/SoL-Pi install").set_defaults(
        func=cmd_pi_doctor
    )
    pi.add_parser("variants", help="list experiment arms").set_defaults(func=cmd_pi_variants)
    pr = pi.add_parser("run", help="run one variant on one case and ingest it")
    pr.add_argument("case_id")
    pr.add_argument("--variant", required=True)
    pr.add_argument("--run-id", required=True)
    pr.add_argument("--store", required=True)
    pr.add_argument("--work-dir", required=True)
    pr.add_argument("--script", help="scripted-provider script (offline; outputs synthetic)")
    pr.add_argument("--live", action="store_true", help="real provider; every live flag needed")
    pr.add_argument("--provider")
    pr.add_argument("--model")
    pr.add_argument("--credential-env", help="name of the env var holding the API key")
    pr.add_argument("--max-cost-usd")
    pr.add_argument("--max-requests", type=int)
    pr.add_argument("--max-acquisitions", type=int, default=1)
    pr.add_argument(
        "--sandbox", action="store_true", help="run Pi in the bwrap sandbox (always on for --live)"
    )
    pr.add_argument("--reducer-provider", help="reducer provider for S2b/S3 (live runs)")
    pr.add_argument("--reducer-model", help="reducer model for S2b/S3 (live runs)")
    pr.add_argument("--doc", action="append", default=[], help="allowed documentation (.md)")
    pr.add_argument("--ladder", help="advisory ladder document (required from S1 on)")
    pr.add_argument("--synthetic-ladder", action="store_true")
    pr.add_argument("--instruction")
    pr.add_argument("--timeout", type=float, default=300)
    pr.add_argument("--cases-root")
    pr.add_argument(
        "--candidate",
        help='HarnessCandidate JSON ({"components": {...}}); RLM variants default to the seed',
    )
    pr.set_defaults(func=cmd_pi_run)
    rv = pi.add_parser(
        "run-vte", help="run one variant on one VTE case, its ORCA jobs on this host, and ingest it"
    )
    rv.add_argument("case_id")
    rv.add_argument("--variant", required=True)
    rv.add_argument("--run-id", required=True)
    rv.add_argument("--store", required=True)
    rv.add_argument("--work-dir", required=True, help="short: the broker socket lives under it")
    rv.add_argument("--script", help="scripted-provider script (offline; outputs synthetic)")
    rv.add_argument("--live", action="store_true", help="real provider; every live flag needed")
    rv.add_argument("--provider")
    rv.add_argument("--model")
    rv.add_argument("--credential-env", help="name of the env var holding the API key")
    rv.add_argument("--max-cost-usd")
    rv.add_argument("--max-requests", type=int)
    rv.add_argument(
        "--sandbox", action="store_true", help="run Pi in the sandbox (always on for --live)"
    )
    rv.add_argument("--reducer-provider", help="reducer provider for S2b/S3 (live runs)")
    rv.add_argument("--reducer-model", help="reducer model for S2b/S3 (live runs)")
    rv.add_argument("--enable-live-execution", action="store_true", help="let the broker run ORCA")
    rv.add_argument("--orca-binary")
    rv.add_argument("--orca-work-root")
    rv.add_argument("--max-wall-seconds", type=int, help="per ORCA job")
    rv.add_argument(
        "--stand-in", action="store_true", help="the binary is a test stand-in, not ORCA"
    )
    rv.add_argument(
        "--max-executions",
        type=int,
        default=DEFAULT_MAX_EXECUTIONS,
        help="orca_submit calls per run, cache hits included (A59)",
    )
    rv.add_argument("--orca-cache", help="shared broker cache directory (A59)")
    rv.add_argument(
        "--report-reminder",
        action="store_true",
        help="tell the agent once to call report_vtes if it stops without a claim",
    )
    rv.add_argument("--orca-mpi-home", help="MPI prefix the ORCA job sandbox exposes (A65)")
    rv.add_argument(
        "--orca-max-nprocs",
        type=int,
        default=DEFAULT_LIMITS.max_nprocs,
        help="largest %%pal nprocs / PALn the broker accepts (A65)",
    )
    rv.add_argument(
        "--orca-max-maxcore-mb",
        type=int,
        default=DEFAULT_LIMITS.max_maxcore_mb,
        help="largest %%maxcore the broker accepts, MB per process (A65)",
    )
    rv.add_argument(
        "--orca-max-output-mib",
        type=int,
        default=DEFAULT_LIMITS.max_output_bytes >> 20,
        help="largest file an ORCA job may write, its log included, MiB (A65)",
    )
    rv.add_argument("--doc", action="append", default=[], help="allowed documentation (.md)")
    rv.add_argument("--ladder", help="advisory ladder document (required from S1 on)")
    rv.add_argument("--synthetic-ladder", action="store_true")
    rv.add_argument("--instruction")
    rv.add_argument("--timeout", type=float, default=3600, help="seconds for the whole Pi run")
    rv.add_argument("--cases-root", help="VTE cases (default cases-dev/questdb)")
    rv.set_defaults(func=cmd_pi_run_vte)
    pf = pi.add_parser(
        "fork",
        help="fork a recorded Pi run at a checkpoint: replay it there, then continue on a "
        "script or a live model",
    )
    pf.add_argument("run_id", help="the parent run")
    pf.add_argument("--checkpoint", required=True, help="see `onoda checkpoint list`")
    pf.add_argument("--new-run", required=True)
    pf.add_argument("--store", required=True)
    pf.add_argument("--work-dir", required=True, help="short for a VTE run: the broker socket")
    pf.add_argument("--script", help="the continuation (offline; synthetic)")
    pf.add_argument(
        "--live",
        action="store_true",
        help="continue on a real provider (every live flag needed; caps are on top of the prefix)",
    )
    pf.add_argument("--provider")
    pf.add_argument("--model")
    pf.add_argument("--credential-env", help="name of the env var holding the API key")
    pf.add_argument("--max-cost-usd")
    pf.add_argument("--max-requests", type=int, help="the live model's requests")
    pf.add_argument("--reason", required=True, help="why this fork, recorded with it")
    pf.add_argument(
        "--fresh-allowance",
        action="store_true",
        help="give the fork the parent's caps again on top of the replayed prefix",
    )
    pf.add_argument(
        "--sandbox", action="store_true", help="run Pi in the sandbox (always on for --live)"
    )
    pf.add_argument(
        "--enable-live-execution", action="store_true", help="VTE: let the broker run ORCA"
    )
    pf.add_argument("--orca-binary", help="VTE: the parent's ORCA binary (or stand-in)")
    pf.add_argument("--orca-work-root")
    pf.add_argument("--max-wall-seconds", type=int, help="per ORCA job")
    pf.add_argument("--stand-in", action="store_true", help="the binary is a test stand-in")
    pf.add_argument("--orca-cache", help="shared broker cache directory (A59)")
    pf.add_argument("--timeout", type=float, default=300)
    pf.add_argument("--cases-root")
    pf.set_defaults(func=cmd_pi_fork)
    rp2 = pi.add_parser("replay", help="re-run a scripted run; fails closed on any drift")
    rp2.add_argument("run_id")
    rp2.add_argument("--store", required=True)
    rp2.add_argument("--work-dir", required=True)
    rp2.add_argument("--timeout", type=float, default=300)
    rp2.add_argument("--cases-root", help="cases root (VTE runs: default cases-dev/questdb)")
    rp2.set_defaults(func=cmd_pi_replay)
    pe = pi.add_parser("evaluate", help="grade a Pi run's claim in a separate evaluator store")
    pe.add_argument("run_id")
    pe.add_argument("--store", required=True)
    pe.add_argument("--evaluator-store", required=True)
    pe.add_argument("--cases-root")
    pe.set_defaults(func=cmd_pi_evaluate)
    pev = pi.add_parser(
        "evaluate-vte", help="grade a VTE run's claim by state identity in the evaluator store"
    )
    pev.add_argument("run_id")
    pev.add_argument("--store", required=True)
    pev.add_argument("--evaluator-store", required=True)
    pev.add_argument("--cases-root", help="VTE cases (default cases-dev/questdb)")
    pev.set_defaults(func=cmd_pi_evaluate_vte)

    vt = sub.add_parser("vte", help="QUESTDB vertical-transition-energy runs")
    vt = vt.add_subparsers(dest="vte_command", required=True)
    vr = vt.add_parser(
        "report", help="coverage, MAE and false successes per arm over graded VTE runs"
    )
    vr.add_argument("run_ids", nargs="+")
    vr.add_argument("--store")
    vr.add_argument("--evaluator-store")
    vr.add_argument(
        "--run-stores",
        help="dir holding <run_id>/store and <run_id>/evaluator per run (parallel runs); "
        "the shadow sections need one shared store and are left out",
    )
    vr.add_argument(
        "--arm", action="append", help="compare these two arms (repeat once); default: all"
    )
    vr.set_defaults(func=cmd_vte_report)
    va = vt.add_parser(
        "audit",
        help="grade known-wrong variants of each run's supported claims and count what the "
        "grader still credits; writes nothing to the stores",
    )
    va.add_argument("run_ids", nargs="+")
    va.add_argument("--store", help="the runs' store")
    va.add_argument("--run-stores", help="parallel layout: DIR/<run>/store per run")
    va.add_argument("--cases-root", help="VTE cases (default cases-dev/questdb)")
    va.set_defaults(func=cmd_vte_audit)
    vs = vt.add_parser(
        "shadow",
        help="ask a System One model (Jev, Clef) which ladder rule each job of a run calls for; "
        "recorded beside the grade, never shown to the agent (A67)",
    )
    vs.add_argument("run_id")
    vs.add_argument("--store", required=True)
    vs.add_argument("--evaluator-store", required=True)
    vs.add_argument("--provider", required=True, choices=["jev", "clef"])
    vs.add_argument("--model", help="default: typesafe/jev-1.13 (jev), clef-flash (clef)")
    vs.add_argument(
        "--ladder",
        help="the ladder whose rules A-H are the choices (default: the reconstruction, A61)",
    )
    vs.add_argument("--cases-root")
    vs.add_argument(
        "--instructions", help="question wording from `onoda decide tune` (best-instructions.json)"
    )
    vs.add_argument("--enable-live-decisions", action="store_true")
    vs.set_defaults(func=cmd_vte_shadow)
    vm = vt.add_parser(
        "memory-shadow",
        help="seed a fresh kypp store from the memory tiers and earlier runs, ask it after every "
        "job of these runs, and score its recall against the tiers; never shown to the agent",
    )
    vm.add_argument("run_ids", nargs="+")
    vm.add_argument("--store", required=True)
    vm.add_argument("--evaluator-store", required=True)
    vm.add_argument(
        "--kypp-db", required=True, help="a new kypp database file (refused if it exists)"
    )
    vm.add_argument("--kypp", default="kypp", help="the kypp CLI (default: kypp on PATH)")
    vm.add_argument(
        "--seed-run",
        action="append",
        help="an earlier run (finished before every shadowed run started) to seed pitfalls from",
    )
    vm.add_argument("--memory-dir", help="default: experiments/questdb/memory")
    vm.add_argument("--limit", type=int, default=5, help="claims kypp returns per job (default 5)")
    vm.set_defaults(func=cmd_vte_memory_shadow)

    vq = vt.add_parser(
        "shadow-questions", help="write the shadow questions template (input to decide tune)"
    )
    vq.add_argument("--ladder")
    vq.add_argument("--out", required=True)
    vq.set_defaults(func=cmd_vte_shadow_questions)
    vd = vt.add_parser(
        "shadow-dataset",
        help="label each graded run's cited jobs ready yes/no from the grader (A70)",
    )
    vd.add_argument("run_ids", nargs="+")
    vd.add_argument("--store", required=True)
    vd.add_argument("--evaluator-store", required=True)
    vd.add_argument("--cases-root")
    vd.add_argument("--out", required=True, help="onoda.decision-example/1 JSONL")
    vd.set_defaults(func=cmd_vte_shadow_dataset)

    de = sub.add_parser("decide", help="System One decision models (Jev, Clef)")
    de = de.add_subparsers(dest="decide_command", required=True)
    dt = de.add_parser(
        "tune", help="hill-climb question wording against labelled examples with GEPA (A70)"
    )
    dt.add_argument("--dataset", required=True, help="onoda.decision-example/1 JSONL")
    dt.add_argument("--questions", required=True, help="questions template JSON")
    dt.add_argument("--provider", required=True, choices=["jev", "clef"])
    dt.add_argument("--model")
    dt.add_argument("--reflection-model", required=True, help="OpenRouter model id")
    dt.add_argument("--reflection-credential-env", default="OPENROUTER_API_KEY")
    dt.add_argument("--reflection-max-tokens", type=int, default=4000)
    dt.add_argument("--reflection-max-cost-usd", default="0.05", help="per reflection call")
    dt.add_argument("--max-reflections", type=int, required=True)
    dt.add_argument("--max-decision-calls", type=int, required=True)
    dt.add_argument("--max-cost-usd", required=True, help="whole search, both models")
    dt.add_argument("--val-fraction", default="0.3")
    dt.add_argument("--minibatch-size", type=int, default=4)
    dt.add_argument("--seed", type=int, default=0)
    dt.add_argument("--out", required=True, help="dir: result.json, best-instructions.json, cache")
    dt.add_argument("--enable-live-decisions", action="store_true")
    dt.set_defaults(func=cmd_decide_tune)

    ab = sub.add_parser("ablation", help="SoL-Pi add-one ablation over Pi arms (M3b)")
    ab = ab.add_subparsers(dest="ablation_command", required=True)
    ar = ab.add_parser("run", help="run every arm on every case and write the report")
    ar.add_argument("spec", help="onoda.ablation-spec/1 YAML")
    ar.add_argument("--store", required=True)
    ar.add_argument("--evaluator-store", required=True)
    ar.add_argument("--work-dir", required=True)
    ar.add_argument("--timeout", type=float, default=300, help="seconds per Pi run")
    ar.add_argument("--cases-root")
    ar.add_argument("--full", action="store_true", help="print the whole report")
    ar.add_argument("--live", action="store_true", help="real provider in the sandbox, no scripts")
    ar.add_argument("--provider", help="live: Pi provider for the agent")
    ar.add_argument("--model", help="live: agent model")
    ar.add_argument("--credential-env", help="live: env var holding the API key")
    ar.add_argument("--max-requests", type=int, help="live: request cap per run")
    ar.add_argument("--max-cost-usd", help="live: cost cap per run")
    ar.add_argument("--ablation-max-cost-usd", help="live: cost cap for the whole ablation")
    ar.add_argument("--reducer-provider", help="live: reducer provider for S2b/S3")
    ar.add_argument("--reducer-model", help="live: reducer model for S2b/S3")
    ar.set_defaults(func=cmd_ablation_run)
    ash = ab.add_parser("show", help="print a stored ablation report")
    ash.add_argument("report", help="report digest (sha256:...)")
    ash.add_argument("--evaluator-store", required=True)
    ash.set_defaults(func=cmd_ablation_show)

    op = sub.add_parser(
        "optimize", help="GEPA search over harness candidates with SoL-Pi gates (M4)"
    )
    op = op.add_subparsers(dest="optimize_command", required=True)
    op.add_parser("seed", help="print the seed candidate and its provenance").set_defaults(
        func=cmd_optimize_seed
    )
    osp = op.add_parser("split", help="derive an acceptance/final split (deterministic)")
    osp.add_argument("--case", action="append", help="case id (default: every case)")
    osp.add_argument("--seed", type=int, required=True)
    osp.add_argument("--final-count", type=int, required=True)
    osp.add_argument("--cases-root")
    osp.set_defaults(func=cmd_optimize_split)
    orun = op.add_parser("run", help="search from the seed over the acceptance slice")
    orun.add_argument("--split", required=True, help="onoda.split/1 JSON")
    orun.add_argument("--gates", required=True, help="onoda.gate-spec/2 YAML/JSON")
    orun.add_argument("--search-id", required=True)
    orun.add_argument("--store", required=True, help="agent store for the trial runs")
    orun.add_argument("--evaluator-store", required=True)
    orun.add_argument("--optimizer-store", required=True)
    orun.add_argument("--work-dir", required=True)
    orun.add_argument("--scripts", help="dir of <case_id>.json scripts (offline; synthetic)")
    orun.add_argument("--reflection-script", help="JSON list of reflection replies (synthetic)")
    orun.add_argument(
        "--live", action="store_true", help="real agent and reflection models (A51); flags below"
    )
    orun.add_argument("--provider", help="live: Pi provider for the agent")
    orun.add_argument("--model", help="live: agent model")
    orun.add_argument("--credential-env", help="live: env var holding the agent's API key")
    orun.add_argument("--max-cost-usd", help="live: cost cap per trial (one Pi run)")
    orun.add_argument("--reflection-provider", help="live: reflection provider (openrouter)")
    orun.add_argument("--reflection-model", help="live: reflection model")
    orun.add_argument("--reflection-credential-env", help="live: env var for reflection")
    orun.add_argument("--reflection-max-tokens", type=int, help="live: reply cap per call")
    orun.add_argument("--reflection-max-cost-usd", help="live: cost cap per reflection call")
    orun.add_argument("--search-max-cost-usd", help="live: cost cap for the whole search")
    orun.add_argument("--seed", help="HarnessCandidate JSON (default: the seed candidate)")
    orun.add_argument("--variant", default="S1r")
    orun.add_argument("--ladder")
    orun.add_argument("--synthetic-ladder", action="store_true")
    orun.add_argument("--max-trials", type=int, required=True, help="Pi runs, failed ones too")
    orun.add_argument("--max-reflections", type=int, required=True)
    orun.add_argument("--minibatch-size", type=int, default=2)
    orun.add_argument(
        "--repeats", type=int, default=1, help="runs per case per evaluation, pooled by the gates"
    )
    orun.add_argument("--max-requests", type=int)
    orun.add_argument("--timeout", type=float, default=300, help="seconds per Pi run")
    orun.add_argument("--cases-root")
    orun.set_defaults(func=cmd_optimize_run)
    ocmp = op.add_parser(
        "compare", help="gate check of one candidate against another (promotion, A54)"
    )
    ocmp.add_argument("--candidate", required=True, help="HarnessCandidate JSON")
    ocmp.add_argument("--baseline", help="HarnessCandidate JSON (default: the seed candidate)")
    ocmp.add_argument("--split", required=True, help="onoda.split/1 JSON; runs its acceptance")
    ocmp.add_argument("--gates", required=True, help="onoda.gate-spec/2 YAML/JSON")
    ocmp.add_argument("--check-id", required=True)
    ocmp.add_argument("--store", required=True, help="agent store for the runs")
    ocmp.add_argument("--evaluator-store", required=True)
    ocmp.add_argument("--work-dir", required=True)
    ocmp.add_argument("--repeats", type=int, default=2, help="runs per case per candidate")
    ocmp.add_argument("--live", action="store_true", help="real agent model (required)")
    ocmp.add_argument("--provider", help="live: Pi provider for the agent")
    ocmp.add_argument("--model", help="live: agent model")
    ocmp.add_argument("--credential-env", help="live: env var holding the agent's API key")
    ocmp.add_argument("--max-requests", type=int, help="live: request cap per run")
    ocmp.add_argument("--max-cost-usd", help="live: cost cap per run")
    ocmp.add_argument("--compare-max-cost-usd", help="live: cost cap for the whole check")
    ocmp.add_argument("--variant", default="S1r")
    ocmp.add_argument("--ladder")
    ocmp.add_argument("--synthetic-ladder", action="store_true")
    ocmp.add_argument("--timeout", type=float, default=300, help="seconds per Pi run")
    ocmp.add_argument("--cases-root")
    ocmp.set_defaults(func=cmd_optimize_compare)
    ost = op.add_parser("status", help="candidates, decisions and the final set of a search")
    ost.add_argument("--optimizer-store", required=True)
    ost.add_argument("--search-id", required=True)
    ost.set_defaults(func=cmd_optimize_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except OnodaError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
