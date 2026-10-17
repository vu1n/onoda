#!/usr/bin/env python3
"""Detached, billed-counter-guarded QUESTDB batches; no credentials in artifacts."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path("/Users/vuln/code/onoda")
HERE = ROOT / "experiments/questdb/runs/opus-hillclimb-1"
SCORING = ["ketene", "thioacetone", "acetaldehyde", "diazomethane"]
HELDOUT = ["formamide", "acetone", "cyclopropenone", "methylenecyclopropene"]


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def billed():
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return Decimal(str(json.load(response)["data"]["usage"]))


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def command(pin, args, target):
    with target.open("w") as output, target.with_suffix(".err").open("w") as error:
        subprocess.run(
            ["uv", "run", "onoda", *args], cwd=pin, stdout=output, stderr=error, check=True
        )


def terminate_owned(active):
    # Pi/ORCA may create new process groups; traverse only these run descendants.
    owned = {proc.pid for proc, _ in active.values()}
    pending = list(owned)
    while pending:
        parent = pending.pop()
        result = subprocess.run(
            ["pgrep", "-P", str(parent)], capture_output=True, text=True, timeout=5
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f"cannot enumerate owned children: {result.stderr}")
        for pid in map(int, result.stdout.split()):
            if pid not in owned:
                owned.add(pid)
                pending.append(pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(owned, reverse=True):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue  # already reaped after the stop signal
        time.sleep(2)
    for proc, log in active.values():
        proc.wait()
        log.close()


def run_batch(spec):
    pin = ROOT / ".pinned" / spec["pin"]
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=pin, text=True).strip()
    assert sha == spec["sha"], "pin moved"
    assert not subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=pin
    ), "dirty pin"
    batch = HERE / spec["batch"]
    batch.mkdir()
    work = Path(spec["work"])
    assert not work.exists(), "WORK must be fresh"
    work.mkdir()
    out = batch / "out"
    out.mkdir()
    molecules = HELDOUT if spec.get("heldout") else SCORING
    runs = [f"S1-{m}-r{r}" for m in molecules for r in (1, 2)]
    snapshot = HERE / "budget-start.json"
    usage = billed()
    if not snapshot.exists():
        save(snapshot, {"usage": str(usage), "time": time.time()})
    start = Decimal(json.loads(snapshot.read_text())["usage"])
    if usage - start >= 135:
        raise RuntimeError("budget floor: less than $15 remains; no launch")
    env = os.environ.copy()
    env.update(
        ENABLE_LIVE_EXECUTION="1",
        PROVIDER="openrouter",
        MODEL="anthropic/claude-opus-5.5:high",
        CREDENTIAL_ENV="OPENROUTER_API_KEY",
        PI_MODELS=str(pin / "experiments/questdb/pi-models-claude-5.5.json"),
        MAX_REQUESTS="200",
        MAX_COST_USD="100",
        TIMEOUT="7200",
        REPORT_REMINDER="1",
        PARALLEL="4",
        REPEATS="2",
        CASES=" ".join("questdb-" + m for m in molecules),
        WORK=str(work),
        OUT=str(out),
    )
    started = time.time()
    save(
        batch / "launch.json",
        {
            **spec,
            "runs": runs,
            "usage_before": str(usage),
            "started": started,
            "settings": {
                k: env[k]
                for k in [
                    "MODEL",
                    "PROVIDER",
                    "MAX_REQUESTS",
                    "MAX_COST_USD",
                    "TIMEOUT",
                    "REPORT_REMINDER",
                    "PARALLEL",
                    "PI_MODELS",
                    "WORK",
                    "OUT",
                ]
            },
        },
    )
    active = {}
    pending = list(runs)
    done = {}
    stop = None
    last_bill = 0
    try:
        while pending or active:
            if (HERE / "STOP").exists():
                raise RuntimeError((HERE / "STOP").read_text().strip())
            if time.time() - last_bill >= 20:
                usage = billed()  # fail closed when accounting is unavailable
                append(
                    HERE / "billing.jsonl",
                    {
                        "time": time.time(),
                        "usage": str(usage),
                        "delta": str(usage - start),
                        "batch": spec["batch"],
                    },
                )
                last_bill = time.time()
                if usage - start >= 145:
                    raise RuntimeError(
                        "budget floor: stopping in-flight requests with $5 billing margin"
                    )
                if usage - start >= 135:
                    stop = "budget floor: less than $15 remains"
                    pending.clear()
            for rid, (proc, log) in list(active.items()):
                if proc.poll() is None:
                    continue
                log.close()
                del active[rid]
                if proc.returncode:
                    raise RuntimeError(f"{rid}: runner exited {proc.returncode}")
                evaluation = out / f"{rid}.evaluate.json"
                if not evaluation.exists():
                    raise RuntimeError(f"{rid}: no evaluation; inspect run.err")
                result = json.loads((out / f"{rid}.run.json").read_text())
                evfile = next((out / "runs" / rid / "evaluator/records/evaluation").glob("*.json"))
                metrics = json.loads(evfile.read_text())["metrics"]
                false_success = int(metrics["false_success_claims"])
                if false_success:
                    raise RuntimeError(f"STOP: {rid} false successes {false_success}")
                command(
                    pin,
                    ["vte", "audit", rid, "--run-stores", str(out / "runs")],
                    batch / f"{rid}.audit.json",
                )
                audit = json.loads((batch / f"{rid}.audit.json").read_text())
                if audit["false_accepts"]:
                    raise RuntimeError(f"STOP: {rid} audit false_accepts {audit['false_accepts']}")
                store = out / "runs" / rid / "store"
                config = json.loads(next((store / "records/run").glob("*.json")).read_text())[
                    "config"
                ]
                digest = hashlib.sha256(Path(env["PI_MODELS"]).read_bytes()).hexdigest()
                assert config["pi_models_sha256"] == digest, "missing/wrong models digest"
                cost = sum(
                    Decimal(json.loads(p.read_text())["usage"]["cost_usd"] or "0")
                    for p in (store / "records/invocation").glob("*.json")
                )
                assert cost > 0, "Pi estimate is zero"
                done[rid] = {
                    "accurate": int(metrics["accurate"]),
                    "found": int(metrics["coverage"]),
                    "total": int(metrics["coverage.total"]),
                    "claim": metrics["claim.present"] == "true",
                    "settled": result["complete"] and result["status"] == "settled",
                    "false_successes": false_success,
                    "audit_false_accepts": audit["false_accepts"],
                    "pi_cost_usd": str(cost),
                    "pi_models_sha256": digest,
                }
                save(batch / "partial.json", done)
                print(spec["batch"], rid, done[rid], flush=True)
            while pending and len(active) < 4:
                rid = pending.pop(0)
                log = (batch / f"{rid}.log").open("w")
                proc = subprocess.Popen(
                    ["bash", "experiments/questdb/run.sh"],
                    cwd=pin,
                    env={
                        **env,
                        "ONLY_RUNS": rid,
                        "ORCA_WORK": str(work / "j" / str(runs.index(rid))),
                    },
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                active[rid] = (proc, log)
            time.sleep(2)
    except BaseException as error:
        stop = f"{type(error).__name__}: {error}"
        (HERE / "STOP").write_text(stop + "\n")
        terminate_owned(active)
    if done:
        command(
            pin,
            ["vte", "report", *done, "--run-stores", str(out / "runs"), "--arm", "S1"],
            batch / "report.json",
        )
        command(
            pin, ["vte", "audit", *done, "--run-stores", str(out / "runs")], batch / "audit.json"
        )
    final_usage = billed()
    row = {
        **spec,
        "per_run": done,
        "score": sum(x["accurate"] for x in done.values()),
        "per_molecule": {
            m: sum(x["accurate"] for r, x in done.items() if r.startswith(f"S1-{m}-"))
            for m in molecules
        },
        "complete": len(done) == 8,
        "stop": stop,
        "billed_usd": str(
            final_usage - Decimal(json.loads((batch / "launch.json").read_text())["usage_before"])
        ),
        "usage_before": json.loads((batch / "launch.json").read_text())["usage_before"],
        "usage_after": str(final_usage),
        "total_spend_usd": str(final_usage - start),
        "wall_seconds": round(time.time() - started),
    }
    save(batch / "score.json", row)
    append(HERE / "ledger.jsonl", row)
    with (HERE / "RESULTS.md").open("a") as stream:
        stream.write(
            f"\n### Batch {spec['batch']}\n\nCommit `{sha}`, memory {spec['memory']}: "
            f"{spec['change']}. "
            f"Score **{row['score']}**, molecule totals {row['per_molecule']}. "
            f"Billed ${row['billed_usd']}; {row['wall_seconds']} seconds. "
            f"Decision pending review. Stop: {stop or 'none'}.\n\n"
        )
        for rid, data in done.items():
            stream.write(
                f"- {rid}: {data['accurate']}/{data['total']}, found {data['found']}, "
                f"claim {data['claim']}, settled {data['settled']}, "
                f"false successes {data['false_successes']}, "
                f"audit false accepts {data['audit_false_accepts']}.\n"
            )
    if stop:
        raise RuntimeError(stop)
    return row


def decide(row, incumbent, delta):
    reasons = []
    if not row["complete"] or not all(r["settled"] and r["claim"] for r in row["per_run"].values()):
        reasons.append("every run must settle with a claim")
    if row["score"] < incumbent["score"] + delta:
        reasons.append(f"score below incumbent +{delta}")
    if any(row["per_molecule"][m] < incumbent["per_molecule"][m] - 1 for m in SCORING):
        reasons.append("a molecule lost more than one summed slot")
    return reasons


def record_decision(batch, decision, reasons):
    entry = {"batch": batch, "decision": decision, "reasons": reasons}
    append(HERE / "ledger.jsonl", entry)
    with (HERE / "RESULTS.md").open("a") as stream:
        stream.write(f"\nDecision for {batch}: **{decision}**. {reasons}\n")


def seed_mean():
    rows = [json.loads(p.read_text()) for p in HERE.glob("seed-*/score.json")]
    assert rows and all(r["complete"] for r in rows), "seed incomplete"
    return {
        "score": sum(r["score"] for r in rows) / len(rows),
        "per_molecule": {m: sum(r["per_molecule"][m] for r in rows) / len(rows) for m in SCORING},
        "source": [r["batch"] for r in rows],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    if (HERE / "STOP").exists():
        raise RuntimeError("STOP file present")
    plan = json.loads(args.plan.read_text())
    for spec in plan:
        row = run_batch(spec)
        if spec["batch"] == "seed-1" and any(
            abs(row["per_run"][f"S1-{m}-r1"]["accurate"] - row["per_run"][f"S1-{m}-r2"]["accurate"])
            > 1
            for m in SCORING
        ):
            run_batch(
                {
                    **spec,
                    "batch": "seed-2",
                    "work": "/tmp/ohs2",
                    "change": "second seed batch: repeat difference exceeded 1",
                }
            )

        if spec["batch"].startswith("candidate-"):
            incumbent_path = HERE / "incumbent.json"
            incumbent = (
                json.loads(incumbent_path.read_text()) if incumbent_path.exists() else seed_mean()
            )
            reasons = decide(row, incumbent, 3)
            if reasons:
                record_decision(spec["batch"], "rejected", reasons)
                continue
            record_decision(spec["batch"], "screen passed; confirmation required", [])
            confirm = run_batch(
                {**spec, "batch": spec["batch"] + "-confirm", "work": spec["work"] + "f"}
            )
            reasons = decide(confirm, incumbent, 2)
            if reasons:
                record_decision(spec["batch"], "rejected on confirmation", reasons)
            else:
                record_decision(spec["batch"], "accepted", [])
                save(
                    incumbent_path,
                    {
                        "score": (row["score"] + confirm["score"]) / 2,
                        "per_molecule": {
                            m: (row["per_molecule"][m] + confirm["per_molecule"][m]) / 2
                            for m in SCORING
                        },
                        "source": [row["batch"], confirm["batch"]],
                        "sha": spec["sha"],
                        "pin": spec["pin"],
                        "memory": spec["memory"],
                    },
                )


if __name__ == "__main__":
    main()
