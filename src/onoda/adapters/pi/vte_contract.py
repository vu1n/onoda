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
  one request per connection), mirrored by `pi/extensions/lib/spool.ts`. It
  lives in `onoda.science.orca_broker`, which owns the broker, and is
  re-exported here so `onoda.science` never imports an adapter.

The spool line the extension writes per submission is `contract.TaskExecute`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator

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

# The broker protocol lives with the broker (`science` never imports `adapters`).
from onoda.science.orca_broker import (  # noqa: F401
    BROKER_PROTOCOL,
    DEFAULT_MAX_EXECUTIONS,
    JOB_ID_RE,
    MAX_INPUT_CHARS,
    BrokerAttach,
    BrokerReply,
    BrokerSubmit,
)
from onoda.science.orca_local import FILE_NAME_RE
from onoda.science.vte import Spin, VteTarget

VTE_TASK_SCHEMA = "onoda.pi-vte-task/1"
VTE_CLAIM_SCHEMA = "onoda.pi-vte-claim/1"

ENV_BROKER = "ONODA_PI_BROKER"  # path of the broker's Unix socket


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
