"""Versioned, immutable record schemas (PORTABLE_RUN_SPEC §2).

Every record carries an explicit `schema` string ("onoda.<kind>/<n>"). Records
are frozen pydantic models with no float fields; `to_json()` yields the plain
dict that is canonicalized and hashed. A record's own digest is never part of
its hash input: content-addressed kinds (steps, checkpoints, fork declarations)
are stored under `digest_of(record.to_json())`.

Readers reject unknown schema versions. Migrations create new records from old
dicts and never touch the source bytes (see `MIGRATIONS`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from onoda.canonical import digest_of
from onoda.errors import SchemaVersionError

Digest = str  # "sha256:<64 hex>"
Visibility = Literal["agent", "evaluator"]
Boundary = Literal[
    "initialized",
    "job-collected",
    "diagnostics-committed",
    "decision-committed-before-submission",
    "finalized",
]
BOUNDARIES: tuple[Boundary, ...] = (
    "initialized",
    "job-collected",
    "diagnostics-committed",
    "decision-committed-before-submission",
    "finalized",
)


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ClassVar[str]
    version: ClassVar[int] = 1
    content_addressed: ClassVar[bool] = False
    id_field: ClassVar[str | None] = None

    schema_: str = Field(alias="schema")

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def record_digest(self) -> Digest:
        return digest_of(self.to_json())

    def record_id(self) -> str:
        if self.content_addressed:
            return self.record_digest()
        assert self.id_field is not None
        return getattr(self, self.id_field)

    @classmethod
    def schema_id(cls) -> str:
        return f"onoda.{cls.kind}/{cls.version}"

    @classmethod
    def new(cls, **fields: Any):
        return cls(schema=cls.schema_id(), **fields)


class ArtifactDescriptor(Record):
    """Metadata for immutable raw bytes; identity is the byte hash."""

    kind = "artifact"
    id_field = "digest"

    digest: Digest
    size: int
    media_type: str
    encoding: str | None = None
    units: str | None = None
    visibility: Visibility = "agent"
    source: dict[str, Any] = Field(default_factory=dict)
    license: str | None = None
    labels: list[str] = Field(default_factory=list)


class TaskManifest(Record):
    kind = "task"
    id_field = "task_id"

    task_id: str
    inputs: dict[str, Digest]
    permitted_context: list[str] = Field(default_factory=list)
    split: str
    group_ids: list[str] = Field(default_factory=list)
    target: dict[str, Any] = Field(default_factory=dict)
    profile_versions: dict[str, str] = Field(default_factory=dict)
    synthetic: bool


Determinism = Literal["pure", "nondeterministic", "external"]


class StepSpec(Record):
    """What was asked for. Its digest is the specification identity, not an execution ID."""

    kind = "step"
    content_addressed = True

    operation: str
    inputs: dict[str, Digest]
    config: dict[str, Any] = Field(default_factory=dict)
    code_ref: str
    environment: dict[str, str] = Field(default_factory=dict)
    determinism: Determinism


class Usage(BaseModel):
    """Resource units. `cost_usd` is a decimal string; None means unknown, never zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: dict[str, int] = Field(default_factory=dict)
    measured: bool = False
    simulated: bool = True
    cost_usd: str | None = None
    price_table: str | None = None


InvocationKind = Literal["execution", "decision", "derivation", "reuse", "read"]
InvocationStatus = Literal["succeeded", "failed", "submission_uncertain", "cancelled"]


class InvocationReceipt(Record):
    """One actual execution. Identical specs yield distinct receipts."""

    kind = "invocation"
    id_field = "invocation_id"

    invocation_id: str
    run_id: str
    spec_digest: Digest
    invocation_kind: InvocationKind
    inputs: dict[str, Digest]
    outputs: dict[str, Digest] = Field(default_factory=dict)
    parents: list[str] = Field(default_factory=list)
    status: InvocationStatus
    attempt: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    started_at: str
    finished_at: str | None = None
    derived_from: str | None = None
    labels: list[str] = Field(default_factory=list)


class Event(Record):
    """Append-only journal entry. `prev_digest` chains to the previous event."""

    kind = "event"
    id_field = "event_id"

    event_id: str
    run_id: str
    seq: int
    actor: str
    type: str
    causes: list[str] = Field(default_factory=list)
    payload_refs: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_digest: Digest | None
    ts: str


class Checkpoint(Record):
    """A consistent committed frontier plus materializable semantic state.

    `visible` maps logical state names to artifact digests the run may see.
    `ancestry_cut` maps each ancestor run id to the last event seq visible
    (the visibility cut used to exclude an ancestor's future).
    `capabilities` declares what restoration is supported, e.g.
    {"semantic_restart": "supported", "native_resume": "unsupported"}.
    """

    kind = "checkpoint"
    content_addressed = True

    run_id: str
    boundary: Boundary
    event_seq: int
    frontier: list[str]
    visible: dict[str, Digest]
    phase: str
    policy: dict[str, Any] = Field(default_factory=dict)
    memory_release: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    quiescent: bool
    ancestry_cut: dict[str, int] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(default_factory=dict)


class ParentRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    checkpoint: Digest


class Run(Record):
    kind = "run"
    id_field = "run_id"

    run_id: str
    experiment_id: str
    task: str  # task_id
    config: dict[str, Any] = Field(default_factory=dict)
    parent: ParentRef | None = None
    fork: Digest | None = None  # ForkDeclaration digest when forked
    quota_id: str
    created_at: str
    labels: list[str] = Field(default_factory=list)


class ForkDeclaration(Record):
    kind = "fork"
    content_addressed = True

    new_run_id: str
    parent: ParentRef
    intervention: dict[str, Any]
    invalidated: list[str] = Field(default_factory=list)
    visibility_cut: dict[str, int] = Field(default_factory=dict)
    budget_treatment: Literal["inherit-remaining", "fresh-allowance"] = "inherit-remaining"
    context_translation: str | None = None
    reason: str


class Evaluation(Record):
    """A separate derivation over immutable outputs; never overwrites history."""

    kind = "evaluation"
    id_field = "evaluation_id"

    evaluation_id: str
    run_id: str
    evaluator: str
    evaluator_version: str
    output_refs: dict[str, Digest]
    reference_version: str
    assignments: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)
    metrics: dict[str, str] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    created_at: str


RECORD_TYPES: dict[str, type[Record]] = {
    cls.kind: cls
    for cls in (
        ArtifactDescriptor,
        TaskManifest,
        StepSpec,
        InvocationReceipt,
        Event,
        Checkpoint,
        Run,
        ForkDeclaration,
        Evaluation,
    )
}

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _artifact_v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """v0 descriptors named the digest `sha256` and stored bare hex."""
    out = {k: v for k, v in data.items() if k != "sha256"}
    out["schema"] = "onoda.artifact/1"
    out["digest"] = "sha256:" + data["sha256"]
    return out


# (source schema id) -> migration producing a dict of the next version.
MIGRATIONS: dict[str, Migration] = {"onoda.artifact/0": _artifact_v0_to_v1}


def load_record(kind: str, data: dict[str, Any], *, migrate: bool = False) -> Record:
    """Parse a record dict, rejecting unknown schema versions.

    With `migrate=True`, known older versions are migrated in memory; the
    caller stores the result as a *new* record and keeps the source digest.
    """
    cls = RECORD_TYPES.get(kind)
    if cls is None:
        raise SchemaVersionError(f"unknown record kind {kind!r}")
    schema = data.get("schema")
    if migrate:
        while schema != cls.schema_id() and schema in MIGRATIONS:
            data = MIGRATIONS[schema](data)
            schema = data.get("schema")
    if schema != cls.schema_id():
        raise SchemaVersionError(
            f"{kind} record has schema {schema!r}; this reader supports {cls.schema_id()!r}"
        )
    return cls.model_validate(data)
