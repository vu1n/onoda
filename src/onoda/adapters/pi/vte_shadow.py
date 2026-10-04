"""Shadow decisions on a recorded VTE run: what a System One model would have said (A67).

After a run, every broker job's output is shown to a decision model (Jev or
Clef, `onoda.systemone`) with two questions:

- `rule`: which ladder rule (A-H) applies first to this job. The options and
  their criteria are the rule paragraphs of the ladder file itself, so a
  GEPA-tuned ladder and the shadow model never drift apart.
- `ready`: whether the job's states are converged and identified well enough
  to report (a yes/no probability).

The state is built only from what the agent saw: the case's targets, the job's
own output (its input echo, convergence, CASSCF natural occupations, state
compositions and transition-energy listings, as verbatim lines), the
parser's status and the host checks (A66). No reference value is read.

Shadow means the agent never sees an answer: the run is already over. The
answers are recorded in the evaluator store as one `Evaluation`
(`vte-shadow-<provider>`), with the exact request and response bytes, and
`shadow_report` sets them beside the grader's verdicts so the research can
ask whether the model would have caught the agent's mistakes. The grader
never reads them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Protocol

from onoda.adapters.pi.vte import VTE_EVALUATOR, _latest_vte_evaluation, _vte_jobs, mean_ev
from onoda.canonical import digest_of, sha256_digest
from onoda.decision_tune import Example, apply_candidate
from onoda.errors import OnodaError
from onoda.evaluation import _next_evaluation_id
from onoda.records import Evaluation, Usage
from onoda.science.orca_checks import job_checks, job_facts
from onoda.science.orca_parser import parse_orca_output
from onoda.science.vte import LoadedVteCase
from onoda.store import Store
from onoda.systemone import SystemOneAnswer

SHADOW_VERSION = "2"  # 2: states say what an absent SCF means (A68)
MAX_EXCERPT_CHARS = 12000  # Jev reads 32k tokens; leaves room for criteria and targets
_RULE = re.compile(r"^\*\*([A-H])\. (.+?)\*\*\s*(.*)$")
_INPUT_ECHO = re.compile(r"^\|\s*\d+>")
# (anchor, lines kept from it): the parts of a CASSCF/NEVPT2 log a ladder decision reads.
_ANCHORS = (
    ("Final CASSCF energy", 30),  # final energy and the natural-orbital occupation table
    ("CAS-SCF STATES FOR BLOCK", 10),  # each root's energy and leading configurations
    ("SA-CASSCF TRANSITION ENERGIES", 12),
    ("NEVPT2 TRANSITION ENERGIES", 12),
    ("ERROR", 3),
    ("aborting the run", 3),
    ("NOT CONVERGED", 3),
    ("****ORCA TERMINATED NORMALLY****", 1),
)
SPIN_NAMES = {1: "singlet", 3: "triplet"}


class DecisionModel(Protocol):
    provider: str
    model: str

    def ask(self, state: Any, questions: Mapping[str, Any]) -> SystemOneAnswer: ...


def ladder_rules(ladder: bytes) -> dict[str, str]:
    """Rule letter -> its paragraph from the ladder file (`**A. Title.** text ...`)."""
    rules: dict[str, list[str]] = {}
    current: str | None = None
    for line in ladder.decode("utf-8").splitlines():
        if m := _RULE.match(line):
            current = m.group(1)
            rules[current] = [f"{m.group(2)} {m.group(3)}".strip()]
        elif current and line.strip() and not line.startswith("#"):
            rules[current].append(line.strip())
        else:
            current = None
    if sorted(rules) != list("ABCDEFGH"):
        raise OnodaError(f"the ladder must define rules A-H, found {sorted(rules)}")
    return {k: " ".join(v) for k, v in sorted(rules.items())}


def questions(ladder: bytes) -> dict[str, Any]:
    return {
        "rule": {
            "type": "choice",
            "instructions": (
                "Rules are checked in priority order, A first. Which rule applies first to "
                "this ORCA job, given the target states?"
            ),
            "criteria": ladder_rules(ladder),
        },
        "ready": {
            "type": "noul",
            "instructions": (
                "Did this job converge and identify the target states well enough that their "
                "SC-NEVPT2 energies can be reported now?"
            ),
        },
    }


def excerpt(log: bytes) -> str:
    """The input echo plus the verbatim windows `_ANCHORS` pick, in file order, capped."""
    lines = log.decode("utf-8", errors="replace").splitlines()
    keep: set[int] = {i for i, ln in enumerate(lines) if _INPUT_ECHO.match(ln)}
    for i, ln in enumerate(lines):
        for anchor, n in _ANCHORS:
            if anchor in ln and not _INPUT_ECHO.match(ln):
                keep.update(range(i, min(i + n, len(lines))))
    out, size, last = [], 0, -1
    for i in sorted(keep):
        text = lines[i].rstrip()
        if last >= 0 and i != last + 1:
            text = "...\n" + text
        if size + len(text) + 1 > MAX_EXCERPT_CHARS:
            out.append("... (excerpt cut)")
            break
        out.append(text)
        size += len(text) + 1
        last = i
    return "\n".join(out)


def scf_note(scf: str, log: bytes) -> str | None:
    """Plain words for an absent SCF, which decision models read as an SCF failure (A68)."""
    if scf != "absent":
        return None
    moread = any(
        _INPUT_ECHO.match(ln) and "moread" in ln.lower()
        for ln in log.decode("utf-8", errors="replace").splitlines()
    )
    why = (
        "its starting orbitals are read from a file with MORead"
        if moread
        else "the output has no SCF section"
    )
    return f"no SCF ran in this job ({why}); an absent SCF is not an SCF convergence failure"


def job_state(
    case: LoadedVteCase, job_id: str, log: bytes, checks: tuple[str, ...]
) -> dict[str, Any]:
    obs = parse_orca_output(log)
    targets = [
        f"{t.count} {SPIN_NAMES.get(t.spin, f'mult {t.spin}')} {t.irrep}"
        for t in case.manifest.targets
    ]
    return {
        "molecule": case.manifest.molecule,
        "target_states": targets,
        "job_id": job_id,
        "parser": {
            "termination": obs.termination,
            "scf": obs.scf,
            "scf_note": scf_note(obs.scf, log),
            "casscf_converged": obs.casscf_converged,
            "problems": [f"{p.field}: {p.state}: {p.detail}" for p in obs.problems][:10],
        },
        "host_checks": list(checks),
        "output_excerpt": excerpt(log),
    }


def _p(value: Any) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.000001"), ROUND_HALF_EVEN), "f")


def _job_states(store: Store, run_id: str, case: LoadedVteCase):
    """(job id, job, state) for every broker job of a VTE run, oldest first.

    The state is what `shadow_run` shows a model; `shadow_examples` labels the
    same states, so tuned wording is tuned on exactly what the model will see.
    """
    run = store.run_record(run_id)
    if run.config.get("task_kind") != "vte":
        raise OnodaError(f"run {run_id} is not a VTE run")
    if run.config.get("case_id") != case.manifest.case_id:
        raise OnodaError(f"run {run_id} was recorded from case {run.config.get('case_id')!r}")
    earlier: list[tuple[str, Any]] = []
    for job_id, job in sorted(_vte_jobs(store, run_id).items()):
        facts = job_facts(job.output)
        checks = job_checks(job_id, facts, earlier)
        earlier.append((job_id, facts))
        yield job_id, job, job_state(case, job_id, job.output, checks)


# Context: doc://onoda/decision-models-shadow-only@0001#never-grade — shadow answers go to the evaluator store only; the agent never sees them
def shadow_run(
    store: Store,
    evaluator_store: Store,
    run_id: str,
    case: LoadedVteCase,
    model: DecisionModel,
    *,
    ladder: bytes,
    instructions: Mapping[str, str] | None = None,
) -> Evaluation:
    """Ask `model` about every job of `run_id` and record the answers (see the module doc).

    `instructions` replaces the questions' wording (A70: `onoda decide tune`'s
    `best-instructions.json`); the rule options stay the ladder's.
    """
    run = store.run_record(run_id)
    qs = apply_candidate(questions(ladder), instructions or {})
    assignments: dict[str, Any] = {}
    output_refs: dict[str, str] = {}
    costs: list[Decimal] = []
    cost_known = True
    synthetic = "synthetic" in run.labels
    states = list(_job_states(store, run_id, case))
    for job_id, job, state in states:
        answer = model.ask(state, qs)
        synthetic |= answer.synthetic or job.synthetic
        req = evaluator_store.put_bytes(
            answer.request, media_type="application/json", visibility="evaluator"
        )
        resp = evaluator_store.put_bytes(
            answer.response, media_type="application/json", visibility="evaluator"
        )
        output_refs[f"job.{job_id}"] = sha256_digest(job.output)
        output_refs[f"job.{job_id}.request"] = req.digest
        output_refs[f"job.{job_id}.response"] = resp.digest
        rule = answer.answers.get("rule", {})
        probs = rule.get("probabilities") or {}
        assignments[f"job.{job_id}.rule"] = rule.get("choice")
        assignments[f"job.{job_id}.rule.p"] = {k: _p(v) for k, v in sorted(probs.items())}
        ready = answer.answers.get("ready", {}).get("noul")
        assignments[f"job.{job_id}.ready.p"] = None if ready is None else _p(ready)
        if answer.cost_usd is None:
            cost_known = False
        else:
            costs.append(Decimal(answer.cost_usd))
    evaluator = f"vte-shadow-{model.provider}"
    evaluation = Evaluation.new(
        evaluation_id=_next_evaluation_id(evaluator_store, run_id, evaluator, SHADOW_VERSION),
        run_id=run_id,
        evaluator=evaluator,
        evaluator_version=SHADOW_VERSION,
        output_refs=output_refs,
        reference_version=f"ladder:{sha256_digest(ladder)}",  # no reference value is used
        assignments=assignments,
        unresolved=[],
        metrics={
            "provider": model.provider,
            "model": model.model,
            "jobs": str(len(states)),
            "instructions": digest_of(qs) if instructions else "ladder default",
            "synthetic": "true" if synthetic else "false",
            "note": "shadow decisions: never shown to the agent, never a grade",
        },
        usage=Usage(
            units={"decision_calls": len(states)},
            measured=not synthetic,
            simulated=synthetic,
            cost_usd=format(sum(costs, Decimal(0)), "f") if cost_known else None,
        ),
        created_at=evaluator_store.clock.now(),
    )
    evaluator_store.put_record(evaluation)
    return evaluation


_GROUPS = ("identified", "unidentified", "rejected", "uncited")


def _group(outcomes: list[str]) -> str:
    """A job's grader outcome: the best outcome of any claim citing it."""
    for g in ("identified", "unidentified"):
        if g in outcomes:
            return g
    return "rejected" if outcomes else "uncited"


def _cited(evaluator_store: Store, run_id: str) -> dict[str, list[str]]:
    """Job id -> the grader's outcome for each claim of the run that cites it."""
    grade = _latest_vte_evaluation(evaluator_store, run_id)
    cited: dict[str, list[str]] = {}
    for key, job_id in grade.metrics.items():
        if key.startswith("claim.") and key.endswith(".job"):  # see evaluate_vte_claims
            tag = key.removesuffix(".job")
            verdict = grade.assignments.get(tag, "")
            identity = grade.assignments.get(f"{tag}.identity")
            cited.setdefault(job_id, []).append(identity or verdict)
    return cited


def shadow_examples(
    store: Store, evaluator_store: Store, runs: Iterable[tuple[str, LoadedVteCase]]
) -> list[Example]:
    """Labelled examples for `onoda decide tune` (A70), from graded VTE runs.

    One example per job a claim cites: `ready` is "yes" when the grader
    identified a claim from it, "no" when every claim citing it was rejected.
    Jobs nothing cites, or whose claims are only unidentified, carry no
    grader verdict and are left out. The label is the grader's outcome
    word, never a reference value.
    """
    out = []
    for run_id, case in runs:
        cited = _cited(evaluator_store, run_id)
        for job_id, _job, state in _job_states(store, run_id, case):
            group = _group(cited.get(job_id, []))
            if group in ("identified", "rejected"):
                label = "yes" if group == "identified" else "no"
                out.append(Example(f"{run_id}/{job_id}", state, {"ready": label}))
    return out


def _latest_shadows(evaluator_store: Store, run_id: str) -> list[Evaluation]:
    """The latest shadow evaluation of each provider for this run."""
    latest: dict[str, str] = {}
    for rid in sorted(evaluator_store.list_record_ids("evaluation")):
        head, _, _ = rid.rpartition(".")
        if head.startswith(f"{run_id}.vte-shadow-"):
            latest[head] = rid
    out = []
    for rid in latest.values():
        ev = evaluator_store.get_record("evaluation", rid)
        assert isinstance(ev, Evaluation)
        out.append(ev)
    return out


def shadow_report(store: Store, evaluator_store: Store, run_ids: Iterable[str]) -> dict[str, Any]:
    """Shadow picks per arm and provider, set beside the grader's verdict on each job.

    Each job falls in one group by the best grader outcome of any claim
    citing it: `identified`, `unidentified` (supported but not pinned to a
    reference row), `rejected` (contradicted or unsupported) or `uncited`.
    `ready_p` is averaged per group, so the report shows whether the model's
    "ready" separates jobs the grader accepted from jobs it rejected. Counts
    and means only; no provider is declared better.
    """
    arms: dict[str, dict[str, Any]] = {}
    per_run: list[dict[str, Any]] = []
    synthetic = False
    for run_id in run_ids:
        run = store.run_record(run_id)
        arm = run.config["variant"]
        cited = _cited(evaluator_store, run_id)
        for ev in _latest_shadows(evaluator_store, run_id):
            synthetic |= ev.metrics.get("synthetic") == "true"
            a = arms.setdefault(arm, {}).setdefault(
                ev.metrics["provider"],
                {"jobs": 0, "rules": {}, "ready_p": {g: [] for g in _GROUPS}},
            )
            jobs = []
            for key, rule in sorted(ev.assignments.items()):
                if not (key.startswith("job.") and key.endswith(".rule")):
                    continue
                job_id = key.removeprefix("job.").removesuffix(".rule")
                ready = ev.assignments.get(f"job.{job_id}.ready.p")
                verdicts = cited.get(job_id, [])
                group = _group(verdicts)
                a["jobs"] += 1
                a["rules"][rule or "none"] = a["rules"].get(rule or "none", 0) + 1
                if ready is not None:
                    a["ready_p"][group].append(Decimal(ready))
                jobs.append(
                    {"job": job_id, "rule": rule, "ready_p": ready, "grader": verdicts or None}
                )
            per_run.append(
                {"run": run_id, "arm": arm, "provider": ev.metrics["provider"], "jobs": jobs}
            )
    for providers in arms.values():
        for a in providers.values():
            a["ready_p"] = {g: mean_ev(v) for g, v in a["ready_p"].items()}
    return {
        "arms": arms,
        "runs": per_run,
        "grader": VTE_EVALUATOR,
        "synthetic": synthetic,
        "note": (
            "synthetic shadow answers or runs: not a model or chemistry result"
            if synthetic
            else "shadow decisions beside the grader's verdicts; never a grade"
        ),
    }
