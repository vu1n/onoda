"""`labrun` command line (PORTABLE_RUN_SPEC §14). Every command prints JSON.

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

from labrun.errors import LabrunError
from labrun.store import Store


def _store(path: str, *, evaluator: bool = False) -> Store:
    return Store(path, allowed_visibility=("evaluator",) if evaluator else ("agent",))


def _controller(store_path: str):
    from labrun.controller import Controller
    from labrun.fixture import PROGRAMS
    from labrun.jobs import MockScheduler

    store = _store(store_path)
    scheduler = MockScheduler(Path(f"{Path(store_path).resolve()}.scheduler"), PROGRAMS)
    return Controller(store, scheduler)


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
    from labrun.portability import export_bundle

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
    from labrun.portability import import_bundle

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
    from labrun.portability import inspect_capabilities
    from labrun.replay import replay_run

    store = _store(a.store)
    out: dict[str, Any] = {"run_id": a.run_id}
    if a.capabilities:
        out["capabilities"] = inspect_capabilities(store, a.run_id)
    else:
        out["history"] = replay_run(store, a.run_id)
    return out


def cmd_replay(a) -> Any:
    from labrun.replay import replay_run

    if not a.offline:
        raise LabrunError("replay is offline-only; pass --offline to acknowledge")
    return replay_run(_store(a.store), a.run_id)


def cmd_reanalyze(a) -> Any:
    from labrun.replay import reanalyze

    return reanalyze(_store(a.store), a.run_id, step=a.step, transform=a.transform)


def cmd_fork(a) -> Any:
    from labrun.forks import INTERVENTIONS, fork_run

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
        raise LabrunError(f"resume mode {a.mode!r} unavailable: only fixture mode runs offline")
    return _state_json(_controller(a.store).resume(a.run_id))


def cmd_compare(a) -> Any:
    from labrun.budgets import cost_views, physical_cost

    store = _store(a.store)
    runs = []
    for run_id in a.run_ids:
        run = store.get_record("run", run_id)
        final = None
        for e in reversed(store.events(run_id)):
            if e.type == "checkpoint" and e.payload.get("boundary") == "finalized":
                cp = store.get_record("checkpoint", e.payload_refs["checkpoint"].split("/", 1)[1])
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
    from labrun.evaluation import evaluate_run, publish_reference

    store = _store(a.store)
    evaluator = _store(a.evaluator_store, evaluator=True)
    task = store.get_record("task", store.get_record("run", a.run_id).task)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labrun", description=__doc__.splitlines()[0])
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except LabrunError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
