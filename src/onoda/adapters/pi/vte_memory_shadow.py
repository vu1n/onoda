"""Memory shadow: would kypp's recall have carried the fix a run needed? (decision memory-tiers)

The agent's memory is the frozen markdown tiers (`experiments/questdb/memory`,
A75). kypp (Vu's swarm-memory store) runs beside them in shadow only, to learn
whether a selective store would do better than a whole page:

1. A fresh kypp store is seeded with the same knowledge the tiers hold (one
   accepted claim per tier bullet) plus one pitfall per failure seen in
   *earlier* runs (`seed_runs`, each finished before every shadowed run
   started, so the store cannot know a shadowed run's outcome).
2. For each job of a shadowed run, kypp is asked (`kypp recall`) with what the
   agent could see right after that job: its `!` keyword lines and the
   parser's summary of its output (A74).
3. A job's failures are classed from the parser (not terminated, CASSCF not
   converged, no irreps, no SC-NEVPT2 table) and from the grader's reason
   classes of claims citing it (A53's limit: reason words, never values). A
   failure counts as covered when the recalled text, or the tiers' text,
   contains a phrase of its fix (`FIXES`). The text sizes are recorded too,
   since a page that covers everything by being long is the cost being
   measured.

Shadow means nothing here reaches the agent or the grader: the answers go to
the evaluator store as `vte-memory-shadow-kypp` evaluations. No reference
value, reference key or error is read; kypp runs as a local subprocess with
its own database and no credentials.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from onoda.adapters.pi.vte import _latest_vte_evaluation
from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import OnodaError
from onoda.evaluation import _next_evaluation_id
from onoda.records import Evaluation, Usage
from onoda.science.orca_parser import parse_orca_output
from onoda.science.orca_summary import job_summary
from onoda.store import Store

EVALUATOR = "vte-memory-shadow-kypp"
VERSION = "1"
KYPP_PROJECT = "onoda-questdb"
DEFAULT_LIMIT = 5

# Failure class -> phrases a memory must contain to carry its fix (case-insensitive).
FIXES: dict[str, tuple[str, ...]] = {
    "job_not_normal": ("termination: normal",),
    "casscf_not_converged": ("orbstep trah",),
    "no_irreps": ("usesym",),
    "no_transition_table": ("ptmethod sc_nevpt2", "single-root"),
    "claim_no_state": ("subtracted", "single-root"),
    "claim_value": ("subtracted",),
    "claim_irrep_not_printed": ("usesym",),
    "claim_conflict": ("own character",),
    "claim_character": ("two orbitals separated by a comma",),
    "claim_job_problem": ("termination: normal",),
}
# What a failure looked like, for the pitfalls seeded from earlier runs (no fix text).
SEEN: dict[str, str] = {
    "job_not_normal": "the job did not terminate normally",
    "casscf_not_converged": "its CASSCF did not converge",
    "no_irreps": "its transition energies were printed without irreps",
    "no_transition_table": "a converged CASSCF printed no SC-NEVPT2 transition table",
}
_BULLET = re.compile(r"^- (.+)$")
_SECTION = re.compile(r"^#+\s+(.+?)\s*$")


@dataclass(frozen=True)
class Lesson:
    type: str  # kypp claim type
    subject: str
    content: str


@dataclass(frozen=True)
class Job:
    job_id: str
    input: bytes
    output: bytes


def run_jobs(store: Store, run_id: str) -> list[Job]:
    """The run's broker jobs in id order, with the input the agent wrote and the output it got."""
    jobs = {}
    for event in store.events(run_id):
        if event.type != "invocation.committed":
            continue
        receipt = store.receipt(event.payload["invocation_id"])
        job_id = receipt.metadata.get("job_id")
        if job_id is None or "log" not in receipt.outputs:
            continue
        jobs[job_id] = Job(
            job_id,
            store.get_bytes(receipt.inputs["input"]),
            store.get_bytes(receipt.outputs["log"]),
        )
    return [jobs[k] for k in sorted(jobs)]


def job_failures(log: bytes) -> list[str]:
    """Failure classes the parser alone shows in one output."""
    obs = parse_orca_output(log)
    found = []
    if obs.termination != "normal":
        found.append("job_not_normal")
    if obs.casscf_converged is False:
        found.append("casscf_not_converged")
    if obs.mr_states and all(s.symmetry is None for s in obs.mr_states):
        found.append("no_irreps")
    if obs.casscf_converged and not any("NEVPT2" in (s.method or "") for s in obs.mr_states):
        found.append("no_transition_table")
    return found


def claim_failures(evaluator_store: Store, run_id: str) -> dict[str, list[str]]:
    """Job id -> reason classes of the claims citing it (verdict reasons only, A53)."""
    grade = _latest_vte_evaluation(evaluator_store, run_id)
    out: dict[str, list[str]] = {}
    for key, job_id in grade.metrics.items():
        if not (key.startswith("claim.") and key.endswith(".job")):
            continue
        tag = key.removesuffix(".job")
        reason = grade.metrics.get(f"{tag}.reason") or grade.metrics.get(f"{tag}.identity_reason")
        if reason:
            out.setdefault(job_id, []).append(f"claim_{reason}")
    return out


def keyword_lines(inp: bytes) -> list[str]:
    return [
        ln.strip()
        for ln in inp.decode("utf-8", errors="replace").splitlines()
        if ln.strip().startswith("!")
    ]


def tier_lessons(tiers: Mapping[str, str]) -> list[Lesson]:
    """One accepted procedure per tier bullet (continuation lines joined), in file order."""
    lessons = []
    for name, text in sorted(tiers.items()):
        section, bullet = name, None
        bullets: list[tuple[str, list[str]]] = []
        for line in text.splitlines():
            if m := _SECTION.match(line):
                section, bullet = m.group(1), None
            elif m := _BULLET.match(line):
                bullet = [m.group(1)]
                bullets.append((section, bullet))
            elif bullet is not None and line.startswith("  ") and line.strip():
                bullet.append(line.strip())
            else:
                bullet = None
        for i, (sec, parts) in enumerate(bullets, 1):
            lessons.append(Lesson("procedure", f"{name} {i}: {sec}", " ".join(parts)))
    return lessons


def run_lessons(store: Store, evaluator_store: Store, run_id: str) -> list[Lesson]:
    """One pitfall per failure an earlier run showed: what happened, never a value or a fix."""
    molecule = store.run_record(run_id).config.get("case_id", "unknown case")
    by_claim = claim_failures(evaluator_store, run_id)
    lessons = []
    for job in run_jobs(store, run_id):
        keywords = "; ".join(keyword_lines(job.input)) or "no keyword line"
        for cls in job_failures(job.output):
            lessons.append(
                Lesson(
                    "pitfall",
                    f"{molecule} {run_id} {job.job_id}: {cls}",
                    f"In {molecule}, job {job.job_id} ({keywords}): {SEEN[cls]}.",
                )
            )
        for cls in by_claim.get(job.job_id, []):
            lessons.append(
                Lesson(
                    "pitfall",
                    f"{molecule} {run_id} {job.job_id}: {cls}",
                    f"In {molecule}, a claim citing job {job.job_id} ({keywords}) was graded "
                    f"{cls.removeprefix('claim_').replace('_', ' ')}.",
                )
            )
    return lessons


class Kypp:
    """The `kypp` CLI on its own database, run as a subprocess with a minimal environment."""

    def __init__(self, binary: str, db: Path, *, project: str = KYPP_PROJECT):
        self.binary, self.db, self.project = binary, db, project

    def _run(self, args: Sequence[str]) -> str:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "KYPP_MEMORY_DB": str(self.db),
            "KYPP_PROJECT": self.project,
            "KYPP_USER": "onoda",
            "KYPP_AGENT": "onoda-memory-shadow",
        }
        for key in ("KYPP_EMBED_MODEL", "KYPP_OLLAMA_HOST"):  # local semantic recall, if set
            if key in os.environ:
                env[key] = os.environ[key]
        proc = subprocess.run(
            [self.binary, *args], env=env, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            raise OnodaError(f"kypp {args[0]} failed: {proc.stderr.strip()[-500:]}")
        return proc.stdout

    @property
    def semantic(self) -> bool:
        return "KYPP_EMBED_MODEL" in os.environ

    def remember(self, lesson: Lesson) -> None:
        self._run(
            ["remember", lesson.subject, lesson.content, "--type", lesson.type, "--accept",
             "--project", self.project]
        )  # fmt: skip

    def recall(self, query: str, limit: int) -> list[dict[str, Any]]:
        out = self._run(
            ["recall", *query.split(), "--limit", str(limit), "--json", "--project", self.project]
        )
        return [{"subject": c["subject"], "content": c["content"]} for c in json.loads(out)]


def _covers(text: str, cls: str) -> bool:
    low = text.lower()
    return any(p in low for p in FIXES.get(cls, ()))


def check_order(store: Store, seed_runs: Iterable[str], run_ids: Iterable[str]) -> None:
    """Refuse a seed run that had not finished before a shadowed run started."""
    for seed in seed_runs:
        events = list(store.events(seed))
        if not events:
            raise OnodaError(f"seed run {seed} has no events")
        done = events[-1].ts  # its last journal event: nothing was written to it after this
        for run_id in run_ids:
            if done >= store.run_record(run_id).created_at:
                raise OnodaError(
                    f"seed run {seed} finished at {done}, not before {run_id} started: "
                    "the shadow store would know that run's outcome"
                )


def seed(
    kypp: Kypp,
    store: Store,
    evaluator_store: Store,
    tiers: Mapping[str, str],
    seed_runs: Sequence[str],
) -> list[Lesson]:
    if kypp.db.exists():
        raise OnodaError(f"{kypp.db} exists; the shadow seeds a fresh kypp database")
    lessons = tier_lessons(tiers)
    for run_id in seed_runs:
        lessons += run_lessons(store, evaluator_store, run_id)
    for lesson in lessons:
        kypp.remember(lesson)
    return lessons


# Context: doc://onoda/decision-models-shadow-only@0001#never-grade — shadow answers go to the evaluator store only; the agent never sees them
def memory_shadow(
    store: Store,
    evaluator_store: Store,
    run_id: str,
    kypp: Kypp,
    *,
    tiers: Mapping[str, str],
    lessons: Sequence[Lesson],
    seed_runs: Sequence[str],
    limit: int = DEFAULT_LIMIT,
) -> Evaluation:
    """Ask kypp after every job of `run_id` and score its recall against the job's failures."""
    run = store.run_record(run_id)
    if run.config.get("task_kind") != "vte":
        raise OnodaError(f"run {run_id} is not a VTE run")
    tier_text = "\n".join(tiers[k] for k in sorted(tiers))
    by_claim = claim_failures(evaluator_store, run_id)
    assignments: dict[str, Any] = {}
    output_refs: dict[str, str] = {}
    sizes: list[int] = []
    instances = kypp_hits = tier_hits = 0
    for job in run_jobs(store, run_id):
        query = "\n".join([*keyword_lines(job.input), *job_summary(job.output)])
        recalled = kypp.recall(query, limit)
        text = "\n".join(f"{c['subject']}: {c['content']}" for c in recalled)
        failures = job_failures(job.output) + by_claim.get(job.job_id, [])
        ref = evaluator_store.put_bytes(
            canonical_bytes({"query": query, "recalled": recalled}),
            media_type="application/json",
            visibility="evaluator",
        )
        output_refs[f"job.{job.job_id}.recall"] = ref.digest
        assignments[f"job.{job.job_id}.failures"] = failures
        assignments[f"job.{job.job_id}.kypp.covers"] = [c for c in failures if _covers(text, c)]
        assignments[f"job.{job.job_id}.tiers.covers"] = [
            c for c in failures if _covers(tier_text, c)
        ]
        assignments[f"job.{job.job_id}.kypp.chars"] = str(len(text))
        sizes.append(len(text))
        instances += len(failures)
        kypp_hits += len(assignments[f"job.{job.job_id}.kypp.covers"])
        tier_hits += len(assignments[f"job.{job.job_id}.tiers.covers"])
    seeded = evaluator_store.put_bytes(
        canonical_bytes([[x.type, x.subject, x.content] for x in lessons]),
        media_type="application/json",
        visibility="evaluator",
    )
    output_refs["seed.lessons"] = seeded.digest
    synthetic = "synthetic" in run.labels
    evaluation = Evaluation.new(
        evaluation_id=_next_evaluation_id(evaluator_store, run_id, EVALUATOR, VERSION),
        run_id=run_id,
        evaluator=EVALUATOR,
        evaluator_version=VERSION,
        output_refs=output_refs,
        reference_version=f"tiers:{sha256_digest(tier_text.encode())}",  # no reference value
        assignments=assignments,
        unresolved=[],
        metrics={
            "jobs": str(len(sizes)),
            "failures": str(instances),
            "kypp.covered": str(kypp_hits),
            "tiers.covered": str(tier_hits),
            "kypp.chars.total": str(sum(sizes)),
            "tiers.chars": str(len(tier_text)),
            "kypp.limit": str(limit),
            "kypp.recall": "semantic" if kypp.semantic else "keyword",
            "seed_runs": ",".join(seed_runs),
            "seed_lessons": str(len(lessons)),
            "synthetic": "true" if synthetic else "false",
            "note": "memory shadow: never shown to the agent, never a grade",
        },
        usage=Usage(
            units={"kypp_recalls": len(sizes)},
            measured=not synthetic,
            simulated=synthetic,
            cost_usd="0",
        ),
        created_at=evaluator_store.clock.now(),
    )
    evaluator_store.put_record(evaluation)
    return evaluation


def memory_shadow_report(
    store: Store, evaluator_store: Store, run_ids: Iterable[str]
) -> dict[str, Any]:
    """Per arm: failures, how many kypp's recall and the tiers covered, and the text each cost."""
    arms: dict[str, dict[str, Any]] = {}
    synthetic = False
    for run_id in run_ids:
        prefix = f"{run_id}.{EVALUATOR}-v"
        ids = sorted(
            i for i in evaluator_store.list_record_ids("evaluation") if i.startswith(prefix)
        )
        if not ids:
            continue
        ev = evaluator_store.get_record("evaluation", ids[-1])
        assert isinstance(ev, Evaluation)
        m = ev.metrics
        synthetic |= m["synthetic"] == "true"
        a = arms.setdefault(
            store.run_record(run_id).config["variant"],
            {"runs": 0, "jobs": 0, "failures": 0, "kypp_covered": 0, "tiers_covered": 0,
             "kypp_chars": 0, "tiers_chars_per_run": m["tiers.chars"]},
        )  # fmt: skip
        a["runs"] += 1
        a["jobs"] += int(m["jobs"])
        a["failures"] += int(m["failures"])
        a["kypp_covered"] += int(m["kypp.covered"])
        a["tiers_covered"] += int(m["tiers.covered"])
        a["kypp_chars"] += int(m["kypp.chars.total"])
    for a in arms.values():
        chars = a.pop("kypp_chars")
        a["kypp_chars_per_job"] = (
            format(Decimal(chars) / Decimal(a["jobs"]), ".0f") if a["jobs"] else "unresolved"
        )
    return {
        "arms": arms,
        "synthetic": synthetic,
        "note": "kypp recall vs the markdown tiers, in shadow; never a grade",
    }
