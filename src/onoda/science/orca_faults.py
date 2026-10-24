"""Fault plans for the ORCA broker: serve a known failure in place of one job's output.

A run may carry a `FaultPlan` (opt-in, off by default). The broker still runs
the triggering job for real, then serves the agent the real output cut by a
named, versioned transform, and journals both (broker-fault-injection). v1 has
one fault and one trigger:

- trigger `first-eligible-nevpt2`: the first submission whose input requests
  NEVPT2 and is `eligible` (a single-point energy, by a positive allowlist);
- fault `killed-before-nevpt2`: keep the output before the first NEVPT2
  section header and serve it as `failed` / `timeout`, offering only
  `job.gbw`. It fires only over a job the parser calls a success (normal
  termination, CASSCF converged); otherwise the trigger is consumed and the
  run is `not-fired` with the reason.

Everything here is a pure function of input or output bytes, so the same
input always gets the same verdict.
"""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import Field, ValidationError

from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import OnodaError
from onoda.records import Frozen as _Frozen
from onoda.science.orca_parser import parse_orca_output

FAULT_PLAN_SCHEMA = "onoda.fault-plan/1"
FAULT_LABEL = "fault-injected"
# Bump when a transform's output for the same bytes would change.
TRANSFORM_VERSION = "1"
# What a real wall-time kill gets from the broker (`OrcaLocalScheduler`'s reason).
SERVED_STATUS: Literal["failed"] = "failed"
SERVED_REASON = "timeout"
# The products a kill before NEVPT2 leaves: the CASSCF orbitals, nothing else.
SERVED_FILES = frozenset({"job.gbw"})


class FaultPlan(_Frozen):
    """One run's fault plan; its canonical bytes are what the run config pins."""

    schema_: Literal["onoda.fault-plan/1"] = Field(FAULT_PLAN_SCHEMA, alias="schema")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    fault: Literal["killed-before-nevpt2"]
    trigger: Literal["first-eligible-nevpt2"]


def load_fault_plan(data: bytes) -> tuple[FaultPlan, bytes]:
    """Parse a plan file (YAML or JSON); returns the plan and its canonical bytes."""
    try:
        raw = yaml.safe_load(data)
        plan = FaultPlan.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise OnodaError(f"not a fault plan: {exc}") from exc
    return plan, canonical_bytes(plan.model_dump(mode="json", by_alias=True))


def plan_digest(canonical: bytes) -> str:
    return sha256_digest(canonical)


# --- the trigger: which inputs a fault may apply to ------------------------------------

# Bases by name: the Dunning and Karlsruhe families the QUESTDB cases use. Not
# `orca_input`'s free-form pattern, so a run-type keyword passed as a basis is not one.
ELIGIBLE_BASES = (
    *(f"{p}cc-pV{z}Z" for p in ("", "aug-") for z in "DTQ"),
    "def2-SVP",
    "def2-SVPD",
    "def2-TZVP",
    "def2-TZVPP",
    "def2-TZVPD",
    "def2-QZVP",
    "ma-def2-SVP",
    "ma-def2-TZVP",
)
_BASES = frozenset(b.lower() for b in ELIGIBLE_BASES)
# `!` keywords other than a basis that leave a single point a single point.
ELIGIBLE_KEYWORDS = frozenset(
    k.lower()
    for k in (
        # method and run type (no run-type keyword is a single point)
        "RHF",
        "SP",
        "Energy",
        # RI and auxiliary bases
        "RIJCOSX",
        "RIJK",
        "RIJONX",
        "NoCOSX",
        "AutoAux",
        "def2/J",
        "def2/JK",
        # grids
        "DefGrid1",
        "DefGrid2",
        "DefGrid3",
        # convergence
        "NormalSCF",
        "TightSCF",
        "VeryTightSCF",
        # symmetry
        "UseSym",
        "NoUseSym",
        # print
        "MiniPrint",
        "SmallPrint",
        "NormalPrint",
        "LargePrint",
        "PrintBasis",
        "PrintMOs",
        # orbitals
        "MORead",
    )
)
ELIGIBLE_BLOCKS = frozenset({"casscf", "scf", "pal", "maxcore", "moinp", "output"})

_NEVPT2_RE = re.compile(r"nevpt2", re.IGNORECASE)
_PTMETHOD_RE = re.compile(r"^\s*ptmethod\s+(\S+)\s*$", re.IGNORECASE)
_BLOCK_RE = re.compile(r"%\s*(\w*)")
_FORBIDDEN_RE = re.compile(r"runtyp|\$new_job", re.IGNORECASE)


def _code_lines(text: str) -> list[str] | None:
    """The input's lines without whole-line `#` comments; None if a `#` is anywhere else.

    ORCA treats `#` as a comment to the end of the line; an inline one is legal
    but `orca_input` never writes one, so it fails closed rather than be parsed.
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "#" in line:
            return None
        lines.append(line)
    return lines


def requests_nevpt2(text: str) -> bool:
    """Whether the input asks for NEVPT2 anywhere outside a comment, in any spelling."""
    lines = _code_lines(text)
    body = "\n".join(lines) if lines is not None else text
    return bool(_NEVPT2_RE.search(body))


def ineligible(text: str) -> str | None:
    """Why this NEVPT2 input may not carry the fault, or None if it is an eligible single point.

    A positive allowlist (broker-fault-injection): every `!` keyword is a pinned
    basis or keyword, every `%` block or directive is a pinned one, NEVPT2 is
    requested only as `PTMethod <x>NEVPT2` inside `%casscf`, and nothing sets a
    run type or starts a second job. Anything not anticipated is ineligible.
    """
    if not requests_nevpt2(text):
        return "no NEVPT2 requested"
    lines = _code_lines(text)
    if lines is None:
        return "an inline # comment"
    body = "\n".join(lines)
    if m := _FORBIDDEN_RE.search(body):
        return f"{m.group(0)!r} in the input"
    bases = 0
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("!"):
            continue
        for token in stripped[1:].split():
            t = token.lower()
            if t in _BASES:
                bases += 1
            elif t not in ELIGIBLE_KEYWORDS:
                return f"keyword {token!r} is not on the single-point list"
    if bases != 1:
        return f"{bases} listed basis sets on the ! lines, need exactly one"
    for m in _BLOCK_RE.finditer(body):
        if m.group(1).lower() not in ELIGIBLE_BLOCKS:
            return f"%{m.group(1)} is not on the block list"
    in_casscf = False
    pt_methods = []
    for line in lines:
        s = line.strip().lower()
        if s.startswith("%casscf"):
            in_casscf = True
        elif in_casscf and s == "end":
            in_casscf = False
        elif _NEVPT2_RE.search(line):
            pm = _PTMETHOD_RE.match(line)
            if not in_casscf or pm is None:
                return "NEVPT2 is requested other than by PTMethod in %casscf"
            pt_methods.append(pm.group(1))
    if len(pt_methods) != 1:
        return f"{len(pt_methods)} PTMethod lines, need one"
    return None


# --- the fault: what the agent is served -----------------------------------------------

_NEVPT2_HEADER_RE = re.compile(rb"^-{20,}\r?\n[ \t]*< NEVPT2  >[ \t]*\r?$", re.MULTILINE)


def cut_before_nevpt2(log: bytes) -> bytes | None:
    """The output before the first NEVPT2 section header (its dashed rule), or None if absent."""
    m = _NEVPT2_HEADER_RE.search(log)
    return log[: m.start()] if m else None


def not_a_success(log: bytes) -> str | None:
    """Why the deterministic parser does not call this job a success, or None.

    A success is normal termination with a converged CASSCF; the scheduler's
    status is not consulted (orca-parser-authoritative).
    """
    obs = parse_orca_output(log)
    if obs.termination != "normal":
        return f"termination {obs.termination}"
    if obs.casscf_converged is not True:
        return "CASSCF not converged" if obs.casscf_converged is False else "no CASSCF verdict"
    return None
