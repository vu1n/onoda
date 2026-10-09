"""Agent-visible evidence from the final CASSCF orbital and CI listings.

This is a bounded, line-cited observation, never a state-character assignment
or a source of graded values. Configuration digits follow the printed active
MO range in order, including orbitals whose rounded occupation is 0 or 2.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_INPUT_ECHO = re.compile(r"^\|\s*\d+>")
_RANGE = re.compile(r"^\s*Active\s+(\d+)\s+-\s+(\d+)\s+\(\s*(\d+) orbitals\)\s*$")
_MO = re.compile(
    r"^\s*(\d+)\s+(\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)"
    r"(?:\s+(\d+-\S+))?\s*$"
)
_BLOCK = re.compile(
    r"^CAS-SCF STATES FOR BLOCK\s+(\d+) MULT=\s*(\d+)"
    r"(?: IRREP=\s*(\S+))? NROOTS=\s*(\d+)\s*$"
)
_ROOT = re.compile(
    r"^ROOT\s+(\d+):\s+E=\s+-?\d+\.\d+ Eh"
    r"(?:\s+-?\d+\.\d+ eV\s+-?\d+\.\d+ cm\*\*-1)?\s*$"
)
_CONFIG = re.compile(r"^\s*(\d+\.\d+)\s+\[\s*\d+\]:\s+([012]+)\s*$")
MAX_ROOTS = 40
MAX_ACTIVE_ORBITALS = 40
MAX_CONFIGS = 3  # first printed configurations per root; omitted counts stay visible
Line = tuple[int, str]


def casscf_state_summary(log: bytes, *, converged: bool) -> tuple[str, ...]:
    """Final CI roots with the matching MO map, or an explicit unavailable reason.

    The caller supplies the authoritative parser's convergence verdict. Only a
    single final CASSCF results section is supported: do not pair orbital and
    CI listings across calculations or silently choose among competing results.
    """
    if not converged:
        return ("Converged CAS-SCF states: unavailable (CASSCF convergence not established)",)
    lines = [
        (i, ln.rstrip())
        for i, ln in enumerate(log.decode("utf-8", errors="replace").splitlines(), 1)
        if not _INPUT_ECHO.match(ln)
    ]
    results = [i for i, (_, ln) in enumerate(lines) if ln == "CASSCF RESULTS"]
    if len(results) != 1:
        return ("Converged CAS-SCF states: unavailable (expected one final results section)",)
    start = results[0]
    blocks = [i for i in range(start, len(lines)) if _BLOCK.fullmatch(lines[i][1])]
    if not blocks:
        return ("Converged CAS-SCF states: unavailable (no final CI blocks printed)",)
    mapping, width = _active_map(lines[:start], lines[start : blocks[0]])
    out = ["Converged CAS-SCF states (final CI; occupations use the active-MO order below):"]
    out.extend(mapping)
    count = 0
    for index, begin in enumerate(blocks[:MAX_ROOTS]):
        end = blocks[index + 1] if index + 1 < len(blocks) else len(lines)
        header_line, header = lines[begin]
        match = _BLOCK.fullmatch(header)
        assert match is not None
        block, mult, irrep, nroots = match.groups()
        section = lines[begin + 1 : end]
        roots: list[tuple[Line, list[Line]]] = []
        for line in section:
            text = line[1]
            if _ROOT.fullmatch(text):
                roots.append((line, []))
            elif _CONFIG.fullmatch(text) and roots:
                roots[-1][1].append(line)
            elif text.strip() and not set(text.strip()) == {"-"}:
                # Blank separators are allowed; the next named section ends CI.
                break
        ids = [int(_ROOT.fullmatch(root[1]).group(1)) for root, _ in roots]
        complete = (
            len(ids) == int(nroots)
            and all(root == i for i, root in enumerate(ids))
            and all(configs for _, configs in roots)
        )
        out.append(
            f"- block {block}, multiplicity {mult}, {irrep or 'no irrep printed'}, "
            f"NROOTS={nroots} (line {header_line})"
        )
        if not complete:
            out.append("  CI listing incomplete or unsupported; inspect the cited output block")
            continue
        for root, configs in roots:
            count += 1
            if count > MAX_ROOTS:
                continue
            out.append(f"  {root[1].strip()} (line {root[0]})")
            if width is not None and any(
                len(_CONFIG.fullmatch(text).group(2)) != width for _, text in configs
            ):
                out.append(
                    "    configuration width differs from active-MO range; mapping unavailable"
                )
                continue
            out.extend(
                f"    {text.strip()} (line {number})" for number, text in configs[:MAX_CONFIGS]
            )
            if len(configs) > MAX_CONFIGS:
                out.append(f"    ... {len(configs) - MAX_CONFIGS} more printed configurations")
    if count > MAX_ROOTS:
        out.append(f"... {count - MAX_ROOTS} more CI roots; inspect the cited output blocks")
    if len(blocks) > MAX_ROOTS:
        out.append(f"... {len(blocks) - MAX_ROOTS} more CI blocks omitted")
    compositions = [
        number for number, text in lines[start:] if text == "LOEWDIN ORBITAL-COMPOSITIONS"
    ]
    if compositions:
        out.append(
            f"Final orbital AO compositions: line {compositions[0]} "
            "(chemical character not assigned)"
        )
    return tuple(out)


def _active_map(before: Sequence[Line], final: Sequence[Line]) -> tuple[list[str], int | None]:
    ranges = [(number, m) for number, text in before if (m := _RANGE.fullmatch(text))]
    unavailable = [
        "Active-MO mapping unavailable: missing, ambiguous or incomplete printed range/table"
    ]
    if len(ranges) != 1:
        return unavailable, None
    range_line, match = ranges[0]
    first, last, size = map(int, match.groups())
    if size != last - first + 1 or size < 1:
        return unavailable, None
    if size > MAX_ACTIVE_ORBITALS:
        return [
            f"Active-MO mapping omitted: {size} orbitals exceeds summary limit (line {range_line})"
        ], size
    tables = [i for i, (_, text) in enumerate(final) if text == "ORBITAL ENERGIES"]
    if len(tables) != 1:
        return unavailable, size
    orbitals = []
    in_table = False
    for number, text in final[tables[0] + 1 :]:
        if m := _MO.fullmatch(text):
            in_table = True
            if first <= int(m.group(1)) <= last:
                orbitals.append((number, m))
        elif in_table:
            break
    if [int(m.group(1)) for _, m in orbitals] != list(range(first, last + 1)):
        return unavailable, size
    out = [
        f"Active-MO order: printed range {first}–{last} (line {range_line}); "
        "configuration digit 1 corresponds to the first MO, not energy-sorted order."
    ]
    for position, (number, m) in enumerate(orbitals, 1):
        mo, occ, eh, _, label = m.groups()
        out.append(
            f"- digit {position}: MO {mo}, {label or 'no irrep printed'}, "
            f"occupation {occ}, E={eh} Eh (line {number})"
        )
    return out, size
