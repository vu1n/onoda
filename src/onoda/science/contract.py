"""M2 shared contract: parsed-observation schema and fixed-input case manifests.

The deterministic parser produces an `OrcaObservation`; the evaluator consumes
one. Nothing else crosses that boundary, so parser and evaluator never share
code paths beyond these models (ACCEPTANCE_TESTS T17/T18, BUILD_SPEC §5).

Honesty rules the models encode:

- Every number is the exact decimal text the program printed, with its units
  and the source line that printed it (`Quantity.evidence`). No floats, no
  unit conversion, no rounding.
- Absence is explicit. A field that could not be read is `None` *and* has a
  `Problem` naming why (`missing`, `error`, `ambiguous`, `unresolved`), so a
  consumer can never mistake "not found" for a default value.
- `synthetic` marks observations parsed from mutated or stand-in bytes; an
  observation of an authentic output has `synthetic=False`.

Case manifests (`cases/**/*.case.yaml`) pin the input and output bytes of an
authentic, externally produced calculation by sha256, record where the bytes
came from and under which license, and say whether a human has reviewed the
case. `load_case` refuses a manifest whose bytes do not match.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from onoda.canonical import sha256_digest
from onoda.errors import IntegrityError, OnodaError
from onoda.records import Frozen as _Frozen

Digest = str

OBSERVATION_SCHEMA = "onoda.orca-observation/1"

ProblemState = Literal["missing", "error", "ambiguous", "unresolved"]
Termination = Literal["normal", "error", "absent"]
ScfStatus = Literal["converged", "not_converged", "absent"]
StatesStatus = Literal["present", "absent", "ambiguous", "error"]


class Evidence(_Frozen):
    """The exact source line a value was read from (1-based line number)."""

    artifact: Digest
    line: int
    text: str


class Quantity(_Frozen):
    """An exact decimal string as printed, its units, and where it was printed."""

    value: str
    units: str
    evidence: Evidence


class Problem(_Frozen):
    """Why a field is absent or untrustworthy. `field` names an observation field."""

    field: str
    state: ProblemState
    detail: str
    evidence: Evidence | None = None


class ExcitedState(_Frozen):
    """One excited state as the program labelled it.

    `root` is the program's own root number. `multiplicity` and `symmetry`
    are only set when the output states them; they are never inferred.
    """

    root: int
    multiplicity: int | None = None
    symmetry: str | None = None
    energy_au: Quantity
    energy_ev: Quantity | None = None
    energy_cm: Quantity | None = None


class OrcaObservation(_Frozen):
    schema_: str = Field(default=OBSERVATION_SCHEMA, alias="schema")
    parser: str
    source: Digest
    synthetic: bool = False
    program_version: str | None = None
    termination: Termination
    scf: ScfStatus
    scf_cycles: int | None = None
    final_energy: Quantity | None = None
    method_family: str | None = None
    excited_states_status: StatesStatus
    excited_states: list[ExcitedState] = Field(default_factory=list)
    warnings: list[Evidence] = Field(default_factory=list)
    problems: list[Problem] = Field(default_factory=list)

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


# ------------------------------------------------------------------ cases

ReviewStatus = Literal["pending-human-review", "reviewed", "rejected"]


class PinnedFile(_Frozen):
    path: str  # relative to the manifest's directory; may not escape it
    sha256: Digest


class Provenance(_Frozen):
    origin: str
    repository: str
    commit: str
    upstream_path: str
    license: str
    license_file: str
    retrieved: str
    executed_here: bool
    notes: str = ""


class Review(_Frozen):
    status: ReviewStatus
    reviewer: str | None = None
    date: str | None = None
    notes: str = ""


class DeclaredRequest(_Frozen):
    """What the input file asks for, transcribed from the input, per multiplicity."""

    roots_by_multiplicity: dict[int, int] = Field(default_factory=dict)
    expects_final_energy: bool = True
    source_lines: list[str] = Field(default_factory=list)


class CaseManifest(_Frozen):
    case_id: str
    title: str
    program: str
    program_version: str
    method: str
    split: Literal["development", "validation", "test"]
    input: PinnedFile
    outputs: dict[str, PinnedFile]
    provenance: Provenance
    request: DeclaredRequest
    review: Review
    labels: list[str] = Field(default_factory=list)


class LoadedCase(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    manifest: CaseManifest
    root: Path
    input_bytes: bytes
    output_bytes: dict[str, bytes]


def _read_pinned(root: Path, pinned: PinnedFile) -> bytes:
    path = (root / pinned.path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise OnodaError(f"case file {pinned.path!r} escapes its case directory")
    data = path.read_bytes()
    if sha256_digest(data) != pinned.sha256:
        raise IntegrityError(f"{pinned.path}: bytes do not match pinned {pinned.sha256}")
    return data


def load_case(manifest_path: str | Path) -> LoadedCase:
    """Parse a case manifest and verify every pinned file against its sha256."""
    manifest_path = Path(manifest_path)
    manifest = CaseManifest.model_validate(yaml.safe_load(manifest_path.read_text()))
    root = manifest_path.parent
    return LoadedCase(
        manifest=manifest,
        root=root,
        input_bytes=_read_pinned(root, manifest.input),
        output_bytes={k: _read_pinned(root, v) for k, v in manifest.outputs.items()},
    )


CASES_ROOT = Path(__file__).resolve().parents[3] / "cases"


def case_manifests(root: Path = CASES_ROOT) -> dict[str, Path]:
    """case_id -> manifest path for every `*.case.yaml` under `root`."""
    found: dict[str, Path] = {}
    for path in sorted(root.glob("**/*.case.yaml")):
        case_id = yaml.safe_load(path.read_text())["case_id"]
        if case_id in found:
            raise OnodaError(f"duplicate case id {case_id!r}: {found[case_id]} and {path}")
        found[case_id] = path
    return found
