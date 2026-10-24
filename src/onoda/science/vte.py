"""Vertical-transition-energy (VTE) cases: an agent computes states, a reference grades them.

A VTE case is the paper's task in miniature (Lee & Rondinelli, arXiv
2609.13357): the agent gets a molecule's geometry and the states to compute,
writes and runs its own ORCA jobs, and reports excitation energies. Unlike an
M2 case (`contract.CaseManifest`), nothing here pins a solver output; the
outputs come from the run itself.

Two files per case, deliberately separate:

- `<case>.vte.yaml` (`VteCaseManifest`): the structure file, the point group
  and the target states (spin, irrep, count) the agent is told about, plus
  provenance. It is agent-visible and is committed to the agent store.
- `references.json` (`VteReferences`): the reference excitation energies.
  Evaluator-only. The manifest pins its sha256 so tampering is caught, but
  only `load_vte_references` reads its content, and no agent-side code path
  calls it (A58).

Reference rows keep QUESTDB's own state label; `parse_state_label` splits it
into spin, irrep and character with the glyph clean-up in
`normalize_character`/`normalize_irrep`, so grading compares identities, never
energy order.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from onoda.errors import IntegrityError
from onoda.records import Frozen as _Frozen
from onoda.science.contract import PinnedFile, Provenance, Review, _read_pinned, case_manifests

VTE_MANIFEST_SUFFIX = ".vte.yaml"
REFERENCES_SCHEMA = "onoda.vte-references/1"

Spin = Literal[1, 3]

# QUESTDB's tables were converted from Word documents set in the Symbol font,
# so Greek letters arrive as private-use code points.
_SYMBOL_FONT = {"": "pi", "": "n", "": "sigma", "": "delta"}
# How agents write a character: Unicode Greek (matched after lowercasing) and
# arrow separators. None of these occur in QUESTDB's labels.
_GREEK = {"π": "pi", "σ": "sigma", "ς": "sigma", "δ": "delta", "∗": "*"}
_SEPARATORS = {"→": ",", "⟶": ",", "->": ","}
_TO_RE = re.compile(r"(?<=\S)\s+to\s+(?=\S)", re.IGNORECASE)
_PRIMES = {"’": "'", "‘": "'", "”": "''", "“": "''", '"': "''"}
_DECIMAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)$")
_IRREP_RE = re.compile(r"^[A-Z][a-z0-9]*'{0,2}$")
# A Rydberg p orbital's axis (`3pz`) repeats what the irrep already says.
_P_AXIS_RE = re.compile(r"(?<=\dp)[xyz]")
_LABEL_RE = re.compile(r"^(?P<spin>[13])(?P<irrep>[A-Z][a-z0-9]*'{0,2})\((?P<char>[^()]+)\)$")


def normalize_irrep(text: str) -> str:
    """`A’’`, `A”`, `A"` -> `A''`; `A’` -> `A'`; anything else unchanged but stripped."""
    out = text.strip()
    for k, v in _PRIMES.items():
        out = out.replace(k, v)
    return out


def normalize_character(text: str) -> str:
    """A character label in ascii: Greek glyphs to names, separators to `,`, no spaces, lowercase.

    QUESTDB's `\\uf070,\\uf070*` -> `pi,pi*`; `n,3s` stays `n,3s`. What agents
    write is read the same way: Unicode Greek (`π`, `σ`, `δ`, either case) and
    `∗` become `pi`, `sigma`, `delta` and `*`, and an arrow (`→`, `->`) or a
    spaced `to` between the two orbitals becomes `,` (`n → π*` -> `n,pi*`). A
    bare `p` is left as is: in a few tables it may be a lost pi, but guessing
    would invent an identity.
    """
    out = text
    for k, v in _SYMBOL_FONT.items():
        out = out.replace(k, v)
    out = _TO_RE.sub(",", out)
    for k, v in _SEPARATORS.items():
        out = out.replace(k, v)
    out = re.sub(r"\s+", "", out).lower()
    for k, v in _GREEK.items():
        out = out.replace(k, v)
    return out


def identity_character(text: str) -> str:
    """The character as the grader compares it: `normalize_character`, Rydberg p axis dropped.

    `n,3pz` and `n,3p` compare equal: within one point-group irrep a p
    orbital's axis is fixed, so the suffix adds nothing the irrep does not.
    Reference keys and reports keep the full `normalize_character` text.
    """
    return _P_AXIS_RE.sub("", normalize_character(text))


def parse_state_label(label: str) -> tuple[int, str, str] | None:
    """(spin, irrep, character) of a QUESTDB label like `1B1(\\uf070,3s)`, or None.

    None for anything else, including fluorescence rows (`1A"[F](...)`), which
    need an excited-state geometry and are not part of a VTE case.
    """
    m = _LABEL_RE.match(normalize_irrep(label).replace(" ", ""))
    if m is None:
        return None
    return int(m["spin"]), m["irrep"], normalize_character(m["char"])


# Context: doc://onoda/vte-task-inputs@0001#agent-gets-structure-and-targets — the agent gets target counts per spin and irrep, never energies or characters
class VteTarget(_Frozen):
    """`count` states of this spin and irrep; what the agent is asked to compute."""

    spin: Spin
    irrep: str
    count: int = Field(ge=1)

    @field_validator("irrep")
    @classmethod
    def _irrep(cls, v: str) -> str:
        if not _IRREP_RE.match(v):
            raise ValueError(f"irrep {v!r} is not a normalized irrep label (see normalize_irrep)")
        return v


class VteCaseManifest(_Frozen):
    case_id: str
    title: str
    molecule: str
    point_group: str
    method: str  # the reference protocol, e.g. "SA-CASSCF/SC-NEVPT2/aug-cc-pVTZ"
    split: Literal["development", "validation", "test"]
    structure: PinnedFile  # xyz, angstrom
    targets: tuple[VteTarget, ...]
    references: PinnedFile  # evaluator-only content; pinned here, read only by the grader
    provenance: Provenance
    review: Review
    labels: list[str] = Field(default_factory=list)


class LoadedVteCase(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    manifest: VteCaseManifest
    root: Path
    structure_bytes: bytes


class VteReferenceRow(_Frozen):
    label: str  # QUESTDB's label as printed in its table
    spin: Spin
    irrep: str
    character: str  # normalize_character(...) of the label's parenthesis
    energy_ev: str  # decimal text, the reference method's excitation energy
    active_space: str = ""  # QUESTDB's per-irrep orbital counts, verbatim
    state_average: str = ""  # QUESTDB's per-irrep root counts, verbatim
    # Other characters that name this same state (`identity_character` text),
    # each with the reason in `notes`; e.g. sulfur's first Rydberg shell is
    # 4s/4p where QUESTDB writes 3s/3p.
    aliases: tuple[str, ...] = ()
    notes: str = ""  # why `character` or `aliases` differ from the label, if they do

    @field_validator("energy_ev")
    @classmethod
    def _decimal(cls, v: str) -> str:
        if not _DECIMAL_RE.match(v):
            raise ValueError(f"not decimal text: {v!r}")
        return v


class VteReferences(_Frozen):
    schema_: Literal["onoda.vte-references/1"] = Field(REFERENCES_SCHEMA, alias="schema")
    case_id: str
    method: str  # e.g. "SC-NEVPT2/aug-cc-pVTZ"
    source: dict[str, str]  # repository, commit, path, license
    rows: tuple[VteReferenceRow, ...]

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


def load_vte_case(manifest_path: str | Path) -> LoadedVteCase:
    """Parse a VTE manifest and verify the structure and the references file pins.

    The references file is hashed, never returned: an agent-side caller gets
    nothing of its content.
    """
    manifest_path = Path(manifest_path)
    manifest = VteCaseManifest.model_validate(yaml.safe_load(manifest_path.read_text()))
    root = manifest_path.parent
    structure = _read_pinned(root, manifest.structure)
    _read_pinned(root, manifest.references)
    return LoadedVteCase(manifest=manifest, root=root, structure_bytes=structure)


def load_vte_references(case: LoadedVteCase) -> VteReferences:
    """The reference rows for `case` (evaluator side only), verified against the pin."""
    data = _read_pinned(case.root, case.manifest.references)
    refs = VteReferences.model_validate_json(data)
    if refs.case_id != case.manifest.case_id:
        raise IntegrityError(
            f"references are for {refs.case_id!r}, the case is {case.manifest.case_id!r}"
        )
    return refs


def vte_case_manifests(root: Path) -> dict[str, Path]:
    """case_id -> manifest path for every `*.vte.yaml` under `root`."""
    return case_manifests(root, suffix=VTE_MANIFEST_SUFFIX)
