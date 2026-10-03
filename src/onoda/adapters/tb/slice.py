"""Run a paired benchmark slice (one task, every arm) under explicit spend caps.

    python -m onoda.adapters.tb.slice build-runtime --out DIR
    python -m onoda.adapters.tb.slice run --tasks-root DIR --tasks FILE --out DIR ...
    python -m onoda.adapters.tb.slice report --out DIR

`run` starts `proxy.SpendProxy` with the credential read from the environment
variable named by `--credential-env`, removes that variable from the
environment Harbor inherits, and runs one round per task: every arm of the
task as its own `harbor run` job, concurrently. Before each round it checks
that the slice cap still covers every arm of the round at the per-run proxy
cap, and stops before the first round it does not. Every setting is explicit;
nothing defaults to spending.

Results are exploratory (A69): the tasks are an operator's pick, one attempt
each, and the numbers are sums over spooled records, not gate verdicts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

from onoda.adapters.pi.contract import DECIMAL_RE, RUNTIME_DIR, VARIANTS
from onoda.adapters.pi.runtime import doctor
from onoda.adapters.tb.proxy import Prices, SpendProxy
from onoda.errors import CapabilityError, OnodaError

AGENT_IMPORT_PATH = "onoda.adapters.tb.harbor_agent:OnodaPiAgent"
HARBOR_VERSION = "0.23.0"  # needs Python >= 3.12, so it runs in its own uv environment
NODE_IMAGE = "node:24.15.0-bookworm-slim"  # the Node the pinned Pi runs on in the container
ARCHES = ("amd64", "arm64")
# The proxy's per-run cap sits above Pi's own, so the recorder's clean
# `budget-exhausted` stop comes first and the proxy is the backstop.
PROXY_CAP_FACTOR = Decimal("1.25")
REPO_ROOT = RUNTIME_DIR.parent
_RUNTIME_MEMBERS = ("package.json", "package-lock.json", "extensions", "node_modules")


def build_runtime(out: Path) -> list[Path]:
    """Write `runtime-<arch>.tar.gz` (Node + the pinned `pi/` tree) for each arch."""
    check = doctor()
    if not check["ok"]:
        raise CapabilityError(f"pinned Pi runtime is not installed: {check['problems']}")
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for arch in ARCHES:
        node = out / f"node-{arch}"
        with node.open("wb") as fh:
            subprocess.run(
                ["docker", "run", "--rm", "--platform", f"linux/{arch}", NODE_IMAGE]
                + ["cat", "/usr/local/bin/node"],
                stdout=fh,
                check=True,
            )
        tarball = out / f"runtime-{arch}.tar.gz"
        with tarfile.open(tarball, "w:gz", compresslevel=3) as tar:
            info = tar.gettarinfo(str(node), arcname="node")
            info.mode = 0o755
            with node.open("rb") as fh:
                tar.addfile(info, fh)
            for name in _RUNTIME_MEMBERS:
                tar.add(RUNTIME_DIR / name, arcname=f"pi/{name}")
        node.unlink()
        made.append(tarball)
    return made


def _harbor_argv(a: argparse.Namespace, task: str, arm: str, base_url: str) -> list[str]:
    kwargs = {
        "variant": arm,
        "runtime_dir": str(Path(a.runtime).resolve()),
        "base_url": base_url,
        "max_requests": a.max_requests,
        "max_cost_usd": a.max_cost_usd,
        "timeout_sec": a.timeout_sec,
        "credential_env": a.credential_env,
    }
    if "evidencePreservingReducer" in VARIANTS[arm].mechanisms:
        kwargs["reducer_model"] = a.reducer_model
    argv = ["uv", "run", "--no-project", "--python", "3.13"]
    argv += ["--with", f"harbor=={HARBOR_VERSION}", "--with-editable", str(REPO_ROOT)]
    argv += ["harbor", "run", "-y", "--quiet", "-n", "1"]
    argv += ["-p", str(Path(a.tasks_root, task).resolve())]
    argv += ["-a", AGENT_IMPORT_PATH, "-m", f"{a.provider}/{a.model}"]
    argv += ["-o", str(Path(a.out, "jobs").resolve()), "--job-name", f"{task}__{arm}"]
    for k, v in kwargs.items():
        argv += ["--ak", f"{k}={v}"]
    return argv


def _run_arm(
    a: argparse.Namespace, proxy: SpendProxy, port: int, task: str, arm: str, env: dict[str, str]
) -> int:
    cap = Decimal(a.max_cost_usd) * PROXY_CAP_FACTOR
    token = proxy.open_run(f"{task}/{arm}", cap)
    base_url = f"http://host.docker.internal:{port}/t/{token}/api/v1"
    log = Path(a.out, "logs", f"{task}__{arm}.log")
    try:
        with log.open("w") as fh:
            return subprocess.run(
                _harbor_argv(a, task, arm, base_url),
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            ).returncode
    finally:
        proxy.close_run(token)


def run(a: argparse.Namespace) -> int:
    arms = a.arms.split(",")
    for arm in arms:
        if arm not in VARIANTS:
            raise OnodaError(f"unknown arm {arm!r}")
        if "evidencePreservingReducer" in VARIANTS[arm].mechanisms and not a.reducer_model:
            raise CapabilityError(f"arm {arm} needs --reducer-model")
    for name in ("max_cost_usd", "slice_max_cost_usd"):
        if not DECIMAL_RE.match(getattr(a, name)):
            raise CapabilityError(f"--{name.replace('_', '-')} is not decimal text")
    credential = os.environ.get(a.credential_env)
    if not credential:
        raise CapabilityError(f"credential env var {a.credential_env!r} is not set")
    lines = (ln.split("#", 1)[0].strip() for ln in Path(a.tasks).read_text().splitlines())
    tasks = [ln for ln in lines if ln]
    out = Path(a.out)
    for d in ("jobs", "logs"):
        (out / d).mkdir(parents=True, exist_ok=True)
    done = {p.name for p in (out / "jobs").iterdir()}
    slice_cap = Decimal(a.slice_max_cost_usd)
    # Earlier invocations into the same --out already spent part of the slice cap.
    ledgers = sorted(out.glob("ledger-*.json"))
    carried = sum((Decimal(json.loads(p.read_text())["spent_usd"]) for p in ledgers), Decimal(0))
    prices = (
        Prices(*(Decimal(x) for x in a.fallback_prices.split(","))) if a.fallback_prices else None
    )
    proxy = SpendProxy(
        credential=credential,
        total_cap_usd=slice_cap - carried,
        upstream=a.upstream,
        prices=prices,
        ledger_path=out / f"ledger-{len(ledgers):03d}.json",
    )
    # Harbor and everything it starts run without the credential.
    env = {k: v for k, v in os.environ.items() if k != a.credential_env}
    env["HARBOR_TELEMETRY"] = "0"
    port = proxy.start()
    round_cap = Decimal(a.max_cost_usd) * PROXY_CAP_FACTOR * len(arms)
    status = 0
    try:
        with ThreadPoolExecutor(max_workers=len(arms)) as pool:
            for task in tasks:
                if all(f"{task}__{arm}" in done for arm in arms):
                    print(f"[skip] {task}: already run", flush=True)
                    continue
                spent = carried + proxy.spent()
                if spent + round_cap > slice_cap:
                    print(
                        f"[stop] {task}: spent {spent} + round cap {round_cap} would cross "
                        f"the slice cap {slice_cap} USD",
                        flush=True,
                    )
                    status = 3
                    break
                t0 = time.monotonic()
                arm_runs = [pool.submit(_run_arm, a, proxy, port, task, arm, env) for arm in arms]
                codes = [f.result() for f in arm_runs]
                print(
                    f"[done] {task}: harbor exit {dict(zip(arms, codes, strict=True))}, "
                    f"{time.monotonic() - t0:.0f}s, slice spent {carried + proxy.spent()} USD",
                    flush=True,
                )
    finally:
        proxy.stop()
    return status


# ------------------------------------------------------------------- report


def _trial(job_dir: Path) -> dict[str, Any]:
    """One arm's run of one task: Harbor's verdict plus the spool metrics."""
    task, arm = job_dir.name.rsplit("__", 1)
    row: dict[str, Any] = {"task": task, "arm": arm, "reward": None, "exception": None}
    for result in sorted(job_dir.glob("*/result.json")):
        data = json.loads(result.read_text())
        rewards = (data.get("verifier_result") or {}).get("rewards") or {}
        row["reward"] = rewards.get("reward")
        exc = data.get("exception_info")
        row["exception"] = exc.get("exception_type") if isinstance(exc, dict) else None
        metrics = result.parent / "agent" / "onoda" / "metrics.json"
        if metrics.is_file():
            row["metrics"] = json.loads(metrics.read_text())
    return row


def _arm_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ms = [r["metrics"] for r in rows if r.get("metrics", {}).get("spool_verified")]
    tok = {k: sum(m["tokens_total"][k] for m in ms) for k in ("input", "output", "cacheRead")}
    return {
        "runs": len(rows),
        "verified_spools": len(ms),
        "solved": sum(1 for r in rows if r["reward"] == 1),
        "requests": {p: sum(m["requests"][p] for m in ms) for p in ("agent", "reducer", "summary")},
        "tokens": tok,
        "tokens_all": sum(tok.values()),
        "context_bytes": sum(sum(m["context_bytes"].values()) for m in ms),
        "cost_usd": str(sum((Decimal(m["cost_usd_total"]) for m in ms), Decimal(0))),
        "reducer_requests": sum(m["reducer"]["requests"] for m in ms),
        "results_reduced": sum(m["reducer"]["results_reduced"] for m in ms),
        "compactions_done": sum(m["compaction"]["phases"].get("done", 0) for m in ms),
        "summary_requests": sum(m["compaction"]["summary_requests"] for m in ms),
        "fused_calls": sum(m["action_fusion"]["fused_calls"] for m in ms),
        "obs_recall_calls": sum(m["observation_pack"]["obs_recall_calls"] for m in ms),
        "update_plan_calls": sum(m["online_context_compact"]["update_plan_calls"] for m in ms),
    }


def report(out: Path) -> dict[str, Any]:
    rows = [_trial(d) for d in sorted((out / "jobs").iterdir()) if "__" in d.name]
    arms = sorted({r["arm"] for r in rows})
    # Paired: only tasks every arm ran count toward the totals.
    paired = sorted(
        t
        for t in {r["task"] for r in rows}
        if {r["arm"] for r in rows if r["task"] == t} == set(arms)
    )
    ledgers = [json.loads(p.read_text()) for p in sorted(out.glob("ledger-*.json"))]
    return {
        "schema": "onoda.tb-slice-report/1",
        "exploratory": True,
        "tasks": paired,
        "arms": {
            arm: _arm_totals([r for r in rows if r["arm"] == arm and r["task"] in paired])
            for arm in arms
        },
        "billed_usd": str(sum((Decimal(x["spent_usd"]) for x in ledgers), Decimal(0))),
        "billed_by_run": {r["label"]: r["cost_usd"] for x in ledgers for r in x["runs"]},
        "runs": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m onoda.adapters.tb.slice")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-runtime", help="pack Node + the pinned pi/ tree per architecture")
    b.add_argument("--out", required=True)
    r = sub.add_parser("run", help="run the slice; every flag is required")
    for flag in (
        "tasks-root",
        "tasks",
        "out",
        "runtime",
        "arms",
        "provider",
        "model",
        "credential-env",
        "upstream",
        "max-cost-usd",
        "slice-max-cost-usd",
    ):
        r.add_argument(f"--{flag}", required=True)
    r.add_argument("--max-requests", required=True, type=int)
    r.add_argument("--timeout-sec", required=True, type=int)
    r.add_argument("--reducer-model", help="reducer model for arms with the reducer")
    r.add_argument(
        "--fallback-prices",
        help="input,output,cache_read USD per million tokens, for responses without a cost",
    )
    s = sub.add_parser("report", help="aggregate a slice directory")
    s.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == "build-runtime":
            for path in build_runtime(Path(a.out)):
                print(path)
            return 0
        if a.cmd == "run":
            return run(a)
        print(json.dumps(report(Path(a.out)), indent=1, sort_keys=True))
        return 0
    except OnodaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
