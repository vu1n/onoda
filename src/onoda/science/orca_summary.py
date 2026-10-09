"""A job's summary: what the deterministic parser read from one ORCA output (A74).

The broker sends these lines with every reply in place of the agent reading
hundreds of kilobytes, and the Pi side keeps them as the run's job ledger.
Every value is the parser's own (`parse_orca_output`, the one the grader
uses) or a line ORCA printed, with its line number in the output so the
agent can open exactly that place. Nothing here reads a reference value, and
nothing grades: a state listed here may still fail grading (wrong state,
wrong character), and a state missing here cannot pass it.
"""

from __future__ import annotations

import re

from onoda.science.contract import ExcitedState
from onoda.science.orca_parser import parse_orca_output
from onoda.science.orca_states import casscf_state_summary

_INPUT_ECHO = re.compile(r"^\|\s*\d+>")
_USESYM = re.compile(r"^\s*Symmetry handling\s+UseSym\s+\.+\s+(\S+)\s*$")
_POINT_GROUP = re.compile(r"^\s*Point group\s+\.+\s+(\S+)\s*$")
_SPIN = {1: "singlet", 2: "doublet", 3: "triplet", 4: "quartet", 5: "quintet"}
MAX_STATE_LINES = 40  # per method; the rest is counted, not listed


def job_summary(log: bytes) -> tuple[str, ...]:
    """The summary lines for one job's output, in a fixed order."""
    obs = parse_orca_output(log)
    lines = [f"termination: {obs.termination}"]
    if obs.final_energy is not None:
        q = obs.final_energy
        lines.append(f"final single point energy: {q.value} {q.units} (line {q.evidence.line})")
    lines.append(_symmetry(log))
    if obs.casscf_converged is not None:
        lines.append(f"CASSCF: {'converged' if obs.casscf_converged else 'NOT converged'}")
    by_method: dict[str, list[ExcitedState]] = {}
    for s in obs.mr_states:
        by_method.setdefault(s.method or "unknown method", []).append(s)
    for method, states in by_method.items():
        lines.append(f"{method} transition energies (line numbers in this output):")
        lines.extend(_state(s) for s in states[:MAX_STATE_LINES])
        if len(states) > MAX_STATE_LINES:
            lines.append(f"- ... {len(states) - MAX_STATE_LINES} more")
    if obs.casscf_converged is not None:
        lines.extend(casscf_state_summary(log, converged=obs.casscf_converged is True))
    lines.extend(
        f"problem ({p.field}): {p.detail}" + (f" (line {p.evidence.line})" if p.evidence else "")
        for p in obs.problems
        # A CASSCF started from read orbitals prints no SCF; that absence is not news.
        if not (p.field == "scf" and obs.casscf_converged is not None)
    )
    return tuple(lines)


def _symmetry(log: bytes) -> str:
    use = group = None
    for ln in log.decode("utf-8", errors="replace").splitlines():
        if _INPUT_ECHO.match(ln):
            continue  # the agent's own input, not what ORCA did
        if use is None and (m := _USESYM.match(ln)):
            use = m.group(1)
        elif group is None and (m := _POINT_GROUP.match(ln)):
            group = m.group(1)
        if use is not None and group is not None:
            break
    if use in ("ON", "1"):
        return f"symmetry: on, point group {group or 'not printed'}"
    return "symmetry: not used (no UseSym ON printed), so states print no irrep"


def _state(s: ExcitedState) -> str:
    spin = _SPIN.get(s.multiplicity or 0, f"multiplicity {s.multiplicity}")
    irrep = s.symmetry or "no irrep printed"
    ev = s.energy_ev
    energy = f"{ev.value} {ev.units}" if ev else f"{s.energy_au.value} {s.energy_au.units}"
    return f"- {spin} {irrep} root {s.root}: {energy} (line {s.energy_au.evidence.line})"
