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

import ast
import hashlib
import hmac
import json
import re
from collections.abc import Callable
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
# M4: loads after SoL-Pi and before the scripted provider and recorder, so the
# recorder still sees the `ipython` results exactly as the model does.
RLM_EXTENSION = "onoda-rlm.ts"
SCRIPTED_EXTENSION = "onoda-scripted.ts"
RECORDER_EXTENSION = "onoda-recorder.ts"
# The RLM kernel: stdlib-only Python, one persistent namespace per Pi process.
RLM_KERNEL = RUNTIME_DIR / "rlm" / "onoda_rlm_kernel.py"

# ------------------------------------------------------------ environment

ENV_SPOOL = "ONODA_PI_SPOOL"  # spool directory (created by the runtime, empty)
ENV_SCRIPT = "ONODA_PI_SCRIPT"  # Script JSON path; scripted provider only
ENV_TASK = "ONODA_PI_TASK"  # staged TaskBundle directory
ENV_POLICY = "ONODA_PI_POLICY"  # GatewayPolicy JSON path
ENV_RUN_INFO = "ONODA_PI_RUN_INFO"  # SpoolStart fields the recorder copies verbatim
# Path of a 0600 file holding the spool's HMAC key. The spool writer reads it
# when constructed (at extension load, before any tool can run) and unlinks it;
# the recorder then removes every ONODA_PI_* variable from the process
# environment so tool subprocesses inherit none of them.
ENV_SPOOL_KEY = "ONODA_PI_SPOOL_KEY"

# RLM (M4). The extension reads all four at load (before the recorder scrubs
# ONODA_PI_*) and hands a child Pi process its own values explicitly.
ENV_RLM_DEPTH = "ONODA_PI_RLM_DEPTH"  # "0" for the top-level run; a child is its parent's + 1
ENV_RLM_COMPONENTS = "ONODA_PI_RLM_COMPONENTS"  # RlmComponents JSON path
ENV_RLM_WORK = "ONODA_PI_RLM_WORK"  # scratch dir: kernel socket, child staging (never the spool)
ENV_RLM_PYTHON = "ONODA_PI_RLM_PYTHON"  # interpreter that runs RLM_KERNEL

SCRIPTED_PROVIDER = "onoda-scripted"
SCRIPTED_MODEL = "scripted"
SCRIPTED_REDUCER_MODEL = "scripted-reducer"

# ------------------------------------------------------------------- tools

TOOL_CASE_INPUT = "case_input"  # read the staged ORCA input (read-only)
TOOL_ORCA_RUN = "orca_run"  # acquire the pinned output; recorded as acquired, never executed
TOOL_REPORT = "report_states"  # the agent's final claim
TOOL_IPYTHON = "ipython"  # RLM variants only: a persistent Python kernel with rlm()
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
    rlm: bool  # loads the RLM extension (`RLM_EXTENSION`, M4) and its `ipython` tool
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
        rlm = (TOOL_IPYTHON,) if self.rlm else ()
        return (*TASK_TOOLS, *BASE_TOOLS, *extra, *rlm)


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

DECIMAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
# Allowed-documentation file names: a plain `*.md` name, never a path.
DOC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")


class GatewayPolicy(_Frozen):
    """What the recorder enforces at Pi's `tool_call` hook, before any tool runs.

    A call outside `allowed_tools`, a `then_run` without a string `command`
    or containing an unfusable token, an `orca_run` past `max_acquisitions`,
    any call after `max_requests` provider requests, or any call once the
    summed cost reached `max_cost_usd` is blocked and spooled as `tool.call`
    with `decision: "blocked"`. Both budgets also abort the agent at
    `turn_start` once reached; with a cost cap set, a response that carries
    no cost figure ends the run too (fail closed).
    """

    schema_: Literal["onoda.pi-policy/1"] = Field("onoda.pi-policy/1", alias="schema")
    allowed_tools: tuple[str, ...]
    max_requests: int = Field(gt=0)
    max_acquisitions: int = Field(ge=0)
    max_cost_usd: str | None = None  # decimal text; checked against Pi's cost estimate (A35)
    unfusable_command_tokens: tuple[str, ...] = UNFUSABLE_COMMAND_TOKENS
    # Absolute path -> placeholder, applied to every string before computing a
    # replay key, so a moved workspace or runtime does not change the key.
    path_aliases: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @field_validator("max_cost_usd")
    @classmethod
    def _cost(cls, v: str | None) -> str | None:
        if v is not None and not DECIMAL_RE.match(v):
            raise ValueError(f"max_cost_usd is not decimal text: {v!r}")
        return v


# ------------------------------------------------------------------ task bundle


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

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, v: str) -> str:
        # Pi reads a leading `@` as a file include and a leading `-` as a flag.
        if v.startswith(("@", "-")):
            raise ValueError("instruction must not start with '@' or '-'")
        return v

    @field_validator("docs")
    @classmethod
    def _docs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if not DOC_NAME_RE.match(name):
                raise ValueError(f"doc name {name!r} is not a plain *.md file name")
        return v


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
    # Synthetic cost (decimal text, USD) the scripted provider reports for
    # this turn, so tests and replays can drive `max_cost_usd`. Never a price.
    usage_cost: str | None = None

    @field_validator("usage_cost")
    @classmethod
    def _cost(cls, v: str | None) -> str | None:
        if v is not None and not DECIMAL_RE.match(v):
            raise ValueError(f"usage_cost is not decimal text: {v!r}")
        return v


class Script(_Frozen):
    """Scripted-provider input. Its outputs are synthetic, never model results."""

    schema_: Literal["onoda.pi-script/1"] = Field("onoda.pi-script/1", alias="schema")
    synthetic: Literal[True] = True
    source: str  # who wrote it: a test name, or "replay:<run_id>"
    turns: tuple[ScriptTurn, ...]
    reducer_turns: tuple[ScriptTurn, ...] = ()  # served to SCRIPTED_REDUCER_MODEL
    # Served to compaction-summary requests: Pi calls the provider without
    # `onPayload` for them (the agent loop always passes it).
    summary_turns: tuple[ScriptTurn, ...] = ()
    # RLM: the scripts of the child Pi processes this run's rlm() calls spawn,
    # by child index (children/0000 gets children[0]); each nests its own.
    children: tuple[Script, ...] = ()

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------ spool

SPOOL_EVENTS = "events.jsonl"  # one SpoolLine per line, `seq` from 0, no gaps
SPOOL_OBJECTS = "objects"  # objects/<sha256 hex>, written and fsynced before any line cites it
SPOOL_SCHEMA = "onoda.pi-spool/1"

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


MAC_RE = re.compile(rb',"mac":"([0-9a-f]{64})"\}$')


class _Line(_Frozen):
    """Every line may end with `mac`, chaining the spool (tamper evidence, not a sandbox).

    Byte rule, mirrored by `SpoolWriter.line` in `spool.ts`: `body` is the
    line's JSON without `mac` (UTF-8, no trailing newline). The written line
    is `body` with its closing `}` replaced by `,"mac":"<hex>"}`, where hex is
    HMAC-SHA256(key, previous line's mac hex + body), and the first line's
    previous mac is the empty string. A reader recovers `body` from the raw
    line by replacing that exact suffix with `}` (`MAC_RE`).
    """

    seq: int = Field(ge=0)
    mac: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


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
    max_cost_usd: str | None = None  # a live run's cost cap, as given (decimal text)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ProviderRequest(_Line):
    """What the model was shown for one request, after every extension ran."""

    type: Literal["provider.request"]
    request_index: int = Field(ge=0)
    model: str
    context: Sha256  # raw JSON bytes of the context / provider payload
    source: Literal["scripted-provider", "before_provider_request"]
    replay_key: Sha256  # sha256 of the normalized context (timestamps dropped, paths aliased)
    # agent: the main loop; reducer: SoL-Pi's reducer; summary: a compaction summary.
    # Only the scripted provider sees reducer and summary requests (ADAPTER_SEAM §2).
    purpose: Literal["agent", "reducer", "summary"] = "agent"


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


class RlmCell(_Line):
    """One `ipython` cell, spooled after the kernel replied and before Pi hands
    the tool result on. `output` is the kernel's `exec.reply` JSON (as sent)."""

    type: Literal["rlm.cell"]
    tool_call_id: str
    cell_index: int = Field(ge=0)
    code: Sha256  # the cell's source text (UTF-8)
    output: Sha256
    is_error: bool
    truncated: bool


class RlmChildStart(_Line):
    """Written before the child process is spawned."""

    type: Literal["rlm.child.start"]
    tool_call_id: str  # the ipython call whose cell called rlm()
    child_index: int = Field(ge=0)
    depth: int = Field(ge=1)  # the child's depth
    spool_subdir: str = Field(pattern=r"^children/[0-9]{4}$")
    prompt: Sha256  # the child's `-p` instruction after the child_prompt template
    max_requests: int = Field(gt=0)  # the parent's remaining request budget at spawn


class RlmChildEnd(_Line):
    """Written after the child process exited, before the kernel gets the answer."""

    type: Literal["rlm.child.end"]
    child_index: int = Field(ge=0)
    exit_code: int | None  # None: killed or timed out
    end_status: str | None  # the child's spool.end status; None if it wrote none
    requests: int = Field(ge=0)  # provider requests the child spooled (charged to the parent)
    cost_usd: str | None  # decimal text summed from the child's responses; None if any unknown
    answer: Sha256 | None  # the answer text handed back to the cell; None on failure
    error: str | None = None


class RlmChildRefused(_Line):
    """An rlm() call refused before any process was spawned."""

    type: Literal["rlm.child.refused"]
    tool_call_id: str
    reason: Literal["max-depth", "max-children", "budget"]
    detail: str


class RlmState(_Line):
    """A `commit_state` value, spooled before the kernel is acknowledged."""

    type: Literal["rlm.state"]
    tool_call_id: str
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    value: Sha256  # canonical JSON (sorted keys, no NaN/Infinity) of the value


class SpoolEnd(_Line):
    type: Literal["spool.end"]
    status: Literal[
        "settled",
        "aborted",
        "budget-exhausted",
        "replay-mismatch",
        "script-exhausted",
        "provider-error",  # the last assistant message ended with stopReason "error"
    ]
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
    | RlmCell
    | RlmChildStart
    | RlmChildEnd
    | RlmChildRefused
    | RlmState
    | SpoolEnd,
    Field(discriminator="type"),
]


class _LineAdapter(BaseModel):
    line: SpoolLine


def parse_spool_line(data: dict[str, Any]) -> Any:
    """Validate one decoded spool line; raises pydantic.ValidationError."""
    return _LineAdapter.model_validate({"line": data}).line


# ------------------------------------------------------------------- RLM (M4)

# The kernel protocol: JSON Lines over one AF_UNIX stream socket. The host
# (onoda-rlm.ts) listens; the kernel connects, sends `ready`, and then both
# sides may send at any time. Host -> kernel: `init` (once, first), `exec`,
# `call.reply`, `shutdown`. Kernel -> host: `ready`, `exec.reply`, `call`.
# While a cell runs, the kernel may send `call` (rlm / commit_state) and
# blocks that cell until the matching `call.reply`. Ids are per-sender
# integers from 0; a reply carries the id of what it answers. Mirrored by
# `pi/extensions/lib/rlm-protocol.ts` and `pi/rlm/onoda_rlm_kernel.py`.
KERNEL_PROTOCOL = "onoda.rlm-kernel/1"
# Appended to a stream's captured output when it exceeds max_output_chars
# (`{limit}` is the cap, `{total}` the uncapped length).
TRUNCATION_MARKER = "\n[onoda-rlm: output truncated at {limit} of {total} chars]"
# Modules neither the kernel source nor the `rlm.helpers` component may import.
RLM_FORBIDDEN_IMPORTS = ("pickle", "marshal", "shelve", "subprocess", "urllib", "http", "ssl")


class KernelInit(_Frozen):
    op: Literal["init"]
    id: int = Field(ge=0)
    protocol: Literal["onoda.rlm-kernel/1"]
    helpers: str  # Python source exec'd into the namespace before any cell
    max_output_chars: int = Field(gt=0)


class KernelExec(_Frozen):
    op: Literal["exec"]
    id: int = Field(ge=0)
    code: str


class KernelCallReply(_Frozen):
    """The host's answer to a kernel `call`. `ok: false` raises `RlmError(error)` in the cell."""

    op: Literal["call.reply"]
    id: int = Field(ge=0)  # the kernel call's id
    ok: bool
    value: str | None = None  # rlm: the child's answer text; commit_state: the value's digest
    error: str | None = None


class KernelShutdown(_Frozen):
    op: Literal["shutdown"]
    id: int = Field(ge=0)


class KernelReady(_Frozen):
    op: Literal["ready"]
    protocol: Literal["onoda.rlm-kernel/1"]
    pid: int


class KernelExecReply(_Frozen):
    """A finished cell. stdout ends with repr() of a trailing expression, as a REPL prints it.

    `error` is the formatted exception (traceback) of a raising cell, else
    None; the kernel keeps running either way. `stdout`/`stderr` are each
    already capped at max_output_chars with TRUNCATION_MARKER; `truncated`
    says whether either was. A reply to `init` has the same shape (the
    helpers' output and error).
    """

    op: Literal["exec.reply"]
    id: int = Field(ge=0)  # the exec (or init) request's id
    stdout: str
    stderr: str
    error: str | None
    truncated: bool


class KernelCall(_Frozen):
    """A call from inside a running cell.

    rlm args: {"prompt": str, "context": str | None}. commit_state args:
    {"name": str, "json": str}, where `json` is `canonical_state_json(value)`
    computed in the kernel; the host spools those exact bytes (UTF-8) as the
    `rlm.state` object and acknowledges with `value` = "sha256:<hex>" of them.
    """

    op: Literal["call"]
    id: int = Field(ge=0)
    fn: Literal["rlm", "commit_state"]
    args: dict[str, Any]


KernelRequest = Annotated[
    KernelInit | KernelExec | KernelCallReply | KernelShutdown, Field(discriminator="op")
]
KernelReply = Annotated[KernelReady | KernelExecReply | KernelCall, Field(discriminator="op")]


class _KernelAdapter(BaseModel):
    request: KernelRequest | None = None
    reply: KernelReply | None = None


def parse_kernel_message(data: dict[str, Any]) -> Any:
    """Validate one decoded protocol message in either direction."""
    key = "request" if data.get("op") in ("init", "exec", "call.reply", "shutdown") else "reply"
    return getattr(_KernelAdapter.model_validate({key: data}), key)


def derive_child_key(key: bytes, child_index: int) -> bytes:
    """A child spool's HMAC key: HMAC-SHA256(parent key, "onoda.pi-rlm-child/<nnnn>").

    Mirrored by `SpoolWriter.childKey` in `spool.ts`; applied recursively for
    grandchildren. No child key is ever written to a file the host keeps, and
    ingest can still verify each child spool from the one in-memory launch key.
    """
    msg = f"onoda.pi-rlm-child/{child_index:04d}".encode()
    return hmac.new(key, msg, hashlib.sha256).digest()


def canonical_state_json(value: Any) -> str:
    """The bytes an `rlm.state` object holds: sorted keys, compact, no NaN/Infinity.

    Mirrors the kernel's own serialization, so the digest the host acknowledges
    is the digest ingest sees.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class RlmLimits(_Frozen):
    max_depth: int = Field(ge=0, le=4)  # deepest child allowed; 0 disables rlm()
    max_children: int = Field(ge=0, le=64)  # rlm() spawns per process (refusals not counted)
    max_output_chars: int = Field(gt=0, le=1_000_000)  # per stream, per cell


class RlmComponents(_Frozen):
    """What `ENV_RLM_COMPONENTS` points at: the candidate's RLM components, parsed."""

    schema_: Literal["onoda.pi-rlm-components/1"] = Field(
        "onoda.pi-rlm-components/1", alias="schema"
    )
    system_prompt: str  # appended to Pi's system prompt at before_agent_start
    tool_doc: str  # the ipython tool's description
    child_prompt: str  # template with {prompt} and {context}; the child's `-p` text
    helpers: str  # Python source exec'd into the kernel namespace at start
    limits: RlmLimits
    candidate_digest: str  # HarnessCandidate.digest(), for the record

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


COMPONENT_MAX_CHARS = 65536


def _check_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} is empty")
    if len(value) > COMPONENT_MAX_CHARS:
        raise ValueError(f"{name} is longer than {COMPONENT_MAX_CHARS} chars")


def forbidden_imports(tree: ast.AST) -> list[str]:
    """Imports in `tree` of an RLM_FORBIDDEN_IMPORTS module, plus any __import__ call."""
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "__import__":
            found.append("__import__")
        found += [n for n in names if n.split(".")[0] in RLM_FORBIDDEN_IMPORTS]
    return found


def _check_helpers(value: str) -> None:
    _check_text("rlm.helpers", value)
    try:
        tree = ast.parse(value)
    except SyntaxError as exc:
        raise ValueError(f"rlm.helpers is not valid Python: {exc}") from exc
    bad = forbidden_imports(tree)
    if bad:
        raise ValueError(f"rlm.helpers may not import {', '.join(bad)}")


def _check_child_prompt(value: str) -> None:
    _check_text("rlm.child_prompt", value)
    if "{prompt}" not in value:
        raise ValueError("rlm.child_prompt must contain {prompt}")
    try:
        rendered = value.format(prompt="p", context="c")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"rlm.child_prompt is not a template: {exc}") from exc
    if rendered.startswith(("@", "-")):
        raise ValueError("rlm.child_prompt must not start with '@' or '-'")


def _check_limits(value: str) -> None:
    RlmLimits.model_validate_json(value)


def _check_sol_pi(value: str) -> None:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"sol_pi.config is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("sol_pi.config must be a JSON object")
    for k, v in data.items():
        if k not in MECHANISMS:
            raise ValueError(f"sol_pi.config may only toggle the four mechanisms, not {k!r}")
        if not isinstance(v, bool):
            raise ValueError(f"sol_pi.config.{k} must be true or false")


# The optimizer's whole reach (A45). Anything else (evaluator, parsers,
# gateway, budgets, splits) is not a component and cannot be proposed.
COMPONENT_VALIDATORS: dict[str, Callable[[str], None]] = {
    "rlm.system_prompt": lambda v: _check_text("rlm.system_prompt", v),
    "rlm.tool_doc": lambda v: _check_text("rlm.tool_doc", v),
    "rlm.child_prompt": _check_child_prompt,
    "rlm.helpers": _check_helpers,
    "rlm.limits": _check_limits,
    "ladder": lambda v: _check_text("ladder", v),  # replaces the bundle's advisory ladder
    "sol_pi.config": _check_sol_pi,  # overrides the variant's mechanism booleans
}
REQUIRED_COMPONENTS = (
    "rlm.system_prompt",
    "rlm.tool_doc",
    "rlm.child_prompt",
    "rlm.helpers",
    "rlm.limits",
)


class HarnessCandidate(_Frozen):
    """A named set of text components: the unit GEPA proposes and the gates judge.

    `components` is GEPA's `dict[str, str]`. Every name must be in
    `COMPONENT_VALIDATORS`, every `REQUIRED_COMPONENTS` name present, and each
    value pass its validator; `ladder` and `sol_pi.config` are optional.
    """

    components: dict[str, str]

    @field_validator("components")
    @classmethod
    def _components(cls, v: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(v) - set(COMPONENT_VALIDATORS))
        if unknown:
            raise ValueError(f"undeclared components: {', '.join(unknown)}")
        missing = [n for n in REQUIRED_COMPONENTS if n not in v]
        if missing:
            raise ValueError(f"missing components: {', '.join(missing)}")
        for name, value in v.items():
            COMPONENT_VALIDATORS[name](value)
        return v

    def digest(self) -> str:
        """sha256 of the components as sorted-key JSON: the candidate's identity."""
        body = json.dumps(self.components, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    @property
    def limits(self) -> RlmLimits:
        return RlmLimits.model_validate_json(self.components["rlm.limits"])

    def sol_pi_mechanisms(self) -> tuple[Mechanism, ...] | None:
        """The mechanisms `sol_pi.config` turns on, or None when it is not set."""
        raw = self.components.get("sol_pi.config")
        if raw is None:
            return None
        data = json.loads(raw)
        return tuple(m for m in MECHANISMS if data.get(m, False))

    def rlm_components(self) -> RlmComponents:
        c = self.components
        return RlmComponents(
            system_prompt=c["rlm.system_prompt"],
            tool_doc=c["rlm.tool_doc"],
            child_prompt=c["rlm.child_prompt"],
            helpers=c["rlm.helpers"],
            limits=self.limits,
            candidate_digest=self.digest(),
        )


# Adapted from DSPy's RLM action instructions (dspy 3.4.0,
# dspy/predict/rlm.py ACTION_INSTRUCTIONS_TEMPLATE, MIT; provenance S18):
# llm_query -> rlm(), SUBMIT -> report_states, and the REPL is a Pi tool.
_SEED_SYSTEM_PROMPT = """\
## Python REPL (ipython tool) and recursive calls

You have an `ipython` tool: a persistent Python REPL. Write Python code and it \
will be executed; you see its output, then write more code based on what you \
learned. This is an iterative process. Variables persist between cells.

Available inside the REPL:
- `rlm(prompt, context=None)` - ask a sub-agent (a fresh Pi session with the \
same task tools) a focused question; returns its answer as a string. Calls \
are limited and draw from your own request budget.
- `commit_state(name, value)` - record a JSON value you will rely on later. \
Anything not committed lives only in the REPL and may be lost.
- `print()` - ALWAYS print to see results.

1. EXPLORE FIRST - Look at your data before processing it. Print samples, \
check types/lengths, understand the structure.
2. ITERATE - Write small code snippets, observe outputs, then decide next \
steps. State persists between cells.
3. VERIFY BEFORE REPORTING - If results seem wrong (zeros, empty, unexpected), \
reconsider your approach.
4. USE rlm() FOR SEMANTICS - String matching finds WHERE things are; a \
sub-agent can judge WHAT they mean.
5. MINIMIZE RETYPING - When values are long, precise, or error-prone (numbers, \
units, identifiers), carry them in variables and parse them in code instead \
of retyping. Copy reported values exactly as printed.
6. REPORT ONLY AFTER SEEING OUTPUTS - report_states records your final \
answer. Inspect printed output first, then report in a later step."""

_SEED_TOOL_DOC = (
    "Run Python code in a persistent REPL. Returns stdout, stderr and the repr of a "
    "trailing expression. Inside the REPL, rlm(prompt, context=None) asks a sub-agent "
    "and returns its answer; commit_state(name, value) records a JSON value durably."
)

_SEED_CHILD_PROMPT = (
    "You are a sub-agent answering one focused question for a parent agent. "
    "Answer in plain text; your final message is returned to the parent verbatim.\n\n"
    "Question: {prompt}\n\nContext:\n{context}"
)

_SEED_HELPERS = (
    "def lines_matching(text, needle):\n"
    '    """(line number, line) pairs of `text` whose line contains `needle`."""\n'
    "    return [(n, ln) for n, ln in enumerate(text.splitlines(), 1) if needle in ln]\n"
)

SEED_CANDIDATE = HarnessCandidate(
    components={
        "rlm.system_prompt": _SEED_SYSTEM_PROMPT,
        "rlm.tool_doc": _SEED_TOOL_DOC,
        "rlm.child_prompt": _SEED_CHILD_PROMPT,
        "rlm.helpers": _SEED_HELPERS,
        "rlm.limits": RlmLimits(
            max_depth=1, max_children=4, max_output_chars=10000
        ).model_dump_json(),
    }
)
SEED_PROVENANCE = {
    "rlm.system_prompt": "adapted from dspy 3.4.0 dspy/predict/rlm.py "
    "ACTION_INSTRUCTIONS_TEMPLATE (MIT, provenance S18)",
    "rlm.tool_doc": "onoda",
    "rlm.child_prompt": "onoda",
    "rlm.helpers": "onoda",
    "rlm.limits": "onoda defaults, not tuned (A45)",
}


# ------------------------------------------------------------ record mapping

# Checkpoint.context keys for a Pi run (PORTABLE_RUN_SPEC checkpoint contents
# as extended by SOL_PI_DELTA §4).
CTX_VARIANT = "pi.variant"
CTX_MECHANISMS = "pi.mechanisms"  # the sol-pi.json in force, or None
CTX_RUNTIME = "pi.runtime"  # {"pi": PI_VERSION, "sol_pi": commit or None}
CTX_OBSERVATION_PACK = "pi.observation_pack"  # {obs_id: store digest} for archived outputs
CTX_COMPACTIONS = "pi.compactions"  # [{"seq", "phase", "details"}]
CTX_REQUESTS = "pi.requests"
CTX_RLM_STATE = "pi.rlm_state"  # {name: store digest} of committed state; never kernel memory

PI_CAPABILITIES = {
    "inspect": "supported",
    "semantic_restart": "unsupported",  # M3 records Pi runs; fork/resume of them is later work
    "native_resume": "unsupported",
    "replay": "scripted-provider",  # re-run under the recorded responses; fails closed on drift
}

RUN_MODES = ("pi-scripted", "pi-live")
PI_EXPERIMENT = "pi-dev"
