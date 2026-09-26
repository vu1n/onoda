"""`onoda` command line (PORTABLE_RUN_SPEC §14). Every command prints JSON.

The mock scheduler stands in for an external system, so its state lives beside
the store (`<store>.scheduler/`), never inside it, and is never exported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from onoda.errors import OnodaError
from onoda.store import Store


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
            "runnable_in_m3": not v.rlm,
        }
        for v in VARIANTS.values()
    ]


def cmd_pi_run(a) -> Any:
    from onoda.adapters.pi.contract import Script
    from onoda.adapters.pi.run import DEFAULT_INSTRUCTION, run_case
    from onoda.adapters.pi.runtime import LiveSettings

    if bool(a.script) == bool(a.live):
        raise OnodaError("give exactly one of --script (offline) or --live")
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
        max_requests=a.max_requests if a.max_requests is not None else (0 if a.live else 16),
        max_acquisitions=a.max_acquisitions,
        timeout_s=a.timeout,
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

    pi = sub.add_parser("pi", help="Pi agent runs, SoL-Pi variants S0-S3 (M3)")
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
    pr.add_argument("--doc", action="append", default=[], help="allowed documentation (.md)")
    pr.add_argument("--ladder", help="advisory ladder document (required from S1 on)")
    pr.add_argument("--synthetic-ladder", action="store_true")
    pr.add_argument("--instruction")
    pr.add_argument("--timeout", type=float, default=300)
    pr.add_argument("--cases-root")
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
