"""Deterministic ladder checks on an agent's ORCA job output (A66).

The QUESTDB ladder's validation criteria include three that need no judgement,
only numbers ORCA prints, so the broker reads them from every job's log and
shows the result next to it instead of leaving the agent to dig through
hundreds of kilobytes:

1. Criterion 1: the lowest SA-CASSCF root lies below the converged
   Hartree-Fock energy of the same basis and geometry. The HF energy may come
   from this job or any earlier job of the run whose basis line and Cartesian
   coordinates are printed identically. Without one the check says so.
2. Criterion 3: the active orbitals' irreps, counted per irrep as ORCA prints
   them. What the agent intended is not in the input, so this is a summary
   to compare against, never a verdict.
3. State-average weights: ORCA prints each root's effective weight. QUESTDB's
   SC-NEVPT2 references (Molpro) weight every root equally, while ORCA splits
   weight by block first, so unequal weights are flagged (the paper's largest
   replication error, arXiv 2609.13357).
4. Root order: SC-NEVPT2 lists each block's roots by corrected energy. If that
   order differs from the SA-CASSCF root order, the perturbation reordered the
   states, so they cannot be identified by position and the CASSCF reference
   may describe them poorly (A68).

These are advisory text for the agent. They are not observations, the grader
never reads them, and they use no reference value. Values are compared as
`Decimal` over the printed text; lines of ORCA's input echo (`|  12> ...`),
which the agent wrote, are never read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from onoda.science.orca_parser import parse_orca_output

_NUMBER = r"-?\d+\.\d+"
_INPUT_ECHO = re.compile(r"^\|\s*\d+>")
_BASIS = re.compile(r"^Your calculation utilizes the basis: (.+?)\s*$")
_COORDS = "CARTESIAN COORDINATES (ANGSTROEM)"
_HF_METHOD = re.compile(r"^\s*Ab initio Hamiltonian\s+Method\s+\.+\s+Hartree-Fock\b")
_SCF_OK = re.compile(r"\bSCF CONVERGED AFTER\s+\d+\s+CYCLES\b")
_SCF_TOTAL = re.compile(rf"^Total Energy\s+:\s+({_NUMBER}) Eh\b")
_LOWEST = re.compile(rf"^LOWEST ROOT \(.*\)\s+=\s+({_NUMBER}) Eh\b")
_FINAL_CAS = re.compile(rf"^Final CASSCF energy\s+:\s+({_NUMBER}) Eh\b")
_ACTIVE_IRREP = re.compile(r"^\s*Irrep\s+(\S+) has\s+\d+ SALCs .*#\(active\)=\s*(\d+)\s*$")
_BLOCK = re.compile(r"^BLOCK\s+\d+ WEIGHT=")
_BLOCK_MULT = re.compile(r"^\s*Multiplicity\s+\.+\s+(\d+)\s*$")
_BLOCK_IRREP = re.compile(r"^\s*Irrep\s+\.+\s+\d+ \((\S+)\)\s*$")
_ROOT_WEIGHT = re.compile(r"^\s*ROOT=(\d+) WEIGHT=\s*(\d+\.\d+)\s*$")
_NEVPT2_LISTING = "NEVPT2 TRANSITION ENERGIES"
# `STATE: ROOT MULT [IRREP] DE/a.u. DE/eV DE/cm**-1`; IRREP is printed only with symmetry.
_TRANSITION = re.compile(
    rf"^\s*\d+:\s+(\d+)\s+(\d+)\s+(?:(\S+)\s+)?({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*$"
)


@dataclass(frozen=True)
class RootWeight:
    multiplicity: int | None
    irrep: str | None
    root: int
    weight: str  # decimal text as printed


@dataclass(frozen=True)
class Transition:
    multiplicity: int
    irrep: str | None
    root: int
    au: str  # excitation energy, Eh, decimal text as printed
    ev: str  # the same in eV, as printed


@dataclass(frozen=True)
class JobFacts:
    """What the checks need from one job's log, read once when the job returns."""

    frame: tuple[str, tuple[str, ...]] | None  # (basis, coordinate lines) as printed
    hf_energy: str | None  # converged Hartree-Fock total energy, Eh
    casscf_ground: str | None  # lowest SA-CASSCF root, Eh, converged CASSCF only
    active_irreps: tuple[tuple[str, int], ...]  # first listing, irreps with active orbitals
    root_weights: tuple[RootWeight, ...]  # first CI-step listing
    nevpt2_transitions: tuple[Transition, ...] = ()  # SC-NEVPT2 listing, as printed


def job_facts(log: bytes) -> JobFacts:
    lines = [
        ln.rstrip()
        for ln in log.decode("utf-8", errors="replace").splitlines()
        if not _INPUT_ECHO.match(ln)
    ]
    weights = _root_weights(lines)
    return JobFacts(
        frame=_frame(lines),
        hf_energy=_hf_energy(lines),
        casscf_ground=_casscf_ground(lines, log, weights),
        active_irreps=_active_irreps(lines),
        root_weights=weights,
        nevpt2_transitions=_nevpt2_transitions(lines),
    )


def _first(pattern: re.Pattern[str], lines: Sequence[str]) -> re.Match[str] | None:
    for ln in lines:
        if m := pattern.search(ln):
            return m
    return None


def _frame(lines: list[str]) -> tuple[str, tuple[str, ...]] | None:
    basis = _first(_BASIS, lines)
    try:
        start = lines.index(_COORDS) + 2  # header, dashes
    except ValueError:
        return None
    coords = []
    for ln in lines[start:]:
        if not ln.strip():
            break
        coords.append(" ".join(ln.split()))
    if basis is None or not coords:
        return None
    return basis.group(1), tuple(coords)


def _hf_energy(lines: list[str]) -> str | None:
    if _first(_HF_METHOD, lines) is None or _first(_SCF_OK, lines) is None:
        return None
    try:
        start = lines.index("TOTAL SCF ENERGY")
    except ValueError:
        return None
    m = _first(_SCF_TOTAL, lines[start : start + 6])
    return m.group(1) if m else None


def _casscf_ground(lines: list[str], log: bytes, weights: tuple[RootWeight, ...]) -> str | None:
    # The parser's convergence verdict, so a check never trusts a CASSCF the grader rejects.
    if parse_orca_output(log).casscf_converged is not True:
        return None
    if m := _first(_LOWEST, lines):
        return m.group(1)
    # A single-root CASSCF prints no transition listing; its final energy is that root's.
    if len(weights) == 1 and (m := _first(_FINAL_CAS, lines)):
        return m.group(1)
    return None


def _active_irreps(lines: list[str]) -> tuple[tuple[str, int], ...]:
    found: list[tuple[str, int]] = []
    for ln in lines:
        if m := _ACTIVE_IRREP.match(ln):
            found.append((m.group(1), int(m.group(2))))
        elif found:
            break
    return tuple((irrep, n) for irrep, n in found if n)


def _root_weights(lines: list[str]) -> tuple[RootWeight, ...]:
    try:
        start = lines.index("CI-STEP:")
    except ValueError:
        return ()
    weights: list[RootWeight] = []
    mult: int | None = None
    irrep: str | None = None
    for ln in lines[start + 1 :]:
        if _BLOCK.match(ln):
            mult = irrep = None
        elif m := _BLOCK_MULT.match(ln):
            mult = int(m.group(1))
        elif m := _BLOCK_IRREP.match(ln):
            irrep = m.group(1)
        elif m := _ROOT_WEIGHT.match(ln):
            weights.append(RootWeight(mult, irrep, int(m.group(1)), m.group(2)))
        elif ln.strip().startswith("PrintLevel"):
            break
    return tuple(weights)


def _nevpt2_transitions(lines: list[str]) -> tuple[Transition, ...]:
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _NEVPT2_LISTING)
    except StopIteration:
        return ()
    found: list[Transition] = []
    for ln in lines[start + 1 :]:
        if m := _TRANSITION.match(ln):
            mult, irrep = int(m.group(2)), m.group(3)
            found.append(Transition(mult, irrep, int(m.group(1)), m.group(4), m.group(5)))
        elif found and not ln.strip():
            break
    return tuple(found)


def job_checks(
    job_id: str, facts: JobFacts, earlier: Sequence[tuple[str, JobFacts]]
) -> tuple[str, ...]:
    """The check lines for `job_id`, given the run's earlier jobs (oldest first).

    A job that ran no CASSCF gets no lines.
    """
    if not facts.root_weights and facts.casscf_ground is None:
        return ()
    return tuple(
        line
        for line in (
            _check_hf(job_id, facts, earlier),
            _check_irreps(facts),
            _check_weights(facts),
            _check_root_order(facts),
        )
        if line
    )


def _check_hf(job_id: str, facts: JobFacts, earlier: Sequence[tuple[str, JobFacts]]) -> str | None:
    if facts.casscf_ground is None:
        return None  # not converged, or no lowest root printed: nothing to compare
    source = None
    for jid, f in [*earlier, (job_id, facts)]:
        if f.hf_energy is not None and f.frame is not None and f.frame == facts.frame:
            source = (jid, f.hf_energy)  # the latest one wins
    if source is None:
        return (
            "not checked (criterion 1, CASSCF below HF): no converged Hartree-Fock job with the "
            "same basis and geometry in this run"
        )
    jid, hf = source
    where = "this job" if jid == job_id else f"job {jid}"
    if Decimal(facts.casscf_ground) < Decimal(hf):
        return (
            f"ok (criterion 1): lowest CASSCF root {facts.casscf_ground} Eh is below "
            f"HF {hf} Eh ({where}, same basis and geometry)"
        )
    return (
        f"WARNING (criterion 1): lowest CASSCF root {facts.casscf_ground} Eh is not below "
        f"HF {hf} Eh ({where}, same basis and geometry); this CASSCF solution is suspect"
    )


def _check_irreps(facts: JobFacts) -> str | None:
    if not facts.active_irreps:
        return None  # symmetry off: ORCA prints no irreps for the active orbitals
    counts = ", ".join(f"{irrep} {n}" for irrep, n in facts.active_irreps)
    return f"active orbitals by irrep (criterion 3, compare with the space you intended): {counts}"


def _check_weights(facts: JobFacts) -> str | None:
    weights = facts.root_weights
    if len(weights) < 2:
        return None
    if len({Decimal(w.weight) for w in weights}) == 1:
        n, each = len(weights), weights[0].weight
        return f"ok (state averaging): {n} roots weighted equally ({each} each)"
    listed = "; ".join(
        f"mult {w.multiplicity}{f' {w.irrep}' if w.irrep else ''} root {w.root}: {w.weight}"
        for w in weights
    )
    return (
        f"WARNING (state averaging): root weights are not equal ({listed}). QUESTDB's references "
        "weight every root equally; ORCA splits the weight between blocks first, so set each "
        "block's weight in proportion to its number of roots"
    )


def _check_root_order(facts: JobFacts) -> str | None:
    if not facts.nevpt2_transitions:
        return None
    blocks: dict[tuple[int, str | None], list[Transition]] = {}
    for t in facts.nevpt2_transitions:
        blocks.setdefault((t.multiplicity, t.irrep), []).append(t)
    swapped = []
    for (mult, irrep), roots in blocks.items():
        by_energy = sorted(roots, key=lambda t: Decimal(t.au))
        if [t.root for t in by_energy] != sorted(t.root for t in roots):
            listed = ", ".join(f"root {t.root} {t.ev} eV" for t in by_energy)
            swapped.append(f"mult {mult}{f' {irrep}' if irrep else ''}: {listed}")
    if not swapped:
        return "ok (root order): SC-NEVPT2 keeps the SA-CASSCF root order in every block"
    return (
        f"WARNING (root order): SC-NEVPT2 reorders the SA-CASSCF roots ({'; '.join(swapped)}). "
        "Identify each state by its CASSCF root's character, not by its position; the CASSCF "
        "reference may describe these states poorly"
    )
