"""Mutation audit of the VTE grader: corrupt real claims and count what it still credits.

For each claimed state the grader supports, the audit grades the state alone
and then a set of known-wrong variants of it (a nudged energy, the other spin,
another irrep, the CASSCF value, a job that did not run or did not converge, a
missing character, the state claimed twice). A variant that still leaves an
identified row within tolerance is a false accept. The grader's verdicts are
read, never changed: every grade goes to a throwaway evaluator store, so an
audit writes nothing to the run's records.

One variant is measured, not expected to fail: `label_swap` gives the state
the character of another reference row of its spin and irrep. The grader
takes the character from the agent (the output does not print it), so a
swapped label that lands within tolerance is credited; the audit counts how
often that happens.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from onoda.adapters.pi.ingest import run_vte_claim
from onoda.adapters.pi.vte import VteJob, _vte_jobs, evaluate_vte_claims
from onoda.adapters.pi.vte_contract import ClaimedVte, VteClaim
from onoda.clock import LogicalClock
from onoda.science.orca_parser import parse_orca_output
from onoda.science.vte import (
    LoadedVteCase,
    VteReferences,
    normalize_character,
    normalize_irrep,
)
from onoda.store import Store

# Appended to a job's output: ORCA's own failure line, which blocks the job (`_BLOCKING_FIELDS`).
_CASSCF_FAILURE = b"\nERROR (ORCA_CASSCF): Convergence Failure\n"
MEASURED_ONLY = frozenset({"label_swap"})

Jobs = dict[str, VteJob]
Variant = tuple[str, tuple[ClaimedVte, ...], Jobs]


@dataclass
class AuditReport:
    """Counts per variant: graded, and false accepts (an identified row within tolerance)."""

    states: int = 0  # supported claimed states the variants were built from
    baseline_accurate: int = 0  # of those, graded alone, identified within tolerance
    graded: dict[str, int] = field(default_factory=dict)
    accepted: dict[str, int] = field(default_factory=dict)
    examples: list[dict[str, str]] = field(default_factory=list)  # first few false accepts

    def add(self, name: str, accepted: bool, example: dict[str, str]) -> None:
        self.graded[name] = self.graded.get(name, 0) + 1
        self.accepted[name] = self.accepted.get(name, 0) + accepted
        if accepted and name not in MEASURED_ONLY and len(self.examples) < 20:
            self.examples.append({"variant": name, **example})

    @property
    def false_accepts(self) -> int:
        return sum(n for k, n in self.accepted.items() if k not in MEASURED_ONLY)

    def as_dict(self) -> dict[str, object]:
        return {
            "states": self.states,
            "baseline_accurate": self.baseline_accurate,
            "variants": {
                k: {"graded": self.graded[k], "accepted": self.accepted[k]}
                for k in sorted(self.graded)
            },
            "false_accepts": self.false_accepts,
            "measured_only": sorted(MEASURED_ONLY),
            "examples": self.examples,
        }


def _with(cs: ClaimedVte, **update: object) -> ClaimedVte:
    data = cs.model_dump()
    data.update(update)
    return ClaimedVte.model_validate(data)


def _energy(cs: ClaimedVte, text: str) -> ClaimedVte:
    return _with(cs, energy={"text": text, "unit": "eV"})


def _casscf_value(cs: ClaimedVte, job: VteJob) -> str | None:
    """The CASSCF energy ORCA printed for the root whose PT2 energy `cs` cites, if any."""
    states = parse_orca_output(job.output).mr_states
    irrep = normalize_irrep(cs.irrep)

    def same(s) -> bool:
        return s.multiplicity == cs.spin and normalize_irrep(s.symmetry or "") == irrep

    roots = {
        s.root
        for s in states
        if s.method != "CASSCF"
        and same(s)
        and s.energy_ev is not None
        and Decimal(s.energy_ev.value) == Decimal(cs.energy.text)
    }
    for s in states:
        if s.method == "CASSCF" and same(s) and s.root in roots and s.energy_ev is not None:
            if Decimal(s.energy_ev.value) != Decimal(cs.energy.text):
                return s.energy_ev.value
    return None


def variants(cs: ClaimedVte, jobs: Jobs, references: VteReferences) -> Iterator[Variant]:
    """Known-wrong versions of a supported claimed state, each with the jobs it is graded on."""
    job = jobs[cs.job_id]
    nudged = format(Decimal(cs.energy.text) + Decimal("0.001"), "f")
    yield "energy_nudged", (_energy(cs, nudged),), jobs
    yield "other_spin", (_with(cs, spin=3 if cs.spin == 1 else 1),), jobs
    irrep = normalize_irrep(cs.irrep)
    for other in sorted({r.irrep for r in references.rows} - {irrep}):
        yield "other_irrep", (_with(cs, irrep=other),), jobs
    casscf = _casscf_value(cs, job)
    if casscf is not None:
        yield "casscf_value", (_energy(cs, casscf),), jobs
    for other_id in sorted(set(jobs) - {cs.job_id}):
        yield "other_job", (_with(cs, job_id=other_id),), jobs
    yield "job_not_in_run", (cs,), {k: v for k, v in jobs.items() if k != cs.job_id}
    not_run = job._replace(executed_here=False, cache_hit=False)
    yield "job_not_executed", (cs,), {**jobs, cs.job_id: not_run}
    unconverged = job._replace(output=job.output + _CASSCF_FAILURE)
    yield "casscf_not_converged", (cs,), {**jobs, cs.job_id: unconverged}
    yield "no_character", (_with(cs, character=""),), jobs
    yield "duplicate", (cs, cs), jobs
    character = normalize_character(cs.character)
    for r in references.rows:
        if r.spin == cs.spin and r.irrep == irrep and r.character != character:
            yield "label_swap", (_with(cs, character=r.character),), jobs


def audit_claim(
    report: AuditReport,
    store: Store,
    *,
    run_id: str,
    case: LoadedVteCase,
    references: VteReferences,
    claim: VteClaim | None,
    jobs: Mapping[str, VteJob],
) -> None:
    """Grade every supported state of `claim` alone and as each variant; add to `report`."""
    run_jobs = dict(jobs)
    count = [0]

    def grade(states: Iterable[ClaimedVte], graded_jobs: Jobs):
        count[0] += 1
        return evaluate_vte_claims(
            store,
            run_id=f"{run_id}.audit{count[0]:05d}",
            case=case,
            references=references,
            claim=VteClaim(case_id=case.manifest.case_id, states=tuple(states)),
            jobs=graded_jobs,
        )

    for cs in claim.states if claim is not None else ():
        alone = grade((cs,), run_jobs)
        if alone.assignments["claim.01"] != "supported":
            continue
        report.states += 1
        report.baseline_accurate += int(alone.metrics["accurate"]) > 0
        for name, states, graded_jobs in variants(cs, run_jobs, references):
            ev = grade(states, graded_jobs)
            example = {
                "run_id": run_id,
                "case_id": case.manifest.case_id,
                "claimed": f"{cs.spin} {cs.irrep} {cs.character} {cs.energy.text} {cs.job_id}",
            }
            report.add(name, int(ev.metrics["accurate"]) > 0, example)


def audit_runs(
    runs: Iterable[tuple[Store, str]],
    case_for: Callable[[str], tuple[LoadedVteCase, VteReferences]],
) -> AuditReport:
    """Audit the grader over recorded VTE runs (`store`, `run_id`); `case_for(case_id)` loads
    the case and its references on the evaluator side. Writes only to a temporary store."""
    report = AuditReport()
    with tempfile.TemporaryDirectory(prefix="onoda-vte-audit-") as tmp:
        scratch = Store(tmp, clock=LogicalClock(), allowed_visibility=("evaluator",))
        for store, run_id in runs:
            case, references = case_for(store.run_record(run_id).config["case_id"])
            audit_claim(
                report,
                scratch,
                run_id=run_id,
                case=case,
                references=references,
                claim=run_vte_claim(store, run_id),
                jobs=_vte_jobs(store, run_id),
            )
    return report
