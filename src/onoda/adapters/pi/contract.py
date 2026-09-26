"""Pi adapter contract: the only surface the M3 lanes share (SOL_PI_DELTA §2-§5).

A Pi run is a real `pi` process (pinned `@earendil-works/pi-coding-agent`
0.85.1, optionally with SoL-Pi at a pinned commit) with onoda's own
extensions loaded around it. Those extensions write an append-only *spool*
while the process runs; after it exits, `onoda.adapters.pi.ingest` turns the
spool into ordinary store records. The spool is the write-ahead log: every
byte the model is shown is written (and fsynced) to the spool before Pi
hands it to the model, so ingesting it later preserves commit-before-visible
ordering in the portable record.

This module fixes:

- the runtime pins and extension load order (recorder last, so it sees the
  context and tool results after every other extension has transformed them);
- the environment variables the TypeScript extensions read;
- the spool layout and its line types (`SpoolLine`), mirrored field-for-field
  by `pi/extensions/lib/spool.ts`;
- the scripted-provider script (`Script`) used for offline runs and replay;
- the staged task bundle (`TaskBundle`) and the agent's claim (`Claim`);
- the variant table S0-S3 (`VARIANTS`) and the SoL-Pi config each one maps to;
- the gateway policy (`GatewayPolicy`) the recorder enforces at `tool_call`.

Nothing here launches a process or writes to a store.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ------------------------------------------------------------------ runtime pins

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "0.85.1"  # provenance/sources.json S14
SOL_PI_PACKAGE = "sol-pi"
SOL_PI_COMMIT = "1559b5cb12c72da4a485bc50fe326586b216fb19"  # provenance/sources.json S13

REPO_ROOT = Path(__file__).resolve().parents[4]
# package.json + package-lock.json; node_modules comes from scripts/pi-setup.sh
RUNTIME_DIR = REPO_ROOT / "pi"
EXTENSIONS_DIR = RUNTIME_DIR / "extensions"
SOL_PI_ENTRY = Path("node_modules/sol-pi/src/sol-pi/index.ts")  # relative to RUNTIME_DIR

# Load order is part of the contract. The task extension loads first so its
# raw tool-result tap sees results before SoL-Pi's reducer rewrites them; the
# recorder loads last so it records exactly what the model is shown.
TASK_EXTENSION = "onoda-task.ts"
SCRIPTED_EXTENSION = "onoda-scripted.ts"
RECORDER_EXTENSION = "onoda-recorder.ts"

# ------------------------------------------------------------ environment

ENV_SPOOL = "ONODA_PI_SPOOL"  # spool directory (created by the runtime, empty)
ENV_SCRIPT = "ONODA_PI_SCRIPT"  # Script JSON path; scripted provider only
ENV_TASK = "ONODA_PI_TASK"  # staged TaskBundle directory
ENV_POLICY = "ONODA_PI_POLICY"  # GatewayPolicy JSON path
ENV_RUN_INFO = "ONODA_PI_RUN_INFO"  # SpoolStart fields the recorder copies verbatim

SCRIPTED_PROVIDER = "onoda-scripted"
SCRIPTED_MODEL = "scripted"
SCRIPTED_REDUCER_MODEL = "scripted-reducer"

# ------------------------------------------------------------------- tools

TOOL_CASE_INPUT = "case_input"  # read the staged ORCA input (read-only)
TOOL_ORCA_RUN = "orca_run"  # acquire the pinned output; recorded as acquired, never executed
TOOL_REPORT = "report_states"  # the agent's final claim
TASK_TOOLS = (TOOL_CASE_INPUT, TOOL_ORCA_RUN, TOOL_REPORT)
BASE_TOOLS = ("read", "bash", "edit", "write")
MECHANISM_TOOLS = {"observationPack": ("obs_recall",), "onlineContextCompact": ("update_plan",)}

# then_run commands that could reach a solver or the store are never fusable
# (SOL_PI_DELTA §2: submit/cancel and anything with budget impact).
UNFUSABLE_COMMAND_TOKENS = ("orca", "onoda", "sbatch", "qsub", "srun", "bsub")

# ------------------------------------------------------------------ variants

Mechanism = Literal[
    "observationPack", "evidencePreservingReducer", "onlineContextCompact", "actionFusion"
]
MECHANISMS: tuple[Mechanism, ...] = (
    "observationPack",
    "evidencePreservingReducer",
    "onlineContextCompact",
    "actionFusion",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Variant(_Frozen):
    """One EXPERIMENT_PLAN arm (SOL_PI_DELTA §5). Mechanisms are SoL-Pi feature flags."""

    id: str
    question: str
    ladder: bool  # requires an operator-supplied advisory ladder document
    rlm: bool  # requires the RLM extension (not built in M3; refused)
    mechanisms: tuple[Mechanism, ...] = ()

    def sol_pi_config(
        self, *, reducer_provider: str | None = None, reducer_model: str | None = None
    ) -> dict[str, Any] | None:
        """The `sol-pi.json` for this variant, or None when SoL-Pi is not loaded."""
        if not self.mechanisms:
            return None
        cfg: dict[str, Any] = {"version": 1, **{m: m in self.mechanisms for m in MECHANISMS}}
        cfg["cacheWriteReadRatio"] = 12.5  # SoL-Pi's default, recorded not tuned (A14)
        if "evidencePreservingReducer" in self.mechanisms:
            if not (reducer_provider and reducer_model):
                raise ValueError(f"variant {self.id} needs a reducer provider and model")
            cfg["evidencePreservingReducerProvider"] = reducer_provider
            cfg["evidencePreservingReducerModel"] = reducer_model
        return cfg

    def tools(self) -> tuple[str, ...]:
        extra = tuple(t for m in self.mechanisms for t in MECHANISM_TOOLS.get(m, ()))
        return (*TASK_TOOLS, *BASE_TOOLS, *extra)


# S2 letters follow SOL_PI_DELTA §2's mechanism order (assumptions register A31).
VARIANTS: dict[str, Variant] = {
    v.id: v
    for v in (
        Variant(id="S0", question="Pi baseline, allowed documentation", ladder=False, rlm=False),
        Variant(id="S1", question="advisory ladder on the base harness", ladder=True, rlm=False),
        Variant(id="S1r", question="S1 + RLM extension", ladder=True, rlm=True),
        Variant(
            id="S2a",
            question="S1 + ObservationPack",
            ladder=True,
            rlm=False,
            mechanisms=("observationPack",),
        ),
        Variant(
            id="S2b",
            question="S1 + Evidence-Preserving Reducer",
            ladder=True,
            rlm=False,
            mechanisms=("evidencePreservingReducer",),
        ),
        Variant(
            id="S2c",
            question="S1 + Online Context Compact",
            ladder=True,
            rlm=False,
            mechanisms=("onlineContextCompact",),
        ),
        Variant(
            id="S2d",
            question="S1 + Action Fusion",
            ladder=True,
            rlm=False,
            mechanisms=("actionFusion",),
        ),
        Variant(
            id="S3",
            question="S1 + full SoL-Pi stack",
            ladder=True,
            rlm=False,
            mechanisms=MECHANISMS,
        ),
    )
}

# -------------------------------------------------------------- gateway policy


class GatewayPolicy(_Frozen):
    """What the recorder enforces at Pi's `tool_call` hook, before any tool runs.

    A call outside `allowed_tools`, a fused `then_run` containing an
    unfusable token, an `orca_run` past `max_acquisitions`, or any call after
    `max_requests` provider requests is blocked and spooled as `tool.call`
    with `decision: "blocked"`. `max_requests` also aborts the agent at
    `turn_start` once reached.
    """

    schema_: Literal["onoda.pi-policy/1"] = Field("onoda.pi-policy/1", alias="schema")
    allowed_tools: tuple[str, ...]
    max_requests: int = Field(gt=0)
    max_acquisitions: int = Field(ge=0)
    unfusable_command_tokens: tuple[str, ...] = UNFUSABLE_COMMAND_TOKENS
    # Absolute path -> placeholder, applied to every string before computing a
    # replay key, so a moved workspace or runtime does not change the key.
    path_aliases: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ task bundle

DECIMAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


class TaskBundle(_Frozen):
    """`task.json` at the root of a staged bundle directory.

    Layout: `task.json`, `input/<input_name>`, `output/<output_name>`,
    optional `docs/*.md` (allowed documentation, appended to the system
    prompt in name order) and `ladder.md` (the advisory ladder; required by
    ladder variants). The bundle never contains evaluator references.
    """

    schema_: Literal["onoda.pi-task/1"] = Field("onoda.pi-task/1", alias="schema")
    case_id: str
    instruction: str  # the user prompt handed to `pi -p`
    input_name: str
    input_sha256: str
    output_name: str
    output_sha256: str
    output_provenance: Literal["acquired"] = "acquired"  # executed_here is always false in M3
    review_status: str
    docs: tuple[str, ...] = ()  # file names under docs/
    ladder_sha256: str | None = None
    synthetic_ladder: bool = False  # a test fixture, never a reconstruction of the paper's

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ClaimedQuantity(_Frozen):
    text: str  # decimal text exactly as the agent reports it
    unit: str

    @field_validator("text")
    @classmethod
    def _decimal(cls, v: str) -> str:
        if not DECIMAL_RE.match(v):
            raise ValueError(f"not a decimal: {v!r}")
        return v


class ClaimedState(_Frozen):
    root: int = Field(ge=1)
    multiplicity: int | None = None  # None when the output does not print it (A28)
    energy: ClaimedQuantity


class Claim(_Frozen):
    """The `report_states` tool input, spooled verbatim as the agent's answer."""

    schema_: Literal["onoda.pi-claim/1"] = Field("onoda.pi-claim/1", alias="schema")
    case_id: str
    final_energy: ClaimedQuantity | None = None
    states: tuple[ClaimedState, ...] = ()
    notes: str = ""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ script


class TextBlock(_Frozen):
    type: Literal["text"]
    text: str


class ToolCallBlock(_Frozen):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: dict[str, Any]


Block = Annotated[TextBlock | ToolCallBlock, Field(discriminator="type")]


class ScriptTurn(_Frozen):
    content: tuple[Block, ...]
    # Replay: the provider fails closed (`replay.mismatch`) unless the
    # normalized context it receives hashes to this key.
    expect_replay_key: str | None = None


class Script(_Frozen):
    """Scripted-provider input. Its outputs are synthetic, never model results."""

    schema_: Literal["onoda.pi-script/1"] = Field("onoda.pi-script/1", alias="schema")
    synthetic: Literal[True] = True
    source: str  # who wrote it: a test name, or "replay:<run_id>"
    turns: tuple[ScriptTurn, ...]
    reducer_turns: tuple[ScriptTurn, ...] = ()  # served to SCRIPTED_REDUCER_MODEL

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ spool

SPOOL_EVENTS = "events.jsonl"  # one SpoolLine per line, `seq` from 0, no gaps
SPOOL_OBJECTS = "objects"  # objects/<sha256 hex>, written and fsynced before any line cites it
SPOOL_SCHEMA = "onoda.pi-spool/1"

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Line(_Frozen):
    seq: int = Field(ge=0)


class SpoolStart(_Line):
    type: Literal["spool.start"]
    schema_: Literal["onoda.pi-spool/1"] = Field(alias="schema")
    pi_version: str
    sol_pi_commit: str | None
    variant: str
    mechanisms: tuple[str, ...]
    provider: str
    model: str
    scripted: bool
    session_id: str
    session_dir: str

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ProviderRequest(_Line):
    """What the model was shown for one request, after every extension ran."""

    type: Literal["provider.request"]
    request_index: int = Field(ge=0)
    model: str  # SCRIPTED_REDUCER_MODEL marks a reducer call
    context: Sha256  # raw JSON bytes of the context / provider payload
    source: Literal["scripted-provider", "before_provider_request"]
    replay_key: Sha256  # sha256 of the normalized context (timestamps dropped, paths aliased)


class ProviderResponse(_Line):
    type: Literal["provider.response"]
    request_index: int = Field(ge=0)
    message: Sha256  # the final assistant message JSON
    stop_reason: str
    usage_tokens: dict[str, int]  # input, output, cacheRead, cacheWrite, totalTokens
    cost_usd: str | None  # decimal text of Pi's computed cost; None if unknown


class ToolCall(_Line):
    type: Literal["tool.call"]
    tool_call_id: str
    tool_name: str
    input: Sha256
    request_index: int = Field(ge=0)
    decision: Literal["allowed", "blocked"]
    reason: str | None = None


class ToolResult(_Line):
    """`stage: raw` is before any extension rewrote it; `visible` is what the model sees."""

    type: Literal["tool.result"]
    tool_call_id: str
    tool_name: str
    stage: Literal["raw", "visible"]
    content: Sha256  # JSON of the result content blocks
    details: Sha256 | None = None
    is_error: bool


class TaskAcquire(_Line):
    type: Literal["task.acquire"]
    tool_call_id: str
    case_id: str
    output: Sha256  # the pinned output bytes handed to the agent
    executed_here: Literal[False] = False


class TaskClaim(_Line):
    type: Literal["task.claim"]
    tool_call_id: str
    claim: Sha256  # Claim JSON bytes


class Compaction(_Line):
    type: Literal["compaction"]
    phase: Literal["before", "done", "failed"]
    details: Sha256 | None = None


class ReplayMismatchLine(_Line):
    type: Literal["replay.mismatch"]
    request_index: int
    expected: str
    actual: Sha256


class SpoolEnd(_Line):
    type: Literal["spool.end"]
    status: Literal["settled", "aborted", "budget-exhausted", "replay-mismatch", "script-exhausted"]
    requests: int
    reason: str | None = None


SpoolLine = Annotated[
    SpoolStart
    | ProviderRequest
    | ProviderResponse
    | ToolCall
    | ToolResult
    | TaskAcquire
    | TaskClaim
    | Compaction
    | ReplayMismatchLine
    | SpoolEnd,
    Field(discriminator="type"),
]


class _LineAdapter(BaseModel):
    line: SpoolLine


def parse_spool_line(data: dict[str, Any]) -> Any:
    """Validate one decoded spool line; raises pydantic.ValidationError."""
    return _LineAdapter.model_validate({"line": data}).line


# ------------------------------------------------------------ record mapping

# Checkpoint.context keys for a Pi run (PORTABLE_RUN_SPEC checkpoint contents
# as extended by SOL_PI_DELTA §4).
CTX_VARIANT = "pi.variant"
CTX_MECHANISMS = "pi.mechanisms"  # the sol-pi.json in force, or None
CTX_RUNTIME = "pi.runtime"  # {"pi": PI_VERSION, "sol_pi": commit or None}
CTX_OBSERVATION_PACK = "pi.observation_pack"  # {obs_id: store digest} for archived outputs
CTX_COMPACTIONS = "pi.compactions"  # [{"seq", "phase", "details"}]
CTX_REQUESTS = "pi.requests"

PI_CAPABILITIES = {
    "inspect": "supported",
    "semantic_restart": "unsupported",  # M3 records Pi runs; fork/resume of them is later work
    "native_resume": "unsupported",
    "replay": "scripted-provider",  # re-run under the recorded responses; fails closed on drift
}

RUN_MODES = ("pi-scripted", "pi-live")
PI_EXPERIMENT = "pi-dev"
