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
            "(see `onoda case --help`)"
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


def cmd_pi_run(a) -> Any:
    from onoda.adapters.pi.contract import HarnessCandidate, Script
    from onoda.adapters.pi.run import DEFAULT_INSTRUCTION, run_case
    from onoda.adapters.pi.runtime import LiveSettings, detect_sandbox

    if bool(a.script) == bool(a.live):
        raise OnodaError("give exactly one of --script (offline) or --live")
    if a.max_requests is not None and a.max_requests <= 0:
        raise OnodaError("--max-requests must be greater than 0")
    if a.max_acquisitions < 0:
        raise OnodaError("--max-acquisitions must be 0 or more")
    if bool(a.reducer_provider) != bool(a.reducer_model):
        raise OnodaError("give both --reducer-provider and --reducer-model, or neither")
    script = Script.model_validate_json(Path(a.script).read_text()) if a.script else None
    live = None
    if a.live:
        live = LiveSettings(
            provider=a.provider,
            model=a.model,
            credential_env=a.credential_env,
            max_requests=a.max_requests,
            max_cost_usd=a.max_cost_usd,
        )
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


def cmd_pi_replay(a) -> Any:
    from onoda.adapters.pi.run import replay_run_pi

    store = _store(a.store)
    case_id = store.run_record(a.run_id).config.get("case_id")
    return replay_run_pi(
        store,
        a.run_id,
        _case(case_id, a.cases_root),
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
    from onoda.ablation.run import run_ablation
    from onoda.science.contract import CASES_ROOT

    digest, report = run_ablation(
        a.spec,
        store=_store(a.store),
        evaluator_store=_store(a.evaluator_store, evaluator=True),
        work_dir=Path(a.work_dir),
        cases_root=Path(a.cases_root) if a.cases_root else CASES_ROOT,
        timeout_s=a.timeout,
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
    }


def cmd_ablation_show(a) -> Any:
    store = _store(a.evaluator_store, evaluator=True)
    if "ablation-report" not in store.descriptor(a.report).labels:
        raise OnodaError(f"{a.report} is not an ablation report")
    return store.get_json(a.report)


def build_parser() -> argparse.ArgumentParser:
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
    rp2 = pi.add_parser("replay", help="re-run a scripted run; fails closed on any drift")
    rp2.add_argument("run_id")
    rp2.add_argument("--store", required=True)
    rp2.add_argument("--work-dir", required=True)
    rp2.add_argument("--timeout", type=float, default=300)
    rp2.add_argument("--cases-root")
    rp2.set_defaults(func=cmd_pi_replay)
    pe = pi.add_parser("evaluate", help="grade a Pi run's claim in a separate evaluator store")
    pe.add_argument("run_id")
    pe.add_argument("--store", required=True)
    pe.add_argument("--evaluator-store", required=True)
    pe.add_argument("--cases-root")
    pe.set_defaults(func=cmd_pi_evaluate)

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
