"""Tabulate each live ORCA job against its case's acquired log (same parser, exact decimals).

Usage: uv run python experiments/orca-exec/compare.py [work_root]
"""

import json
import re
import sys
from decimal import Decimal
from pathlib import Path

from onoda.science.contract import CASES_ROOT, case_manifests, load_case
from onoda.science.orca_parser import parse_orca_output

work_root = Path(sys.argv[1] if len(sys.argv) > 1 else "orca-jobs")
state = json.loads((work_root / "scheduler.json").read_text())
manifests = case_manifests(CASES_ROOT)


def val(q):
    return q["value"] if q else None


def diff(a, b):
    return str(Decimal(a) - Decimal(b)) if a is not None and b is not None else "n/a"


for key, job_id in sorted(state["by_key"].items(), key=lambda kv: kv[1]):
    job = state["jobs"][job_id]
    if job["status"] == "submitted":
        print(f"\n## {key} ({job_id}): still running or uncertain\n")
        continue
    case_id = re.sub(r"^exec\d*-", "", key.split(":")[0])
    case = load_case(manifests[case_id])
    cand = parse_orca_output((work_root / job_id / "job.out").read_bytes()).to_json()
    ref = parse_orca_output(case.output_bytes["log"]).to_json()
    print(f"\n## {case_id} ({job_id})\n")
    print(
        f"- scheduler: status={job['status']} returncode={job['returncode']} "
        f"reason={job['reason']} wall={job['duration_ms'] / 1000:.1f} s"
    )
    print(
        f"- candidate: ORCA {cand['program_version']}, termination={cand['termination']}, "
        f"scf={cand['scf']} ({cand['scf_cycles']} cycles), problems={len(cand['problems'])}"
    )
    print(
        f"- reference: ORCA {ref['program_version']}, termination={ref['termination']}, "
        f"scf={ref['scf']} ({ref['scf_cycles']} cycles), problems={len(ref['problems'])}"
    )
    for p in cand["problems"]:
        print(f"  - candidate problem: {json.dumps(p)[:300]}")
    ce, re_ = val(cand["final_energy"]), val(ref["final_energy"])
    print(f"- final energy (Eh): candidate {ce}, reference {re_}, diff {diff(ce, re_)}")
    cs, rs = cand["excited_states"], ref["excited_states"]
    print(
        f"- excited states: candidate {len(cs)} ({cand['excited_states_status']}), "
        f"reference {len(rs)} ({ref['excited_states_status']})"
    )
    if cs or rs:
        print("\n| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |")
        print("|---|---|---|---|---|---|")

        # Pair like the evaluator: by multiplicity, then order within it (never by energy).
        def tagged(states):
            seen, out = {}, {}
            for s in states:
                n = seen[s["multiplicity"]] = seen.get(s["multiplicity"], 0) + 1
                out[f"m{s['multiplicity']}.r{n}"] = s
            return out

        ct, rt = tagged(cs), tagged(rs)
        for tag in sorted(set(ct) | set(rt)):
            c, r = ct.get(tag), rt.get(tag)
            ca = val(c["energy_au"]) if c else None
            ra = val(r["energy_au"]) if r else None
            print(
                f"| {tag} | {c['root'] if c else '-'}/{r['root'] if r else '-'} "
                f"| {c['symmetry'] if c else '-'}/{r['symmetry'] if r else '-'} "
                f"| {ca} | {ra} | {diff(ca, ra)} |"
            )
