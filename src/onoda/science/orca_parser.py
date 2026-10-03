"""Deterministic ORCA 5.x/6.x text-output parser producing an `OrcaObservation`.

Pure function over the output bytes; no solver, network, or float arithmetic.

Honesty rules:

- Every value is the exact decimal text ORCA printed, with its units and the
  line it came from. Numbers must match a strict fixed-point pattern; anything
  else (`0.19x688`, `1e5`, `nan`) is an `error` Problem and the value is omitted.
- Units are checked against the token printed next to each value (`au`, `eV`,
  `cm**-1`). An unexpected unit token is an `error` Problem; nothing is ever
  converted.
- Absence is explicit: a missing version, termination, SCF result, or final
  energy yields a `Problem`, never a default.
- The last line of a file that does not end in a newline is cut off; a value
  read from it is an `error` Problem, not a parsed value.
- Output without normal termination may stop mid-listing, so any excited-state
  listing in it carries an `unresolved` Problem.

Excited states come from TD-DFT/CIS `STATE n: E= ...` lines and MDCI
(EOM/ADC/STEOM) `IROOT= n: ... au ... eV ... cm**-1` lines, which ORCA 6.1.1
ends with `<S**2> = value` for STEOM roots (A56). Each state line is
attributed to the most recent section header (`... EXCITED STATES (SINGLETS)`,
`... RESULTS (RHS)`). Repeated listings of the same root (e.g. ORCA's
"APPROXIMATE EOM LHS" reprint) are deduplicated only when every printed value
is identical; a conflicting repeat makes the status `ambiguous` and the root is
dropped rather than guessed.

STEOM-CCSD output (checked against the vendored ORCA 6.0.1 log) prints, in
order: a `CIS RESULTS` listing (the CIS guess used to pick the active space),
two `EOM-CCSD RESULTS (RHS)` listings preceded by `EOM type ... IP-EOM` and
`EOM type ... EA-EOM` (ionisation/attachment roots, not excitation energies),
then `STEOM-CCSD RESULTS (SINGLETS)`. The parser drops IP/EA listings whenever
their `EOM type` line says so, and drops `CIS RESULTS` when a STEOM listing is
present, reporting only the STEOM roots. Any other mix of methods is ambiguous.

Multireference jobs (checked against real ORCA 6.1.1 output; graded per A58) fill
`mr_states` from two listings, each a `LOWEST ROOT (...)` line, a column
header and `n: ROOT MULT [IRREP] DE/a.u. DE/eV DE/cm**-1` rows, whose bare
numbers take their units from that exact header:
`SA-CASSCF TRANSITION ENERGIES` (method `CASSCF`) and `NEVPT2 TRANSITION
ENERGIES`, whose method is the one ORCA names in `(PT2 = SC-NEVPT2)`. The
IRREP column is printed only when symmetry is on; without it `symmetry` is
None. A state is keyed by (method, multiplicity, irrep, root), never by its
position or energy order (ORCA 6.1.1 prints NEVPT2 roots by energy, which
can reorder them against CASSCF). A listing without a `LOWEST ROOT` line, with
an unexpected column header, or (for NEVPT2) without a PT2 method line is
dropped with an `error` Problem. When CASSCF did not converge (including
when ORCA still printed normal termination) or its convergence is not
printed, no state is reported and `mr_states` carries an `unresolved` Problem.
`casscf_converged` and the PT2 method are read from ORCA's own lines, never
from its echo of the input (`|  12> ...`), which is text the agent wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from onoda.canonical import sha256_digest
from onoda.science.contract import (
    Evidence,
    ExcitedState,
    OrcaObservation,
    Problem,
    Quantity,
)

# @2 reads the `<S**2> = value` suffix ORCA 6.1.1 prints on STEOM `IROOT` lines
# (A56), which @1 reported as an error. @3 adds `casscf_converged` and
# `mr_states` from the same bytes. Records written by @1/@2 keep their id.
PARSER_ID = "orca-parser@3"

_NUMBER = re.compile(r"-?\d+\.\d+")
_VERSION = re.compile(r"^\s*Program Version (\d+\.\d+\.\d+)\b")
_NORMAL_END = "****ORCA TERMINATED NORMALLY****"
# ORCA's fatal-error wording ("ORCA finished by error termination in SCF",
# "... aborting the run"). Only these exact phrases count as an error end.
_ERROR_END = re.compile(r"ORCA finished by error termination|aborting the run")
_SCF_OK = re.compile(r"\bSCF CONVERGED AFTER\s+(\d+)\s+CYCLES\b")
_SCF_FAIL = re.compile(r"\bSCF NOT CONVERGED AFTER\s+(\d+)\s+CYCLES\b")
_FINAL = re.compile(r"^FINAL SINGLE POINT ENERGY\s+(\S+)(.*)$")
_EOM_TYPE = re.compile(r"^EOM type\s+\.\.\.\s+(\S+)")
_TD_HEADER = re.compile(r"^(\S+?)[ -]EXCITED STATES(?: \((SINGLETS|TRIPLETS)\))?$")
_MDCI_HEADER = re.compile(r"^([A-Z][A-Z0-9()/-]*) RESULTS(?: \(([A-Z]+)\))?$")
_STATE = re.compile(r"^STATE\s+(\d+):\s+E=\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(.*)$")
_STATE_REST = re.compile(
    r"^\s*(?:<S\*\*2>\s+=\s+(\S+))?(?:\s+Sym:\s+(\S+))?(?:\s+Mult\s+(\d+))?\s*$"
)
_IROOT = re.compile(r"^IROOT=\s*(\d+):(.*)$")
_STATE_ROOT = re.compile(r"^STATE\s+(\d+):")
_CASSCF_BANNER = "ORCA-CASSCF"
_CASSCF_FAIL = re.compile(
    r"ERROR \(ORCA_CASSCF\): Convergence Failure|\bCAS-?SCF\b.*\bNOT CONVERGED\b"
)
_CASSCF_OK = re.compile(r"\bCAS-SCF\b.*\bHAS CONVERGED\b|\bCASSCF CONVERGED AFTER\s+\d+\s+CYCLES\b")
_PT2 = re.compile(r"\(PT2 = (\S+?)\)")
# ORCA echoes the input as `|  12> ...`; the agent writes that text, so a
# comment there must never read as a convergence verdict or a PT2 label.
_INPUT_ECHO = re.compile(r"^\|\s*\d+>")
_MR_HEADERS = {"SA-CASSCF TRANSITION ENERGIES": "CASSCF", "NEVPT2 TRANSITION ENERGIES": None}
_MR_LOWEST = re.compile(
    r"^LOWEST ROOT \(ROOT\s+(\d+)\s*,\s*MULT\s+(\d+)(?:\s*,\s*IRREP\s+(\S+))?\)\s+="
)
_MR_COLUMNS = ["STATE", "ROOT", "MULT", "DE/a.u.", "DE/eV", "DE/cm**-1"]
_MR_ROW = re.compile(r"^\s*(\d+):(.*)$")

_SPIN = {"SINGLETS": 1, "TRIPLETS": 3}
_UNITS = (("au", "au"), ("eV", "eV"), ("cm**-1", "cm-1"))  # printed token -> recorded unit
_NOT_EXCITATIONS = {"IP-EOM", "EA-EOM"}
_FAMILY = {
    "TD-DFT/TDA": "tddft",
    "TD-DFT": "tddft",
    "CIS": "cis",
    "EOM-CCSD": "eom-ccsd",
    "ADC(2)": "adc2",
    "STEOM-CCSD": "steom-ccsd",
}


@dataclass(frozen=True)
class _Section:
    method: str | None
    multiplicity: int | None
    eom_type: str | None
    evidence: Evidence | None


@dataclass
class _MrListing:
    """An open SA-CASSCF or NEVPT2 transition-energy listing."""

    method: str | None
    header: int
    lowest: bool = False
    irrep: bool | None = None  # None until the column header is read
    bad: bool = False


@dataclass(frozen=True)
class _Parsed:
    state: ExcitedState
    section: _Section
    evidence: Evidence

    def values(self) -> tuple:
        s = self.state
        return tuple(q.value if q else None for q in (s.energy_au, s.energy_ev, s.energy_cm)) + (
            s.multiplicity,
            s.symmetry,
        )


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.source = sha256_digest(data)
        self.problems: list[Problem] = []
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Undecodable bytes become U+FFFD, which no value pattern accepts.
            text = data.decode("utf-8", errors="replace")
            self.problem("source", "error", f"output is not valid UTF-8 ({exc.reason})")
        self.lines = [line.rstrip() for line in text.splitlines()]
        self.cut_line = len(self.lines) if text and not text.endswith("\n") else None

    def ev(self, n: int) -> Evidence:
        return Evidence(artifact=self.source, line=n, text=self.lines[n - 1])

    def problem(self, field: str, state: str, detail: str, n: int | None = None) -> None:
        evidence = self.ev(n) if n is not None else None
        self.problems.append(Problem(field=field, state=state, detail=detail, evidence=evidence))

    def cut(self, field: str, n: int) -> bool:
        if n == self.cut_line:
            self.problem(field, "error", "line is cut off (no trailing newline); not read", n)
            return True
        return False

    def quantity(self, field: str, value: str, units: str, n: int) -> Quantity | None:
        if not _NUMBER.fullmatch(value):
            self.problem(field, "error", f"malformed number {value!r}", n)
            return None
        return Quantity(value=value, units=units, evidence=self.ev(n))

    def energies(self, tokens: list[str], n: int) -> list[Quantity] | None:
        """Read `value au value eV value cm**-1`; None (with Problems) if any part is bad."""
        if len(tokens) != 6:
            self.problem(
                "excited_states", "error", "state line does not have 3 value/unit pairs", n
            )
            return None
        out: list[Quantity] = []
        for (printed, units), value, token in zip(_UNITS, tokens[::2], tokens[1::2], strict=True):
            if token != printed:
                self.problem("excited_states", "error", f"unit {token!r} where {printed!r}", n)
                return None
            q = self.quantity("excited_states", value, units, n)
            if q is None:
                return None
            out.append(q)
        return out


def parse_orca_output(data: bytes, *, synthetic: bool = False) -> OrcaObservation:
    """Parse ORCA 5.x/6.x text output. See the module docstring for the rules."""
    r = _Reader(data)
    version: str | None = None
    normal: list[int] = []
    errors: list[int] = []
    scf: tuple[str, int] | None = None  # status, cycles
    final: Quantity | None = None
    final_seen = False
    warnings: list[Evidence] = []
    section = _Section(None, None, None, None)
    sections_seen = 0
    eom_type: str | None = None
    parsed: list[_Parsed] = []
    unreadable: list[tuple[tuple, _Section]] = []  # ((multiplicity, root), section)
    casscf_ran = False
    casscf_ok: list[int] = []
    casscf_fail: list[int] = []
    pt2: str | None = None
    listing: _MrListing | None = None
    mr: list[tuple[ExcitedState, int, _MrListing]] = []  # (state, line, its listing)
    mr_unreadable: set[tuple] = set()

    for n, line in enumerate(r.lines, start=1):
        stripped = line.strip()
        if listing is not None:
            if _mr_line(r, listing, line, n, mr, mr_unreadable):
                continue
            listing = None
        if stripped in _MR_HEADERS:
            listing = _MrListing(_MR_HEADERS[stripped] or pt2, n)
            if listing.method is None:
                listing.bad = True
                r.problem("mr_states", "error", "NEVPT2 listing but no '(PT2 = ...)' line", n)
            continue
        if stripped == _CASSCF_BANNER:
            casscf_ran = True
        if not _INPUT_ECHO.match(line):
            if _CASSCF_FAIL.search(line):
                casscf_fail.append(n)
            elif _CASSCF_OK.search(line):
                casscf_ok.append(n)
            if m := _PT2.search(line):
                pt2 = m.group(1)
        if line.startswith("WARNING"):
            warnings.append(r.ev(n))
        if m := _VERSION.match(line):
            if version is None:
                version = m.group(1)
            elif m.group(1) != version:
                r.problem("program_version", "ambiguous", f"also prints {m.group(1)}", n)
        if _NORMAL_END in line:
            normal.append(n)
        elif _ERROR_END.search(line):
            errors.append(n)
        if m := _SCF_OK.search(line):
            scf = ("converged", int(m.group(1)))
        elif m := _SCF_FAIL.search(line):
            scf = ("not_converged", int(m.group(1)))
        if m := _FINAL.match(line):
            # ORCA prints no unit on this line; its documented unit is the
            # hartree (Eh). The last printed value is the final one.
            final_seen = True
            final = None
            if r.cut("final_energy", n):
                continue
            if m.group(2).strip():
                r.problem("final_energy", "error", f"unexpected text {m.group(2).strip()!r}", n)
            else:
                final = r.quantity("final_energy", m.group(1), "Eh", n)
            continue
        if m := _EOM_TYPE.match(line):
            eom_type = m.group(1)
            continue
        if m := _TD_HEADER.match(stripped):
            section = _Section(m.group(1), _SPIN.get(m.group(2) or ""), None, r.ev(n))
            sections_seen += 1
            continue
        if (m := _MDCI_HEADER.match(stripped)) and m.group(1) != "CASSCF":
            section = _Section(m.group(1), _SPIN.get(m.group(2) or ""), eom_type, r.ev(n))
            eom_type = None
            sections_seen += 1
            continue
        if m := _STATE_ROOT.match(line):
            root = (section.multiplicity, int(m.group(1)))
            state = None if r.cut("excited_states", n) else _read_state(r, section, line, n)
        elif m := _IROOT.match(line):
            root = (section.multiplicity, int(m.group(1)))
            tokens = m.group(2).split()
            if all(_NUMBER.fullmatch(t) for t in tokens):
                continue  # property table row (e.g. state dipole moments), not a state
            state = None if r.cut("excited_states", n) else _read_iroot(r, section, m, tokens, n)
        else:
            continue
        if state is None:
            unreadable.append((root, section))
        else:
            parsed.append(_Parsed(state, section, r.ev(n)))

    # ---- termination
    if normal:
        termination = "normal"
        if errors:
            termination = "error"
            r.problem(
                "termination", "ambiguous", "normal and error termination both printed", errors[0]
            )
        elif len(normal) > 1:
            r.problem(
                "termination", "ambiguous", "normal termination printed more than once", normal[1]
            )
    elif errors:
        termination = "error"
    else:
        termination = "absent"
        r.problem("termination", "missing", "no termination line; output may be truncated")

    if version is None:
        r.problem("program_version", "missing", "no 'Program Version' line")
    if scf is None:
        scf_status, cycles = "absent", None
        r.problem("scf", "missing", "no SCF convergence line")
    else:
        scf_status, cycles = scf
    if not final_seen:
        r.problem("final_energy", "missing", "no FINAL SINGLE POINT ENERGY line")

    states, status, family = _resolve_states(r, parsed, unreadable)
    if termination != "normal" and (sections_seen or parsed or unreadable):
        r.problem(
            "excited_states", "unresolved", "no normal termination; listing may be incomplete"
        )
    if listing is not None and listing.irrep is None and not listing.bad:
        r.problem("mr_states", "error", "listing ends before its column header", listing.header)
    converged = _casscf_converged(r, casscf_ran, casscf_ok, casscf_fail, termination)
    mr_states = _resolve_mr(r, mr, mr_unreadable, converged)
    if termination != "normal" and mr_states:
        r.problem("mr_states", "unresolved", "no normal termination; listing may be incomplete")

    return OrcaObservation(
        parser=PARSER_ID,
        source=r.source,
        synthetic=synthetic,
        program_version=version,
        termination=termination,
        scf=scf_status,
        scf_cycles=cycles,
        final_energy=final,
        method_family=family,
        excited_states_status=status,
        excited_states=states,
        casscf_converged=converged,
        mr_states=mr_states,
        warnings=warnings,
        problems=r.problems,
    )


def _read_state(r: _Reader, section: _Section, line: str, n: int) -> ExcitedState | None:
    m = _STATE.match(line)
    if m is None:
        r.problem("excited_states", "error", "truncated or malformed STATE line", n)
        return None
    rest = _STATE_REST.match(m.group(8))
    if rest is None:
        r.problem("excited_states", "error", "unexpected text after state energies", n)
        return None
    energies = r.energies(list(m.group(2, 3, 4, 5, 6, 7)), n)
    if energies is None:
        return None
    if rest.group(1) is not None and not _NUMBER.fullmatch(rest.group(1)):
        r.problem("excited_states", "error", f"malformed <S**2> {rest.group(1)!r}", n)
        return None
    mult = int(rest.group(3)) if rest.group(3) else section.multiplicity
    if rest.group(3) and section.multiplicity not in (None, mult):
        r.problem("excited_states", "ambiguous", "Mult disagrees with section header", n)
        return None
    return ExcitedState(
        root=int(m.group(1)),
        multiplicity=mult,
        symmetry=rest.group(2),
        energy_au=energies[0],
        energy_ev=energies[1],
        energy_cm=energies[2],
    )


def _read_iroot(
    r: _Reader, section: _Section, m: re.Match, tokens: list[str], n: int
) -> ExcitedState | None:
    if tokens[6:8] == ["<S**2>", "="] and len(tokens) == 9:
        # Validated, not recorded, and never turned into a multiplicity (A28, A56).
        if not _NUMBER.fullmatch(tokens[8]):
            r.problem("excited_states", "error", f"malformed <S**2> {tokens[8]!r}", n)
            return None
        tokens = tokens[:6]
    energies = r.energies(tokens, n)
    if energies is None:
        return None
    return ExcitedState(
        root=int(m.group(1)),
        multiplicity=section.multiplicity,
        energy_au=energies[0],
        energy_ev=energies[1],
        energy_cm=energies[2],
    )


def _reported(sections: list[_Section]) -> list[bool]:
    """Which listings count as excitation energies (see the module docstring)."""

    def excitation(s: _Section) -> bool:
        return s.eom_type not in _NOT_EXCITATIONS

    steom = any(excitation(s) and s.method == "STEOM-CCSD" for s in sections)
    return [excitation(s) and not (steom and s.method == "CIS") for s in sections]


def _resolve_states(
    r: _Reader, parsed: list[_Parsed], unreadable: list[tuple[tuple, _Section]]
) -> tuple[list[ExcitedState], str, str | None]:
    """Pick the reported listing, dedupe identical repeats, flag conflicts.

    Unreadable lines in a discarded listing (IP/EA-EOM, the STEOM CIS guess)
    do not matter. A reported root with any unreadable line is dropped even
    if a repeat parsed.
    """
    sections = [p.section for p in parsed] + [sec for _, sec in unreadable]
    reported = _reported(sections)
    unreadable = {
        key for (key, _), ok in zip(unreadable, reported[len(parsed) :], strict=True) if ok
    }
    kept = [p for p, ok in zip(parsed, reported[: len(parsed)], strict=True) if ok]
    if parsed and all(p.section.eom_type in _NOT_EXCITATIONS for p in parsed):
        r.problem(
            "excited_states",
            "unresolved",
            "only IP/EA-EOM roots printed; these are not excitation energies",
            parsed[0].section.evidence.line if parsed[0].section.evidence else None,
        )
    methods = {p.section.method for p in kept}
    if len(methods) > 1:
        for method in sorted(methods, key=str):
            first = next(p for p in kept if p.section.method == method)
            r.problem(
                "excited_states",
                "ambiguous",
                f"state listings from several methods; cannot attribute {method!r}",
                first.evidence.line,
            )
        return [], "ambiguous", None

    by_key: dict[tuple, _Parsed] = {}
    conflicts: set[tuple] = set()
    for p in kept:
        key = (p.state.multiplicity, p.state.root)
        first = by_key.setdefault(key, p)
        if first is not p and first.values() != p.values():
            conflicts.add(key)
            r.problem(
                "excited_states",
                "ambiguous",
                f"root {p.state.root} reprinted with different values than line "
                f"{first.evidence.line}",
                p.evidence.line,
            )
    states = [p.state for k, p in by_key.items() if k not in conflicts and k not in unreadable]
    method = next(iter(methods)) if methods else None
    family = _FAMILY.get(method) if method else None
    if unreadable:
        status = "error"
    elif conflicts:
        status = "ambiguous"
    elif states:
        status = "present"
    else:
        status = "absent"
    return states, status, family


def _mr_line(
    r: _Reader,
    listing: _MrListing,
    line: str,
    n: int,
    mr: list[tuple[ExcitedState, int, _MrListing]],
    unreadable: set[tuple],
) -> bool:
    """Feed one line to an open transition-energy listing; False once it has ended.

    Units come from the column header, which must be exactly ORCA's
    (`DE/a.u. DE/eV DE/cm**-1`); the rows print bare numbers.
    """
    stripped = line.strip()
    if listing.irrep is None:  # before the column header
        if not stripped or set(stripped) == {"-"}:
            return True
        if m := _MR_LOWEST.match(stripped):
            listing.lowest = True
            if (m.group(1), m.group(2)) != ("0", "1"):
                r.problem(
                    "mr_states",
                    "unresolved",
                    f"transition energies are from ROOT {m.group(1)} MULT {m.group(2)}, "
                    "not a singlet ground state",
                    n,
                )
            return True
        tokens = stripped.split()
        if tokens[:1] != ["STATE"]:
            if not listing.bad:
                listing.bad = True
                r.problem("mr_states", "error", "listing ends before its column header", n)
            return False
        listing.irrep = tokens[3:4] == ["IRREP"]
        if tokens != _MR_COLUMNS[:3] + ["IRREP"] * listing.irrep + _MR_COLUMNS[3:]:
            listing.bad = True
            r.problem("mr_states", "error", "unexpected column header", n)
        elif not listing.lowest:
            listing.bad = True
            r.problem("mr_states", "error", "no LOWEST ROOT line before the listing", n)
        return True
    m = _MR_ROW.match(line)
    if m is None:
        return False
    if listing.bad:
        return True
    if r.cut("mr_states", n):
        listing.bad = True
        return True
    tokens = m.group(2).split()
    width = 6 if listing.irrep else 5
    if len(tokens) != width or not all(t.isdigit() for t in tokens[:2]):
        listing.bad = True
        r.problem("mr_states", "error", f"row does not have {width} columns", n)
        return True
    root, mult = int(tokens[0]), int(tokens[1])
    irrep = tokens[2] if listing.irrep else None
    energies = [
        r.quantity("mr_states", value, units, n)
        for value, (_, units) in zip(tokens[-3:], _UNITS, strict=True)
    ]
    key = (listing.method, mult, irrep, root)
    if None in energies:
        unreadable.add(key)
        return True
    au, ev, cm = energies
    state = ExcitedState(
        root=root,
        method=listing.method,
        multiplicity=mult,
        symmetry=irrep,
        energy_au=au,
        energy_ev=ev,
        energy_cm=cm,
    )
    mr.append((state, n, listing))
    return True


def _casscf_converged(
    r: _Reader, ran: bool, ok: list[int], fail: list[int], termination: str
) -> bool | None:
    """ORCA's own verdict; None when no CASSCF ran or the verdict is missing or mixed."""
    if fail and ok:
        r.problem(
            "casscf_converged", "ambiguous", "CASSCF printed both converged and failed", fail[0]
        )
        return None
    if fail:
        detail = "CASSCF did not converge"
        if termination == "normal":
            detail += " although ORCA printed normal termination"
        r.problem("casscf_converged", "error", detail, fail[0])
        return False
    if ok:
        return True
    if ran:
        r.problem("casscf_converged", "missing", "CASSCF ran but printed no convergence line")
    return None


def _resolve_mr(
    r: _Reader,
    mr: list[tuple[ExcitedState, int, _MrListing]],
    unreadable: set[tuple],
    converged: bool | None,
) -> list[ExcitedState]:
    """Keep rows of well-formed listings from a converged CASSCF; flag conflicts.

    A key is (method, multiplicity, irrep, root). Identical reprints are
    deduplicated; a conflicting reprint or an unreadable row drops the key.
    """
    kept = [(s, n) for s, n, listing in mr if not listing.bad]
    if kept and converged is not True:
        r.problem(
            "mr_states",
            "unresolved",
            "CASSCF convergence is not established; transition energies not reported",
            kept[0][1],
        )
        return []
    first: dict[tuple, tuple[ExcitedState, int]] = {}
    conflicts: set[tuple] = set()
    for s, n in kept:
        key = (s.method, s.multiplicity, s.symmetry, s.root)
        earlier, line = first.setdefault(key, (s, n))
        if earlier is not s and _printed(earlier) != _printed(s):
            conflicts.add(key)
            r.problem(
                "mr_states",
                "ambiguous",
                f"{s.method} root {s.root} reprinted with different values than line {line}",
                n,
            )
    return [s for key, (s, _) in first.items() if key not in conflicts | unreadable]


def _printed(s: ExcitedState) -> tuple:
    return tuple(q.value if q else None for q in (s.energy_au, s.energy_ev, s.energy_cm))
