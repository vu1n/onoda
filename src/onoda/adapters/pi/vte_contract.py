"""Pi contract for VTE tasks: the agent runs its own ORCA jobs (A57-A62).

The M3 task (`contract.TaskBundle`) hands the agent one pinned output. A VTE
task instead hands it a molecule (`science.vte`), and the agent writes ORCA
inputs and submits them with `orca_submit`. Nothing in the Pi process can run
ORCA (the sandbox does not mount it), so submissions go over a Unix socket to
a host-side broker (`onoda.science.orca_broker`) that owns the ORCA scheduler,
its cache and the attempt cap. This module fixes what crosses each boundary:

- the staged bundle (`VteTaskBundle`, `task.json` schema
  `onoda.pi-vte-task/1`) and the tools a VTE run gets (`VTE_TASK_TOOLS`);
- the agent's answer (`VteClaim`): every state carries its identity (spin,
  irrep, character) and the job it was read from, so the grader can check the
  value against that job's output and match the identity to a reference
  without ever pairing by energy order (A58);
- the broker wire protocol (`BrokerSubmit` -> `BrokerReply`, JSON Lines,
  one request per connection), mirrored by `pi/extensions/lib/spool.ts`.

The spool line the extension writes per submission is `contract.TaskExecute`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

# The tool names live in `contract` (the variant table needs them) and are re-exported here.
from onoda.adapters.pi.contract import (  # noqa: F401
    TOOL_ORCA_SUBMIT,
    TOOL_REPORT_VTE,
    TOOL_STRUCTURE,
    VTE_TASK_TOOLS,
    ClaimedQuantity,
    DocNames,
    Instruction,
)
from onoda.records import Frozen as _Frozen
from onoda.science.orca_local import FILE_NAME_RE
from onoda.science.vte import Spin, VteTarget

VTE_TASK_SCHEMA = "onoda.pi-vte-task/1"
VTE_CLAIM_SCHEMA = "onoda.pi-vte-claim/1"
BROKER_PROTOCOL = "onoda.orca-broker/1"

ENV_BROKER = "ONODA_PI_BROKER"  # path of the broker's Unix socket

# Submissions per run, cache hits included (A59). The paper caps attempts at 8
# per target specification; a small molecule here has about three.
DEFAULT_MAX_EXECUTIONS = 24

# Broker job ids are per run (`j0001`, ...), never the scheduler's own ids.
JOB_ID_RE = re.compile(r"^j\d{4}$")
# `FILE_NAME_RE` (the scheduler's rule): a file an earlier job produced, named as
# the job directory holds it (e.g. `job.gbw`).
MAX_INPUT_CHARS = 65536


class VteTaskBundle(_Frozen):
    """`task.json` of a staged VTE bundle.

    Layout: `task.json`, `structure/<structure_name>`, optional `docs/*.md`
    and `ladder.md`. Never any reference value: the grader reads those from
    the case's `references.json`, which is not staged (A58).
    """

    schema_: Literal["onoda.pi-vte-task/1"] = Field(VTE_TASK_SCHEMA, alias="schema")
    case_id: str
    instruction: Instruction
    molecule: str
    point_group: str
    structure_name: str
    structure_sha256: str
    targets: tuple[VteTarget, ...]
    review_status: str
    docs: DocNames = ()
    ladder_sha256: str | None = None
    synthetic_ladder: bool = False
    max_executions: int = Field(DEFAULT_MAX_EXECUTIONS, gt=0)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @field_validator("structure_name")
    @classmethod
    def _structure_name(cls, v: str) -> str:
        if not FILE_NAME_RE.match(v):
            raise ValueError(f"structure name {v!r} is not a plain file name")
        return v


class ClaimedVte(_Frozen):
    """One reported state: who it is, where it was read, and its energy as printed."""

    spin: Spin
    irrep: str  # as the agent names it; normalized by the grader
    character: str = Field(default="", max_length=64)  # e.g. "n,pi*", "pi,3s"
    energy: ClaimedQuantity  # excitation energy, unit "eV"
    job_id: str

    @field_validator("job_id")
    @classmethod
    def _job(cls, v: str) -> str:
        if not JOB_ID_RE.match(v):
            raise ValueError(f"job id {v!r} is not a broker job id (j0001, ...)")
        return v

    @field_validator("energy")
    @classmethod
    def _ev(cls, v: ClaimedQuantity) -> ClaimedQuantity:
        if v.unit != "eV":
            raise ValueError(f"excitation energies are reported in eV, not {v.unit!r}")
        return v


class VteClaim(_Frozen):
    """The `report_vtes` tool input, spooled verbatim as `task.claim`."""

    schema_: Literal["onoda.pi-vte-claim/1"] = Field(VTE_CLAIM_SCHEMA, alias="schema")
    case_id: str
    states: tuple[ClaimedVte, ...] = ()
    notes: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class BrokerAttach(_Frozen):
    """A file an earlier job of this run produced, copied next to the new input."""

    job_id: str
    name: str

    @field_validator("job_id")
    @classmethod
    def _job(cls, v: str) -> str:
        if not JOB_ID_RE.match(v):
            raise ValueError(f"job id {v!r} is not a broker job id")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not FILE_NAME_RE.match(v):
            raise ValueError(f"file name {v!r} is not a plain file name")
        return v


class BrokerSubmit(_Frozen):
    """Extension -> broker. The input is the `.inp` text the agent wrote, unchanged."""

    protocol: Literal["onoda.orca-broker/1"] = BROKER_PROTOCOL
    op: Literal["submit"] = "submit"
    tool_call_id: str
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    attach: tuple[BrokerAttach, ...] = ()


class BrokerReply(_Frozen):
    """Broker -> extension, written to the broker journal before it is sent.

    `refused` (cap reached, unknown attachment, replay miss) runs nothing and
    carries no job. A cache hit returns an earlier job's output under a new
    job id with `cache_hit` true and `executed_here` false (A59).
    `output_b64` carries the exact log bytes; the extension spools those
    bytes and shows the agent their UTF-8 text.
    """

    protocol: Literal["onoda.orca-broker/1"] = BROKER_PROTOCOL
    status: Literal["completed", "failed", "refused"]
    job_id: str | None = None
    cache_hit: bool = False
    executed_here: bool = False
    input_sha256: str | None = None
    output_sha256: str | None = None
    output_b64: str = ""
    files: tuple[str, ...] = ()  # names this job produced that a later submit may attach
    reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> BrokerReply:
        if self.status == "refused":
            if self.job_id is not None or self.executed_here or self.cache_hit:
                raise ValueError("a refused submission has no job and ran nothing")
        else:
            if self.job_id is None or not JOB_ID_RE.match(self.job_id):
                raise ValueError("a completed or failed submission names its job id")
            if self.output_sha256 is None or self.input_sha256 is None:
                raise ValueError("a completed or failed submission pins input and output")
        if self.cache_hit and self.executed_here:
            raise ValueError("a cache hit is never an execution")
        return self
