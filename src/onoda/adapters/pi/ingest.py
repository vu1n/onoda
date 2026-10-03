"""Ingest a Pi spool into the store as a portable run record (SOL_PI_DELTA §4, A37).

The Pi process never writes the store. Its extensions write an fsynced spool
(the write-ahead log); after the process exits, `ingest_spool` replays that
spool into ordinary records, in spool order, bytes before records before the
journal event that cites them:

- `provider.request`/`provider.response` pairs become `decision` receipts
  (inputs `context`, outputs `message`, the request's replay key in
  metadata). Reducer requests and compaction-summary requests are their own
  receipts, labelled `reducer` and `compaction-summary`. Scripted runs are
  `simulated` and labelled `synthetic`.
- allowed `tool.call`s become `read`/`execution` receipts when their visible
  result arrives; blocked calls (including calls Pi rejected before its
  `tool_call` hook) become `pi.tool-blocked` events, never receipts.
- `task.acquire` is an acquisition `read` receipt citing the pinned output
  with `executed_here: false`, followed by a `job-collected` checkpoint.
- `task.execute` (VTE tasks, A59) is an `execution` receipt labelled
  `orca-local` (`executed_here: true`) or, for a broker cache hit, an
  acquisition `read` receipt (`executed_here: false`, `cache_hit: true`);
  both cite the submitted input and the job's log and are followed by a
  `job-collected` checkpoint whose `visible` names the job (`job.<job id>`).
  Every line must agree with the broker's journal (job id, status, input and
  output sha256, `executed_here`, `cache_hit`); a spool with any
  `task.execute` is refused without that journal. Broker jobs no line cites
  (the extension stopped between the reply and its spool write) are listed
  in a `pi.broker-unspooled` event, so no host execution goes unrecorded.
- In a variant with Action Fusion, `edit`/`write` calls with `then_run` are
  decomposed with `context.decompose_fused_call` into `write_input` +
  `validate_input`; elsewhere Pi ignores `then_run` and the call is plain.
- ObservationPack archives under `<session_dir>/sol-pi/<session_id>/
  observation-pack/` are imported as `context.observation-archived` events
  (obs id -> store digest) for each archived object that matches a recorded
  tool result, `obs_recall` calls as `context.page`.
- a raw tool result that differs from the visible one is `context.reduced`.
- `compaction` lines with phase `done` go through `context.record_compaction`.
- the agent's claim is a `pi-claim` artifact cited by a `pi.claim` event; a
  VTE claim (`report_vtes`) is a `pi-vte-claim` artifact, and its event
  carries the claim schema.
- RLM (M4): an `rlm.cell` joins its `ipython` call's tool receipt (the cell's
  `code` as an input, the kernel's reply as the `cell` output, both committed
  first). Each `rlm.state` value is an `rlm-state` artifact cited by an
  `rlm.state` event, and every later checkpoint carries `CTX_RLM_STATE`
  (name -> digest, latest value per name), so a checkpoint relies on committed
  state, never on kernel memory (A43). Each child spool under
  `children/<nnnn>/` is ingested first as its own run `<run_id>.rlm-<nnnn>`
  (keyed with `derive_child_key`, same task, config plus `parent_run`,
  `child_index`, `depth`), then the parent's `rlm.child` event cites it and,
  when the child completed, its finalized checkpoint. A child whose parent
  wrote no `rlm.child.end`, or whose spool has no `spool.end`, is recorded as
  incomplete and cites no checkpoint. Refused calls are `rlm.child-refused`
  events. Grandchildren nest the same way.

Everything in the spool is untrusted input: every line and object is
validated; given the launch's HMAC key, every line's chained `mac` must
verify (tamper evidence against the agent's own tools, not a sandbox); a
`session_dir` outside `expected_session_dir` is refused; and anything
inconsistent (a torn object, a result for an unknown
call, a fused solver submission recorded as allowed) raises before a single
record is written. Each write is keyed by its spool position, so re-running
an interrupted ingest continues it, re-ingesting a finished one is a no-op,
and a different spool under the same run id is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from onoda.adapters.pi.contract import (
    CTX_COMPACTIONS,
    CTX_MECHANISMS,
    CTX_OBSERVATION_PACK,
    CTX_REQUESTS,
    CTX_RLM_STATE,
    CTX_RUNTIME,
    CTX_VARIANT,
    DECIMAL_RE,
    MAC_RE,
    PI_CAPABILITIES,
    PI_EXPERIMENT,
    PI_PACKAGE,
    RUN_MODES,
    SCRIPTED_PROVIDER,
    SCRIPTED_REDUCER_MODEL,
    SPOOL_EVENTS,
    SPOOL_OBJECTS,
    TOOL_CASE_INPUT,
    TOOL_IPYTHON,
    TOOL_ORCA_RUN,
    TOOL_ORCA_SUBMIT,
    TOOL_REPORT,
    TOOL_REPORT_VTE,
    TOOL_STRUCTURE,
    UNFUSABLE_COMMAND_TOKENS,
    VARIANTS,
    Claim,
    Compaction,
    ProviderRequest,
    ProviderResponse,
    ReplayMismatchLine,
    RlmCell,
    RlmChildEnd,
    RlmChildRefused,
    RlmChildStart,
    RlmState,
    Script,
    ScriptTurn,
    SpoolEnd,
    SpoolStart,
    TaskAcquire,
    TaskClaim,
    TaskExecute,
    ToolCall,
    ToolResult,
    Variant,
    canonical_state_json,
    derive_child_key,
    parse_spool_line,
    with_mechanisms,
)
from onoda.adapters.pi.vte_contract import JOB_ID_RE, VTE_CLAIM_SCHEMA, VteClaim
from onoda.budgets import child_links
from onoda.canonical import DIGEST_PREFIX, canonical_bytes, sha256_digest
from onoda.context import decompose_fused_call, record_compaction
from onoda.errors import CapabilityError, IntegrityError, MissingObjectError, OnodaError
from onoda.records import Checkpoint, Event, InvocationReceipt, Run, StepSpec, TaskManifest, Usage
from onoda.science.orca_local import _ORCA_LABELS
from onoda.science.orca_local import _STAND_IN_LABELS as _SCHEDULER_STAND_IN_LABELS
from onoda.store import Store, check_safe_id, record_ref

ACTOR = "pi-ingest"
ACQUIRE = "acquire:orca-output"  # same operation name as the M2 case runner's acquisition
EXECUTE = "execute:orca-job"  # a broker job ORCA ran for this submission
ACQUIRE_CACHED = "acquire:orca-cache"  # a broker cache hit: an earlier job's bytes
BROKER_CODE_REF = "onoda.science.orca_broker"
# `orca_submit`'s tool receipt records what the agent saw; the ORCA job itself
# is the receipt its `task.execute` line becomes, so the job is never counted twice.
READ_TOOLS = frozenset(
    {TOOL_CASE_INPUT, TOOL_ORCA_RUN, TOOL_STRUCTURE, TOOL_ORCA_SUBMIT, "read", "obs_recall"}
)
CLAIM_TOOLS = {TOOL_REPORT: Claim, TOOL_REPORT_VTE: VteClaim}
# The scheduler's own job labels; a stand-in's observations are also synthetic.
_JOB_LABELS = _ORCA_LABELS
_STAND_IN_LABELS = [*_SCHEDULER_STAND_IN_LABELS, "synthetic"]
FUSED_TOOLS = frozenset({"edit", "write"})
OBS_RECALL = "obs_recall"
_OBS_ID = re.compile(r"^obs_[0-9a-f]{24}$")
_SESSION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
CHILDREN_DIR = "children"  # RlmChildStart.spool_subdir is `children/<nnnn>`
# Config keys ingest sets on a child run; a top-level config may not carry them.
CHILD_CONFIG_KEYS = ("parent_run", "child_index", "depth")


@dataclass(frozen=True)
class IngestResult:
    """What the journal holds for one ingested run (read back from the store)."""

    run_id: str
    spool: str  # digest of the ingested events.jsonl
    complete: bool  # the spool ended with `spool.end` and a finalized checkpoint exists
    status: str | None  # `spool.end` status
    noop: bool  # this call found the spool already ingested
    invocations: tuple[str, ...]
    checkpoints: tuple[str, ...]
    claim: str | None  # digest of the last claim
    observation_pack: dict[str, str]  # obs id -> store digest
    children: tuple[str, ...] = ()  # child run ids, in child-index order (RLM)
    rlm_state: dict[str, str] = field(default_factory=dict)  # name -> digest, latest


class BrokerJournalRecord(Protocol):
    """One answered submission in the host broker's journal (A59).

    `vte_contract.BrokerReply` satisfies it, as does the broker's own journal
    entry. Entries without a job id (requests, refusals) are skipped; an
    optional `stand_in` attribute marks a job a stand-in executable ran.
    """

    job_id: str | None
    status: str
    cache_hit: bool
    executed_here: bool
    input_sha256: str | None
    output_sha256: str | None


# ------------------------------------------------------------------ reading


def _read_file(path: Path, what: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"{what} {path.name} is not a regular file")
    return path.read_bytes()


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _load_json(data: bytes, what: str) -> Any:
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except ValueError as exc:
        raise IntegrityError(f"{what} is not valid JSON: {exc}") from exc


def _number(value: Any) -> int | str | None:
    """A JSON number as a canonical value (ints stay, floats become decimal text); else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and DECIMAL_RE.match(repr(value)):
        return repr(value)
    return None


def _decimalize(value: Any) -> Any:
    """Floats (e.g. SoL-Pi's cacheWriteReadRatio) as decimal text, so records stay float-free."""
    if isinstance(value, float):
        if (text := _number(value)) is None:
            raise OnodaError(f"non-finite number {value!r} in the SoL-Pi config")
        return text
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimalize(v) for v in value]
    return value


def _cited(line: Any) -> list[str]:
    """Object hashes a spool line cites."""
    match line:
        case ProviderRequest():
            return [line.context]
        case ProviderResponse():
            return [line.message]
        case ToolCall():
            return [line.input]
        case ToolResult():
            return [line.content] + ([line.details] if line.details else [])
        case TaskAcquire():
            return [line.output]
        case TaskExecute():
            return [line.input, line.output]
        case TaskClaim():
            return [line.claim]
        case Compaction():
            return [line.details] if line.details else []
        case RlmCell():
            return [line.code, line.output]
        case RlmChildStart():
            return [line.prompt]
        case RlmChildEnd():
            return [line.answer] if line.answer else []
        case RlmState():
            return [line.value]
    return []


@dataclass(frozen=True)
class _Spool:
    lines: tuple[Any, ...]
    objects: dict[str, bytes]  # sha256 hex -> verified bytes
    events: bytes  # events.jsonl up to its last complete line
    digest: str
    torn_tail: bool

    @property
    def start(self) -> SpoolStart:
        return self.lines[0]

    @property
    def end(self) -> SpoolEnd | None:
        return self.lines[-1] if isinstance(self.lines[-1], SpoolEnd) else None


def _check_mac(text: bytes, n: int, key: bytes, prev: str) -> str:
    """Verify line `n`'s chained mac (contract `_Line`); returns it."""
    m = MAC_RE.search(text)
    if m is None:
        raise IntegrityError(f"spool line {n} carries no mac")
    body = text[: m.start()] + b"}"
    expected = hmac.new(key, prev.encode() + body, hashlib.sha256).hexdigest()
    mac = m.group(1).decode()
    if not hmac.compare_digest(expected, mac):
        raise IntegrityError(f"spool line {n} fails its mac: the spool was altered")
    return mac


def _read_spool(spool_dir: Path, key: bytes | None = None) -> _Spool:
    raw = _read_file(spool_dir / SPOOL_EVENTS, "spool")
    # A torn final line was never fsynced, so nothing downstream saw it (WAL rule).
    torn_tail = bool(raw) and not raw.endswith(b"\n")
    if torn_tail:
        raw = raw[: raw.rfind(b"\n") + 1]
    lines = []
    prev_mac = ""
    for n, text in enumerate(raw.split(b"\n")[:-1] if raw else []):
        if key is not None:
            prev_mac = _check_mac(text, n, key, prev_mac)
        data = _load_json(text, f"spool line {n}")
        if not isinstance(data, dict):
            raise IntegrityError(f"spool line {n} is not an object")
        try:
            line = parse_spool_line(data)
        except ValidationError as exc:
            raise IntegrityError(f"spool line {n} violates the contract: {exc}") from exc
        if line.seq != n:
            raise IntegrityError(f"spool line {n} has seq {line.seq}")
        lines.append(line)
    if not lines or not isinstance(lines[0], SpoolStart):
        raise IntegrityError("spool does not begin with spool.start")
    for line in lines[1:]:
        if isinstance(line, SpoolStart):
            raise IntegrityError(f"second spool.start at seq {line.seq}")
        if isinstance(line, SpoolEnd) and line is not lines[-1]:
            raise IntegrityError(f"spool.end at seq {line.seq} is not the last line")
    objects: dict[str, bytes] = {}
    for line in lines:
        for hexdigest in _cited(line):
            if hexdigest in objects:
                continue
            data = _read_file(spool_dir / SPOOL_OBJECTS / hexdigest, "spool object")
            if hashlib.sha256(data).hexdigest() != hexdigest:
                raise IntegrityError(f"spool object {hexdigest} does not match its name")
            objects[hexdigest] = data
    return _Spool(tuple(lines), objects, raw, sha256_digest(raw), torn_tail)


@dataclass(frozen=True)
class _Archive:
    """SoL-Pi ObservationPack's session archive: objects/<obs id>.txt + ledger.jsonl."""

    objects: dict[str, bytes] = field(default_factory=dict)
    ledger: bytes | None = None
    stats: dict[str, dict[str, int]] = field(default_factory=dict)  # obs id -> event counts


def _read_archive(start: SpoolStart, expected_session_dir: Path | None) -> _Archive:
    if not _SESSION_ID.match(start.session_id):
        raise IntegrityError(f"unsafe Pi session id {start.session_id!r}")
    session_dir = Path(start.session_dir)
    if expected_session_dir is not None:
        if not (
            session_dir.is_absolute()
            and session_dir.resolve().is_relative_to(Path(expected_session_dir).resolve())
        ):
            raise IntegrityError(
                f"spool session_dir {start.session_dir!r} is outside {expected_session_dir}"
            )
    root = session_dir / "sol-pi" / start.session_id / "observation-pack"
    if not root.is_dir() or root.is_symlink():
        return _Archive()
    objects: dict[str, bytes] = {}
    obj_dir = root / "objects"
    if obj_dir.is_dir() and not obj_dir.is_symlink():
        for path in sorted(obj_dir.iterdir()):
            obs_id = path.name.removesuffix(".txt")
            if not (path.name.endswith(".txt") and _OBS_ID.match(obs_id)):
                raise IntegrityError(f"unexpected ObservationPack object {path.name}")
            objects[obs_id] = _read_file(path, "ObservationPack object")
    ledger_path = root / "ledger.jsonl"
    if not ledger_path.exists():
        return _Archive(objects)
    ledger = _read_file(ledger_path, "ObservationPack ledger")
    # SoL-Pi appends without fsync; a torn final entry is dropped like a torn spool line.
    if ledger and not ledger.endswith(b"\n"):
        ledger = ledger[: ledger.rfind(b"\n") + 1]
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"full": 0, "placeholder": 0, "recall": 0}
    )
    for n, text in enumerate(ledger.splitlines()):
        entry = _load_json(text, f"ObservationPack ledger line {n}")
        if not isinstance(entry, dict) or entry.get("event") not in (
            "full",
            "placeholder",
            "recall",
        ):
            raise IntegrityError(f"ObservationPack ledger line {n} is not a known entry")
        obs_id = entry.get("id")
        if obs_id not in objects:
            raise IntegrityError(f"ObservationPack ledger cites unarchived {obs_id!r}")
        content_hash = entry.get("contentHash")
        if content_hash is not None and content_hash != hashlib.sha256(objects[obs_id]).hexdigest():
            raise IntegrityError(f"ObservationPack object {obs_id} does not match its ledger hash")
        stats[obs_id][entry["event"]] += 1
    return _Archive(objects, ledger, dict(stats))


def _observation_id(line: ToolResult, content: Any) -> tuple[str, bytes] | None:
    """The id SoL-Pi would archive this visible result under, and its text (observation.ts)."""
    if line.is_error or not isinstance(content, list) or not content:
        return None
    if not all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return None
    text = "\n".join(str(b.get("text", "")) for b in content).encode()
    content_hash = hashlib.sha256(text).hexdigest()
    key = f"{line.tool_name}\0{line.tool_call_id}\0{content_hash}".encode()
    return f"obs_{hashlib.sha256(key).hexdigest()[:24]}", text


# ---------------------------------------------------------------- validation


@dataclass
class _Call:
    line: ToolCall
    args: dict[str, Any]
    fused: list[dict[str, Any]] | None
    raw: ToolResult | None = None
    visible: ToolResult | None = None


@dataclass
class _Analysis:
    calls: dict[str, _Call]
    request_of: dict[int, ProviderRequest]  # response seq -> its request
    unanswered: list[ProviderRequest]
    observation_of: dict[int, str]  # visible result seq -> archived obs id
    child_starts: dict[int, RlmChildStart] = field(default_factory=dict)
    child_ends: dict[int, RlmChildEnd] = field(default_factory=dict)
    executions: list[TaskExecute] = field(default_factory=list)


def _fused_steps(line: ToolCall, args: dict[str, Any], tokens: tuple[str, ...], fusion: bool):
    """Decomposed steps of an Action Fusion call, or None for a plain call.

    Without Action Fusion in the variant Pi ignores `then_run`, so the call is
    plain whatever `then_run` holds.
    """
    then_run = args.get("then_run")
    if not fusion or line.tool_name not in FUSED_TOOLS or then_run is None:
        return None
    command = then_run.get("command") if isinstance(then_run, dict) else None
    if not isinstance(command, str):
        if line.decision == "blocked":
            return []  # marks the call as fused; there is nothing to decompose
        # An allowed then_run we cannot read could be anything, including a submission.
        raise IntegrityError(f"allowed fused call {line.tool_call_id} has a malformed then_run")
    path = args.get("path") if isinstance(args.get("path"), str) else None
    if line.decision == "allowed":
        hits = sorted(t for t in tokens if t in command.lower())
        if hits:
            raise CapabilityError(
                f"spool records fused call {line.tool_call_id} as allowed although its then_run "
                f"contains unfusable {hits}; a fused submission is never allowed, so the spool "
                "is untrusted and nothing was ingested"
            )
    validate: dict[str, Any] = {"operation": "validate_input", "command": command}
    if "timeout" in then_run:
        validate["timeout_s"] = _number(then_run["timeout"])  # None if not a number
    common = {"tool_call_id": line.tool_call_id, "tool": line.tool_name}
    return [{"operation": "write_input", "path": path, **common}, {**validate, **common}]


def _analyze(
    spool: _Spool, archive: _Archive, task: TaskManifest, config: dict[str, Any]
) -> _Analysis:
    """Check the whole spool against the contract and the run; raises before any write."""
    start = spool.start
    if start.variant != config["variant"] or start.variant not in VARIANTS:
        raise OnodaError(f"spool variant {start.variant!r} is not the run's {config['variant']!r}")
    variant = _effective_variant(config)
    if set(start.mechanisms) != set(variant.mechanisms):
        raise IntegrityError(f"spool mechanisms {start.mechanisms} do not match {variant.id}")
    if start.scripted != (config["mode"] == "pi-scripted"):
        raise OnodaError(f"spool scripted={start.scripted} contradicts mode {config['mode']!r}")
    if start.sandbox != config.get("sandbox"):
        raise OnodaError(f"spool sandbox={start.sandbox!r} contradicts the run's")
    tokens = tuple(t.lower() for t in (*UNFUSABLE_COMMAND_TOKENS, *config.get("unfusable", ())))
    fusion = "actionFusion" in variant.mechanisms
    depth = config.get("depth", 0)
    cells: set[str] = set()
    child_starts: dict[int, RlmChildStart] = {}
    child_ends: dict[int, RlmChildEnd] = {}
    executions: dict[str, TaskExecute] = {}  # job id -> its line

    calls: dict[str, _Call] = {}
    pending: dict[int, list[ProviderRequest]] = defaultdict(list)
    request_of: dict[int, ProviderRequest] = {}
    observation_of: dict[int, str] = {}

    def call_for(line: Any, tool: str | None = None) -> _Call:
        call = calls.get(line.tool_call_id)
        if call is None:
            raise IntegrityError(f"seq {line.seq} cites unknown tool call {line.tool_call_id!r}")
        if tool is not None and (call.line.tool_name != tool or call.line.decision != "allowed"):
            raise IntegrityError(f"seq {line.seq} is not from an allowed {tool} call")
        return call

    for line in spool.lines:
        match line:
            case ProviderRequest():
                # A request's role is its spooled purpose, never its model id, so the
                # reducer may share the agent's model. Each source writes only its roles.
                if (line.source == "before_provider_request") != (line.purpose == "agent") and (
                    line.source != "scripted-provider"
                ):
                    raise IntegrityError(
                        f"request at seq {line.seq}: source {line.source} cannot write "
                        f"a {line.purpose} request"
                    )
                if (line.source == "scripted-provider") != start.scripted:
                    raise IntegrityError(
                        f"request at seq {line.seq}: source {line.source} in a "
                        f"{'scripted' if start.scripted else 'live'} run"
                    )
                pending[line.request_index].append(line)
            case ProviderResponse():
                if not pending[line.request_index]:
                    raise IntegrityError(f"response at seq {line.seq} answers no request")
                request_of[line.seq] = pending[line.request_index].pop(0)
                if line.cost_usd is not None and not DECIMAL_RE.match(line.cost_usd):
                    raise IntegrityError(f"cost at seq {line.seq} is not decimal text")
            case ToolCall():
                if line.tool_call_id in calls:
                    raise IntegrityError(f"tool call {line.tool_call_id!r} spooled twice")
                args = _load_json(spool.objects[line.input], f"input of {line.tool_call_id}")
                if not isinstance(args, dict):
                    raise IntegrityError(f"input of {line.tool_call_id} is not an object")
                fused = _fused_steps(line, args, tokens, fusion)
                calls[line.tool_call_id] = _Call(line, args, fused)
            case ToolResult():
                call = call_for(line)
                if call.visible is not None or (line.stage == "raw" and call.raw is not None):
                    raise IntegrityError(f"duplicate {line.stage} result at seq {line.seq}")
                if line.stage == "raw":
                    call.raw = line
                    continue
                call.visible = line
                if call.line.decision != "allowed":
                    continue
                content = _load_json(spool.objects[line.content], f"result at seq {line.seq}")
                found = _observation_id(line, content)
                if found and found[0] in archive.objects:
                    if archive.objects[found[0]] != found[1]:
                        raise IntegrityError(f"archived {found[0]} differs from its tool result")
                    observation_of[line.seq] = found[0]
            case TaskAcquire():
                call_for(line, TOOL_ORCA_RUN)
                pinned = task.inputs.get("output")
                if pinned is not None and pinned != f"sha256:{line.output}":
                    raise IntegrityError(f"acquired output at seq {line.seq} is not the pinned one")
                if task.target.get("case_id", line.case_id) != line.case_id:
                    raise IntegrityError(f"acquisition at seq {line.seq} is for another case")
            case TaskExecute():
                call_for(line, TOOL_ORCA_SUBMIT)
                if not JOB_ID_RE.match(line.job_id):
                    raise IntegrityError(f"seq {line.seq} names job {line.job_id!r}, not a job id")
                if line.job_id in executions:
                    raise IntegrityError(f"job {line.job_id} spooled twice (seq {line.seq})")
                if any(e.tool_call_id == line.tool_call_id for e in executions.values()):
                    raise IntegrityError(f"second task.execute for {line.tool_call_id}")
                try:
                    spool.objects[line.input].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise IntegrityError(f"submitted input at seq {line.seq} is not UTF-8") from exc
                executions[line.job_id] = line
            case TaskClaim():
                tool = call_for(line).line.tool_name
                model = CLAIM_TOOLS.get(tool)
                if model is None:
                    raise IntegrityError(f"seq {line.seq} is not from an allowed report call")
                call_for(line, tool)
                try:
                    model.model_validate_json(spool.objects[line.claim])
                except ValidationError as exc:
                    raise IntegrityError(
                        f"claim at seq {line.seq} is not a valid {model.__name__}"
                    ) from exc
            case RlmCell() | RlmChildStart() | RlmChildEnd() | RlmChildRefused() | RlmState():
                if not variant.rlm:
                    raise IntegrityError(f"{line.type} at seq {line.seq} in non-RLM {variant.id}")
                _check_rlm_line(line, spool, call_for, cells, child_starts, child_ends, depth)
    unanswered = sorted((r for rs in pending.values() for r in rs), key=lambda r: r.seq)
    return _Analysis(
        calls,
        request_of,
        unanswered,
        observation_of,
        child_starts,
        child_ends,
        list(executions.values()),
    )


def _bare(sha: str | None) -> str | None:
    """A journal's `sha256:<hex>` as the spool's bare hex."""
    return sha.removeprefix(DIGEST_PREFIX) if sha is not None else None


def _broker_jobs(journal: Iterable[BrokerJournalRecord]) -> dict[str, BrokerJournalRecord]:
    """The journal's answered submissions by job id; a job answered twice is refused."""
    jobs: dict[str, BrokerJournalRecord] = {}
    for entry in journal:
        job_id = getattr(entry, "job_id", None)
        if job_id is None or getattr(entry, "status", None) not in ("completed", "failed"):
            continue
        if job_id in jobs:
            raise IntegrityError(f"the broker journal answers job {job_id} twice")
        jobs[job_id] = entry
    return jobs


def _check_broker(
    executions: list[TaskExecute], jobs: dict[str, BrokerJournalRecord]
) -> dict[str, BrokerJournalRecord]:
    """Each `task.execute` against the broker's journal (A59); returns the entries cited."""
    cited = {}
    for line in executions:
        entry = jobs.get(line.job_id)
        if entry is None:
            raise IntegrityError(
                f"task.execute at seq {line.seq} cites job {line.job_id}, which the broker "
                "never ran or served; nothing was ingested"
            )
        spooled = (line.status, line.input, line.output, line.cache_hit, line.executed_here)
        journaled = (
            entry.status,
            _bare(entry.input_sha256),
            _bare(entry.output_sha256),
            entry.cache_hit,
            entry.executed_here,
        )
        fields = ("status", "input", "output", "cache_hit", "executed_here")
        if differ := [f for f, s, j in zip(fields, spooled, journaled, strict=True) if s != j]:
            raise IntegrityError(
                f"task.execute at seq {line.seq} (job {line.job_id}) disagrees with the broker "
                f"journal on {differ}; nothing was ingested"
            )
        cited[line.job_id] = entry
    return cited


def _check_rlm_line(
    line: RlmCell | RlmChildStart | RlmChildEnd | RlmChildRefused | RlmState,
    spool: _Spool,
    call_for: Callable[..., _Call],
    cells: set[str],
    child_starts: dict[int, RlmChildStart],
    child_ends: dict[int, RlmChildEnd],
    depth: int,
) -> None:
    """One RLM line against its `ipython` call and the child sequence (M4 contract)."""
    if not isinstance(line, RlmChildEnd):
        call_for(line, TOOL_IPYTHON)
    match line:
        case RlmCell():
            if line.tool_call_id in cells:
                raise IntegrityError(f"second rlm.cell for {line.tool_call_id} at seq {line.seq}")
            cells.add(line.tool_call_id)
            try:
                spool.objects[line.code].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IntegrityError(f"cell code at seq {line.seq} is not UTF-8") from exc
            _load_json(spool.objects[line.output], f"cell output at seq {line.seq}")
        case RlmChildStart():
            if line.child_index != len(child_starts):
                raise IntegrityError(f"child {line.child_index} at seq {line.seq} is out of order")
            if line.spool_subdir != f"{CHILDREN_DIR}/{line.child_index:04d}":
                raise IntegrityError(f"child at seq {line.seq} names {line.spool_subdir!r}")
            if line.depth != depth + 1:
                raise IntegrityError(f"child at seq {line.seq} has depth {line.depth}")
            child_starts[line.child_index] = line
        case RlmChildEnd():
            if line.child_index not in child_starts or line.child_index in child_ends:
                raise IntegrityError(f"rlm.child.end at seq {line.seq} ends no running child")
            if line.cost_usd is not None and not DECIMAL_RE.match(line.cost_usd):
                raise IntegrityError(f"child cost at seq {line.seq} is not decimal text")
            child_ends[line.child_index] = line
        case RlmState():
            data = spool.objects[line.value]
            value = _load_json(data, f"state {line.name!r} at seq {line.seq}")
            try:
                canonical = canonical_state_json(value).encode()
            except ValueError as exc:  # e.g. 1e999 parses to inf
                raise IntegrityError(f"state {line.name!r} at seq {line.seq}: {exc}") from exc
            if canonical != data:
                raise IntegrityError(f"state {line.name!r} at seq {line.seq} is not canonical")


# ------------------------------------------------------------------ writing


def _step_key(event: Event) -> str | None:
    """Every event an ingest writes carries its spool-derived step key."""
    if "step_key" in event.payload:
        return event.payload["step_key"]
    trigger = event.payload.get("trigger_inputs")
    return trigger.get("step_key") if isinstance(trigger, dict) else None


class _Writer:
    """Commit-before-visible writes for one run, each skipped if its step key is journaled."""

    def __init__(
        self,
        store: Store,
        run: Run,
        task: TaskManifest,
        spool: _Spool,
        archive: _Archive,
        analysis: _Analysis,
        sol_pi_config: dict[str, Any] | None,
        children: dict[int, _Child] | None = None,
        ingest_child: Callable[[_Plan], None] | None = None,
        broker: dict[str, BrokerJournalRecord] | None = None,
        unspooled: tuple[str, ...] = (),
    ):
        self.store, self.run, self.task = store, run, task
        self.spool, self.archive, self.a = spool, archive, analysis
        self.sol_pi_config = sol_pi_config
        self.children = children or {}
        self.ingest_child = ingest_child
        self.broker = broker or {}
        self.unspooled = unspooled
        self.scripted = spool.start.scripted
        self.synthetic = ["synthetic"] if self.scripted else []
        self.runtime = {"pi": spool.start.pi_version, "sol_pi": spool.start.sol_pi_commit}
        existing = store.events(run.run_id)
        self.done: dict[str, Event] = {k: e for e in existing if (k := _step_key(e))}
        self.n_invocations = sum(e.type == "invocation.committed" for e in existing)
        self.frontier: list[str] = []
        # The case manifest is provenance for receipts, never part of the bundle.
        self.visible = {f"task.{k}": v for k, v in sorted(task.inputs.items()) if k != "case"}
        self.obs: dict[str, str] = {}
        self.compactions: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.requests = 0
        self.claims = 0
        self.decision_by_index: dict[int, str] = {}
        self.raw_content: dict[str, str] = {}
        self.raw_details: dict[str, str] = {}
        self.rlm_state: dict[str, str] = {}  # name -> digest of its latest committed value
        self.cells: dict[str, tuple[RlmCell, str, str]] = {}  # tool_call_id -> (line, code, out)

    # ---- primitives

    def emit(self, key: str, type: str, *, payload=None, refs=None, causes=()) -> Event:
        if key in self.done:
            return self.done[key]
        event = self.store.append_event(
            self.run.run_id,
            actor=ACTOR,
            type=type,
            payload={**(payload or {}), "step_key": key},
            payload_refs=refs,
            causes=causes,
        )
        self.done[key] = event
        return event

    def obj(
        self, hexdigest: str, media_type: str, labels: list[str], *, encoding: str | None = None
    ) -> str:
        data = self.spool.objects[hexdigest]
        return self.store.put_bytes(
            data, media_type=media_type, encoding=encoding, labels=labels
        ).digest

    def invocation(
        self,
        key: str,
        spec: StepSpec,
        kind: str,
        inputs: dict[str, str],
        outputs: dict[str, str],
        *,
        status: str = "succeeded",
        parents: list[str] | None = None,
        metadata: dict[str, Any],
        labels: list[str],
        usage: Usage,
    ) -> str:
        if key in self.done:
            inv_id = self.done[key].payload["invocation_id"]
        else:
            spec_id = self.store.put_record(spec)
            self.n_invocations += 1
            inv_id = f"{self.run.run_id}.i{self.n_invocations:03d}"
            if self.store.has_record("invocation", inv_id):
                # Committed just before a crash, not yet journaled: journal it as is.
                if self.store.receipt(inv_id).spec_digest != spec_id:
                    raise OnodaError(f"orphan receipt {inv_id} belongs to another step")
            else:
                now = self.store.clock.now()
                self.store.put_record(
                    InvocationReceipt.new(
                        invocation_id=inv_id,
                        run_id=self.run.run_id,
                        spec_digest=spec_id,
                        invocation_kind=kind,
                        inputs=inputs,
                        outputs=outputs,
                        parents=parents or [],
                        status=status,
                        metadata=metadata,
                        usage=usage,
                        started_at=now,
                        finished_at=now,
                        labels=labels,
                    )
                )
            self.emit(
                key,
                "invocation.committed",
                payload={"invocation_id": inv_id},
                refs={"receipt": record_ref("invocation", inv_id)},
            )
        self.frontier.append(inv_id)
        return inv_id

    def checkpoint(self, key: str, boundary: str, phase: str) -> None:
        if key in self.done:
            return
        events = self.store.events(self.run.run_id)
        context: dict[str, Any] = {
            CTX_VARIANT: self.spool.start.variant,
            CTX_MECHANISMS: self.sol_pi_config,
            CTX_RUNTIME: self.runtime,
            CTX_OBSERVATION_PACK: dict(self.obs),
            CTX_COMPACTIONS: list(self.compactions),
            CTX_REQUESTS: self.requests,
        }
        if self.rlm_state:  # absent before any commit_state, so M3 checkpoints are unchanged
            context[CTX_RLM_STATE] = dict(self.rlm_state)
        cp = Checkpoint.new(
            run_id=self.run.run_id,
            boundary=boundary,
            event_seq=events[-1].seq if events else 0,
            frontier=list(self.frontier),
            visible=dict(self.visible),
            phase=phase,
            policy={"mode": self.run.config["mode"], "variant": self.run.config["variant"]},
            context=context,
            jobs=list(self.jobs),
            quiescent=True,
            capabilities=dict(PI_CAPABILITIES),
        )
        digest = self.store.put_record(cp)
        self.emit(
            key,
            "checkpoint",
            payload={"boundary": boundary, "phase": phase},
            refs={"checkpoint": record_ref("checkpoint", digest)},
        )

    def tool_spec(self, name: str, inputs: dict[str, str]) -> StepSpec:
        return StepSpec.new(
            operation=f"pi.tool:{name}",
            inputs=inputs,
            code_ref=f"{PI_PACKAGE}@{self.runtime['pi']}",
            environment={k: v or "" for k, v in self.runtime.items()},
            determinism="external",
        )

    # ---- spool lines

    def ingest(self) -> None:
        spool_desc = self.store.put_bytes(
            self.spool.events, media_type="application/x-ndjson", labels=["pi-spool"]
        )
        self.emit(
            "run.created",
            "run.created",
            refs={
                "run": record_ref("run", self.run.run_id),
                "task": record_ref("task", self.task.task_id),
                "spool": spool_desc.digest,
            },
        )
        self.checkpoint("checkpoint:initialized", "initialized", "agent")
        handlers = {
            ProviderRequest: self.on_request,
            ProviderResponse: self.on_response,
            ToolCall: self.on_call,
            ToolResult: self.on_result,
            TaskAcquire: self.on_acquire,
            TaskExecute: self.on_execute,
            TaskClaim: self.on_claim,
            Compaction: self.on_compaction,
            ReplayMismatchLine: self.on_mismatch,
            RlmCell: self.on_cell,
            RlmChildEnd: self.on_child_end,
            RlmChildRefused: self.on_child_refused,
            RlmState: self.on_state,
        }
        for line in self.spool.lines[1:]:
            if handler := handlers.get(type(line)):
                handler(line)
        self.flush()
        if self.unspooled:
            self.emit(
                "broker-unspooled", "pi.broker-unspooled", payload={"jobs": list(self.unspooled)}
            )
        end = self.spool.end
        if end is not None:
            self.emit(
                "spool.end",
                "pi.spool-end",
                payload={"status": end.status, "requests": end.requests, "reason": end.reason},
            )
            if self.claims:
                self.visible["final"] = self.visible[f"claim.{self.claims}"]
            self.checkpoint("checkpoint:finalized", "finalized", end.status)
        self.emit(
            "ingested",
            "pi.ingested",
            payload={"complete": end is not None, "torn_tail": self.spool.torn_tail},
            refs={"spool": spool_desc.digest},
        )

    def on_request(self, line: ProviderRequest) -> None:
        self.requests += 1
        self.obj(line.context, "application/json", ["pi-context", *self.synthetic])

    def decision(self, req: ProviderRequest, resp: ProviderResponse | None) -> None:
        reducer = req.purpose == "reducer"
        provider = self.spool.start.provider
        if reducer:
            provider = self.run.config.get("reducer_provider") or (
                SCRIPTED_PROVIDER if self.scripted else "unknown"
            )
        role = (
            "reducer" if reducer else "compaction-summary" if req.purpose == "summary" else "agent"
        )
        ctx = self.obj(req.context, "application/json", ["pi-context", *self.synthetic])
        outputs, metadata = (
            {},
            {
                "role": role,
                "provider": provider,
                "model": req.model,
                "request_index": req.request_index,
                "source": req.source,
                "replay_key": req.replay_key,
                "spool_seq": req.seq,
            },
        )
        usage = Usage(units={}, measured=False, simulated=self.scripted, cost_usd=None)
        if resp is not None:
            outputs["message"] = self.obj(
                resp.message, "application/json", ["pi-message", *self.synthetic]
            )
            metadata["stop_reason"] = resp.stop_reason
            usage = Usage(
                units=dict(resp.usage_tokens),
                measured=not self.scripted,
                simulated=self.scripted,
                cost_usd=resp.cost_usd,
                price_table="pi-computed" if resp.cost_usd is not None else None,
            )
        spec = StepSpec.new(
            operation="pi.provider-request",
            inputs={"context": ctx},
            config={"provider": provider, "model": req.model, "role": role},
            code_ref=f"{PI_PACKAGE}@{self.runtime['pi']}",
            determinism="nondeterministic",
        )
        inv_id = self.invocation(
            f"request:{req.seq}",
            spec,
            "decision",
            {"context": ctx},
            outputs,
            status="succeeded" if resp is not None else "failed",
            metadata=metadata,
            labels=[role, *self.synthetic],
            usage=usage,
        )
        if role == "agent":
            self.decision_by_index[req.request_index] = inv_id

    def on_response(self, line: ProviderResponse) -> None:
        self.decision(self.a.request_of[line.seq], line)

    def on_call(self, line: ToolCall) -> None:
        call = self.a.calls[line.tool_call_id]
        inp = self.obj(line.input, "application/json", ["pi-tool-input", *self.synthetic])
        base = {"tool_call_id": line.tool_call_id, "tool_name": line.tool_name}
        if line.decision == "blocked":
            self.emit(
                f"tool:{line.seq}",
                "pi.tool-blocked",
                payload={**base, "reason": line.reason, "fused": call.fused is not None},
                refs={"input": inp},
            )
        elif call.fused is not None:
            self.fuse(line, call.fused)
        elif line.tool_name == OBS_RECALL:
            # Arguments are the model's, unvalidated by Pi at this point: keep only well-typed ones.
            obs_id = call.args.get("id")
            obs_id = obs_id if isinstance(obs_id, str) else None
            offset = call.args.get("offset", 0)
            offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else None
            matched = set(self.a.observation_of.values())
            handle = self.archived(obs_id) if obs_id in matched else None
            self.emit(
                f"page:{line.seq}",
                "context.page",
                payload={**base, "obs_id": obs_id, "offset": offset, "handle": handle},
                refs={"observation": handle} if handle else None,
            )

    def fuse(self, line: ToolCall, steps: list[dict[str, Any]]) -> None:
        keyed = [{**s, "step_key": f"fuse:{line.seq}:{i}"} for i, s in enumerate(steps)]
        recorded = [s["step_key"] in self.done for s in keyed]
        if all(recorded):
            return
        if not any(recorded):
            for event in decompose_fused_call(self.store, self.run.run_id, keyed):
                self.done[event.payload["step_key"]] = event
            return
        # Interrupted mid-decomposition (already validated): finish it as
        # decompose_fused_call would have, under the recorded fusion id.
        prev = self.done[keyed[0]["step_key"]]
        for index, step in enumerate(keyed):
            if step["step_key"] in self.done:
                prev = self.done[step["step_key"]]
                continue
            prev = self.store.append_event(
                self.run.run_id,
                actor="context.fusion",
                type="context.fused-step",
                payload={**step, "fusion_id": prev.payload["fusion_id"], "step_index": index},
                causes=[prev.event_id],
            )
            self.done[step["step_key"]] = prev

    def on_result(self, line: ToolResult) -> None:
        content = self.obj(line.content, "application/json", ["pi-tool-result"])
        details = (
            self.obj(line.details, "application/json", ["pi-tool-details"])
            if line.details
            else None
        )
        call = self.a.calls[line.tool_call_id]
        if line.stage == "raw":
            self.raw_content[line.tool_call_id] = content
            if details:
                self.raw_details[line.tool_call_id] = details
            return
        if call.line.decision == "blocked":
            self.emit(
                f"result:{line.seq}",
                "pi.tool-blocked-result",
                payload={"tool_call_id": line.tool_call_id, "is_error": line.is_error},
                refs={"content": content},
            )
            return
        raw = self.raw_content.get(line.tool_call_id)
        reduced = raw is not None and raw != content
        inv_id = self.tool_receipt(call, content, details, raw if reduced else None, line.is_error)
        if reduced:
            self.emit(
                f"reduced:{line.seq}",
                "context.reduced",
                payload={
                    "tool_call_id": line.tool_call_id,
                    "tool_name": line.tool_name,
                    "invocation_id": inv_id,
                },
                refs={"raw": raw, "visible": content},
            )
        if obs_id := self.a.observation_of.get(line.seq):
            self.archived(obs_id, tool_call_id=line.tool_call_id)

    def tool_receipt(
        self, call: _Call, content: str | None, details: str | None, raw: str | None, error: bool
    ) -> str:
        line = call.line
        inp = self.obj(line.input, "application/json", ["pi-tool-input", *self.synthetic])
        inputs, outputs = {"input": inp}, {}
        metadata: dict[str, Any] = {}
        if cell := self.cells.get(line.tool_call_id):
            inputs["code"], outputs["cell"] = cell[1], cell[2]
            metadata["cell"] = {
                "cell_index": cell[0].cell_index,
                "is_error": cell[0].is_error,
                "truncated": cell[0].truncated,
                "spool_seq": cell[0].seq,
            }
        if content:
            outputs["result"] = content
        if raw:
            outputs["raw"] = raw
        if details := details or self.raw_details.get(line.tool_call_id):
            outputs["details"] = details
        # No visible result: the tool may or may not have run, as for an unreconciled job.
        status = "submission_uncertain" if content is None else "failed" if error else "succeeded"
        parent = self.decision_by_index.get(line.request_index)
        return self.invocation(
            f"tool:{line.seq}",
            self.tool_spec(line.tool_name, inputs),
            "read" if line.tool_name in READ_TOOLS else "execution",
            inputs,
            outputs,
            status=status,
            parents=[parent] if parent else [],
            metadata={
                "tool_call_id": line.tool_call_id,
                "tool_name": line.tool_name,
                "request_index": line.request_index,
                "fused": call.fused is not None,
                "result": "recorded" if content else "missing",
                **metadata,
            },
            labels=list(self.synthetic),
            usage=Usage(units={}, measured=False, simulated=self.scripted, cost_usd=None),
        )

    def archived(self, obs_id: str, *, tool_call_id: str | None = None) -> str:
        if obs_id in self.obs:
            return self.obs[obs_id]
        data = self.archive.objects[obs_id]
        digest = self.store.put_bytes(
            data, media_type="text/plain", encoding="utf-8", labels=["observation"]
        ).digest
        self.obs[obs_id] = digest
        self.emit(
            f"obs:{obs_id}",
            "context.observation-archived",
            payload={
                "obs_id": obs_id,
                "size": len(data),
                "tool_call_id": tool_call_id,
                "ledger": self.archive.stats.get(obs_id, {}),
            },
            refs={"observation": digest},
        )
        return digest

    def on_acquire(self, line: TaskAcquire) -> None:
        call = self.a.calls[line.tool_call_id]
        output = self.store.put_bytes(
            self.spool.objects[line.output],
            media_type="text/plain",
            source={"case_id": line.case_id},
            labels=["job-output", "acquired"],
        ).digest
        tool_input = self.obj(
            call.line.input, "application/json", ["pi-tool-input", *self.synthetic]
        )
        inputs = {"input": self.task.inputs.get("input", tool_input)}
        metadata: dict[str, Any] = {
            "executed_here": False,
            "case_id": line.case_id,
            "tool_call_id": line.tool_call_id,
            "pinned": "output" in self.task.inputs,
        }
        if provenance := self._case_provenance():
            metadata["provenance"] = provenance
        spec = StepSpec.new(
            operation=ACQUIRE,
            inputs=inputs,
            config={"case_id": line.case_id},
            code_ref="onoda.adapters.pi:orca_run",
            determinism="external",
        )
        inv_id = self.invocation(
            f"acquire:{line.seq}",
            spec,
            "read",
            inputs,
            {"log": output},
            metadata=metadata,
            labels=["acquired"],
            usage=Usage(units={}, measured=False, simulated=False, cost_usd=None),
        )
        self.jobs.append({"tool_call_id": line.tool_call_id, "invocation_id": inv_id})
        self.visible[f"acquired.{len(self.jobs)}"] = output
        self.checkpoint(f"checkpoint:job-collected:{line.seq}", "job-collected", "agent")

    def on_execute(self, line: TaskExecute) -> None:
        """A broker job: executed here, or an earlier job's bytes served from the cache."""
        stand_in = bool(getattr(self.broker[line.job_id], "stand_in", False))
        if line.cache_hit:
            job_labels = ["acquired", "cache-hit"]
        else:
            job_labels = [] if stand_in else list(_JOB_LABELS)
        if stand_in:  # a stand-in's observations are synthetic, executed or cached
            job_labels += _STAND_IN_LABELS
        inp = self.obj(line.input, "text/plain", ["orca-input", *self.synthetic], encoding="utf-8")
        output = self.obj(line.output, "text/plain", ["job-output", *job_labels])
        spec = StepSpec.new(
            operation=ACQUIRE_CACHED if line.cache_hit else EXECUTE,
            inputs={"input": inp},
            code_ref=BROKER_CODE_REF,
            determinism="external",
        )
        inv_id = self.invocation(
            f"execute:{line.seq}",
            spec,
            "read" if line.cache_hit else "execution",
            {"input": inp},
            {"log": output},
            status="succeeded" if line.status == "completed" else "failed",
            # `run._vte_jobs` reads job_id, executed_here and cache_hit back for grading.
            metadata={
                "executed_here": line.executed_here,
                "cache_hit": line.cache_hit,
                "job_id": line.job_id,
                "tool_call_id": line.tool_call_id,
                "stand_in": stand_in,
                "broker_journal": "agrees",
            },
            labels=list(dict.fromkeys([*job_labels, *self.synthetic])),
            usage=Usage(units={}, measured=False, simulated=self.scripted, cost_usd=None),
        )
        self.jobs.append(
            {"tool_call_id": line.tool_call_id, "invocation_id": inv_id, "job_id": line.job_id}
        )
        self.visible[f"job.{line.job_id}"] = output
        self.visible[f"job.{line.job_id}.input"] = inp
        self.checkpoint(f"checkpoint:job-collected:{line.seq}", "job-collected", "agent")

    def _case_provenance(self) -> dict[str, Any] | None:
        """Provenance from the pinned M2 case manifest, when the task carries one."""
        case = self.task.inputs.get("case")
        if case is None or not self.store.has_object(case):
            return None
        manifest = self.store.get_json(case)
        prov = manifest.get("provenance") if isinstance(manifest, dict) else None
        return prov if isinstance(prov, dict) else None

    def on_claim(self, line: TaskClaim) -> None:
        vte = self.a.calls[line.tool_call_id].line.tool_name == TOOL_REPORT_VTE
        label = "pi-vte-claim" if vte else "pi-claim"
        claim = self.obj(line.claim, "application/json", [label, *self.synthetic])
        self.claims += 1
        self.visible[f"claim.{self.claims}"] = claim
        payload: dict[str, Any] = {"tool_call_id": line.tool_call_id}
        if vte:  # M3 claim events stay as they were
            payload["schema"] = VTE_CLAIM_SCHEMA
        self.emit(f"claim:{line.seq}", "pi.claim", payload=payload, refs={"claim": claim})

    def on_compaction(self, line: Compaction) -> None:
        details = (
            self.obj(line.details, "application/json", ["pi-compaction"]) if line.details else None
        )
        self.compactions.append({"seq": line.seq, "phase": line.phase, "details": details})
        key = f"compaction:{line.seq}"
        if line.phase != "done":
            self.emit(
                key,
                "pi.compaction",
                payload={"phase": line.phase},
                refs={"details": details} if details else None,
            )
            return
        if key in self.done:
            return
        summary, trigger = b"", {"step_key": key, "spool_seq": line.seq, "details": details}
        if details:
            data = self.spool.objects[line.details]
            entry = _load_json(data, f"compaction details at seq {line.seq}")
            if isinstance(entry, dict) and isinstance(entry.get("summary"), str):
                summary = entry["summary"].encode()
                if isinstance(entry.get("tokensBefore"), int):
                    trigger["tokens_before"] = entry["tokensBefore"]
            else:
                summary = data
        self.done[key] = record_compaction(
            self.store,
            self.run.run_id,
            trigger_inputs=trigger,
            summary=summary,
            replaced_refs=[],
            boundary=None,
        )

    def on_mismatch(self, line: ReplayMismatchLine) -> None:
        self.emit(
            f"mismatch:{line.seq}",
            "pi.replay-mismatch",
            payload={
                "request_index": line.request_index,
                "expected": line.expected,
                "actual": line.actual,
            },
        )

    # ---- RLM (M4)

    def on_cell(self, line: RlmCell) -> None:
        code = self.obj(
            line.code, "text/x-python", ["rlm-cell-code", *self.synthetic], encoding="utf-8"
        )
        output = self.obj(line.output, "application/json", ["rlm-cell-output", *self.synthetic])
        self.cells[line.tool_call_id] = (line, code, output)

    def on_state(self, line: RlmState) -> None:
        value = self.obj(line.value, "application/json", ["rlm-state", *self.synthetic])
        self.rlm_state[line.name] = value
        self.emit(
            f"rlm-state:{line.seq}",
            "rlm.state",
            payload={"name": line.name, "tool_call_id": line.tool_call_id},
            refs={"value": value},
        )

    def on_child_refused(self, line: RlmChildRefused) -> None:
        self.emit(
            f"rlm-refused:{line.seq}",
            "rlm.child-refused",
            payload={
                "tool_call_id": line.tool_call_id,
                "reason": line.reason,
                "detail": line.detail,
            },
        )

    def on_child_end(self, line: RlmChildEnd) -> None:
        self.child(self.children[line.child_index])

    def child(self, child: _Child) -> None:
        """The child's run first, then the `rlm.child` event citing it (commit before visible)."""
        key = f"rlm-child:{child.start.child_index}"
        if key in self.done:
            return
        if child.plan is not None:
            assert self.ingest_child is not None
            self.ingest_child(child.plan)
        finalized = _finalized(self.store, child.run_id) if child.plan is not None else None
        if child.end is None:
            reason = "the parent recorded no rlm.child.end"
        elif child.plan is None:
            reason = "the child wrote no spool"
        elif child.plan.spool.end is None or finalized is None:
            reason = "the child's spool has no spool.end"
        else:
            reason = None
        prompt = self.obj(
            child.start.prompt,
            "text/plain",
            ["rlm-child-prompt", *self.synthetic],
            encoding="utf-8",
        )
        refs = {"prompt": prompt}
        if child.plan is not None:
            refs["run"] = record_ref("run", child.run_id)
        if reason is None:
            assert finalized is not None
            refs["checkpoint"] = record_ref("checkpoint", finalized)
        end = child.end
        if end is not None and end.answer is not None:
            refs["answer"] = self.obj(
                end.answer, "text/plain", ["rlm-child-answer", *self.synthetic], encoding="utf-8"
            )
        self.emit(
            key,
            "rlm.child",
            payload={
                "child_index": child.start.child_index,
                "child_run": child.run_id if child.plan is not None else None,
                "tool_call_id": child.start.tool_call_id,
                "depth": child.start.depth,
                "max_requests": child.start.max_requests,
                "complete": reason is None,
                "incomplete_reason": reason,
                "finalized": finalized if reason is None else None,
                "exit_code": end.exit_code if end else None,
                "end_status": end.end_status if end else None,
                "requests": end.requests if end else None,
                "cost_usd": end.cost_usd if end else None,
                "error": end.error if end else None,
            },
            refs=refs,
        )

    def flush(self) -> None:
        """Receipts for what the spool left unanswered, then archives no result cited."""
        for index in sorted(self.children):
            if self.children[index].end is None:  # the parent stopped while it ran
                self.child(self.children[index])
        for req in self.a.unanswered:
            self.decision(req, None)
        for call in self.a.calls.values():
            if call.line.decision == "allowed" and call.visible is None:
                raw = self.raw_content.get(call.line.tool_call_id)
                self.tool_receipt(call, None, None, raw, True)
        # Only archives a recorded tool result vouches for; anything else in the
        # session dir could have been planted by the agent's own tools.
        for obs_id in sorted(set(self.a.observation_of.values())):
            self.archived(obs_id)
        if self.archive.ledger is not None:
            ledger = self.store.put_bytes(
                self.archive.ledger,
                media_type="application/x-ndjson",
                labels=["observation-pack-ledger"],
            ).digest
            self.emit("obs-ledger", "pi.observation-ledger", refs={"ledger": ledger})


# ------------------------------------------------------------------- public


def _run_config(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("mode") not in RUN_MODES:
        raise OnodaError(f"config mode must be one of {RUN_MODES}, not {config.get('mode')!r}")
    if not isinstance(config.get("variant"), str):
        raise OnodaError("config must name the variant")
    if "spool" in config:
        raise OnodaError("config key 'spool' is set by ingest")
    return json.loads(canonical_bytes(config))  # rejects floats; normalizes tuples


def _effective_variant(config: dict[str, Any]) -> Variant:
    """The run's variant, with a candidate's `sol_pi.config` mechanisms if it set one
    (as `runtime.build_launch` launched it)."""
    return with_mechanisms(VARIANTS[config["variant"]], config.get("sol_pi_config_override"))


def _sol_pi_config(config: dict[str, Any]) -> dict[str, Any] | None:
    variant = _effective_variant(config)
    scripted = config["mode"] == "pi-scripted"
    try:
        cfg = variant.sol_pi_config(
            reducer_provider=config.get("reducer_provider")
            or (SCRIPTED_PROVIDER if scripted else None),
            reducer_model=config.get("reducer_model")
            or (SCRIPTED_REDUCER_MODEL if scripted else None),
        )
    except ValueError as exc:
        raise OnodaError(str(exc)) from exc
    return _decimalize(cfg)


def _charge(spool: _Spool) -> tuple[int, Decimal | None]:
    """What a child spool charges its parent: its requests and summed cost (its own
    responses plus its children's `rlm.child.end` lines), as the extension computes it."""
    requests, costs, known = 0, [], True
    for line in spool.lines:
        if isinstance(line, ProviderRequest):
            requests += 1
        elif isinstance(line, (ProviderResponse, RlmChildEnd)):
            if isinstance(line, RlmChildEnd):
                requests += line.requests
            if line.cost_usd is None:
                known = False
            else:
                costs.append(Decimal(line.cost_usd))
    return requests, sum(costs, Decimal(0)) if known else None


@dataclass(frozen=True)
class _Plan:
    """One spool read and validated, with its children's plans: nothing written yet."""

    run_id: str
    cfg: dict[str, Any]
    spool: _Spool
    run: Run | None  # the run record if an earlier ingest wrote it
    noop: bool  # the run was already fully ingested
    archive: _Archive = field(default_factory=_Archive)
    analysis: _Analysis | None = None
    sol_pi_config: dict[str, Any] | None = None
    children: dict[int, _Child] = field(default_factory=dict)
    broker: dict[str, BrokerJournalRecord] = field(default_factory=dict)  # job id -> entry cited


@dataclass(frozen=True)
class _Child:
    start: RlmChildStart
    end: RlmChildEnd | None
    run_id: str
    plan: _Plan | None  # None: the child wrote no spool


def _finalized(store: Store, run_id: str) -> str | None:
    """Digest of a run's finalized checkpoint, if it has one."""
    found = [d for _, d, cp in store.checkpoints(run_id) if cp.boundary == "finalized"]
    return found[-1] if found else None


def _child_spool(spool_dir: Path, start: RlmChildStart) -> Path | None:
    """The child's spool dir, or None when the child never wrote a spool line."""
    path = spool_dir / start.spool_subdir
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_dir():
        raise IntegrityError(f"child spool {start.spool_subdir} is not a directory")
    events = path / SPOOL_EVENTS
    if not events.exists() and not events.is_symlink():
        return None
    return None if _read_file(events, "child spool").strip() == b"" else path


def _prepare(
    store: Store,
    spool_dir: Path,
    *,
    run_id: str,
    task: TaskManifest,
    cfg: dict[str, Any],
    key: bytes | None,
    expected_session_dir: Path | None,
    broker_jobs: dict[str, BrokerJournalRecord] | None = None,
) -> _Plan:
    """Read and check one spool and, recursively, its children's; raises before any write."""
    check_safe_id(run_id, "run id")
    spool = _read_spool(spool_dir, key)
    run = None
    if store.has_record("run", run_id):
        run = store.run_record(run_id)
        if (
            run.config.get("spool") != spool.digest
            or run.task != task.task_id
            or {k: v for k, v in run.config.items() if k != "spool"} != cfg
        ):
            raise OnodaError(f"run {run_id} was ingested from a different spool or config")
        if any(e.type == "pi.ingested" for e in store.events(run_id)):
            return _Plan(run_id, cfg, spool, run, noop=True)
    archive = _read_archive(spool.start, expected_session_dir)
    analysis = _analyze(spool, archive, task, cfg)
    broker: dict[str, BrokerJournalRecord] = {}
    if analysis.executions:
        if broker_jobs is None:
            raise OnodaError(
                f"spool {spool_dir.name} records task.execute lines; ingest needs the broker "
                "journal to check them"
            )
        broker = _check_broker(analysis.executions, broker_jobs)
    sol_pi_config = _sol_pi_config(cfg)
    named = {start.spool_subdir for start in analysis.child_starts.values()}
    children_dir = spool_dir / CHILDREN_DIR
    if children_dir.is_symlink() or (children_dir.exists() and not children_dir.is_dir()):
        raise IntegrityError(f"{CHILDREN_DIR} in spool {spool_dir.name} is not a directory")
    if children_dir.is_dir():
        for path in sorted(children_dir.iterdir()):
            if f"{CHILDREN_DIR}/{path.name}" not in named:
                raise IntegrityError(f"{CHILDREN_DIR}/{path.name} is named by no rlm.child.start")
    children: dict[int, _Child] = {}
    for index, start in sorted(analysis.child_starts.items()):
        end = analysis.child_ends.get(index)
        child_run = f"{run_id}.rlm-{index:04d}"
        child_dir = _child_spool(spool_dir, start)
        plan = None
        if child_dir is None:
            if end is not None and (end.end_status is not None or end.requests):
                raise IntegrityError(
                    f"child {index} ended {end.end_status} after {end.requests} requests "
                    "but its spool is missing"
                )
        else:
            child_cfg = {**cfg, "parent_run": run_id, "child_index": index, "depth": start.depth}
            plan = _prepare(
                store,
                child_dir,
                run_id=child_run,
                task=task,
                cfg=child_cfg,
                key=derive_child_key(key, index) if key is not None else None,
                expected_session_dir=expected_session_dir,
                broker_jobs=broker_jobs,
            )
            child_end = plan.spool.end
            if end is not None and end.end_status != (child_end.status if child_end else None):
                raise IntegrityError(
                    f"child {index}: the parent recorded end status {end.end_status!r}, "
                    f"its spool says {child_end.status if child_end else None!r}"
                )
            if end is not None:
                cost = None if end.cost_usd is None else Decimal(end.cost_usd)
                if _charge(plan.spool) != (end.requests, cost):
                    raise IntegrityError(
                        f"child {index}: the parent was charged {end.requests} requests and "
                        f"{end.cost_usd} USD, its spool says {_charge(plan.spool)}"
                    )
        children[index] = _Child(start, end, child_run, plan)
    return _Plan(run_id, cfg, spool, run, False, archive, analysis, sol_pi_config, children, broker)


def _cited_jobs(plan: _Plan) -> set[str]:
    """Broker job ids a spool tree's `task.execute` lines cite."""
    jobs = set(plan.broker)
    for child in plan.children.values():
        if child.plan is not None:
            jobs |= _cited_jobs(child.plan)
    return jobs


def _write(
    store: Store,
    plan: _Plan,
    task: TaskManifest,
    labels: tuple[str, ...],
    unspooled: tuple[str, ...] = (),
) -> None:
    """Ingest a prepared spool; children are written when the parent reaches them."""
    if plan.noop:
        return
    run = plan.run
    if run is None:
        cfg = plan.cfg
        scripted = cfg["mode"] == "pi-scripted"
        run_labels = [
            cfg["mode"],
            cfg["variant"],
            *(["synthetic"] if scripted else []),
            *(["rlm-child"] if "parent_run" in cfg else []),
            *labels,
        ]
        run = Run.new(
            run_id=plan.run_id,
            experiment_id=PI_EXPERIMENT,
            task=task.task_id,
            config={**cfg, "spool": plan.spool.digest},
            quota_id=f"{PI_EXPERIMENT}-unbudgeted",
            created_at=store.clock.now(),
            labels=list(dict.fromkeys(run_labels)),
        )
        store.put_record(run)
    assert plan.analysis is not None
    _Writer(
        store,
        run,
        task,
        plan.spool,
        plan.archive,
        plan.analysis,
        plan.sol_pi_config,
        plan.children,
        lambda child: _write(store, child, task, labels),
        plan.broker,
        unspooled,
    ).ingest()


def ingest_spool(
    store: Store,
    spool_dir: str | Path,
    *,
    run_id: str,
    task: TaskManifest,
    config: dict[str, Any],
    labels: tuple[str, ...] | list[str] = (),
    key: bytes | None = None,
    expected_session_dir: str | Path | None = None,
    broker_journal: Iterable[BrokerJournalRecord] | None = None,
) -> IngestResult:
    """Turn one Pi spool into a run record; see the module docstring for the mapping.

    `config` needs `mode` (one of `RUN_MODES`) and `variant`; optional
    `reducer_provider`/`reducer_model` name a live reducer, and `unfusable`
    adds then_run tokens to the contract's. The task's input objects must
    already be committed; the task record is written if absent. A task input
    named `output` pins the acquisition: any other acquired bytes are refused.
    `key` (the launch's spool HMAC key; never stored) makes every line's mac
    mandatory; `expected_session_dir` (the launch's sessions dir) bounds where
    ObservationPack archives are read from. RLM child spools under
    `children/` are read, checked and ingested as runs of their own; every
    spool in the tree is validated before anything is written.
    `broker_journal` (the host broker's journal, A59) is required when any
    spool in the tree records `task.execute`; every such line must agree with
    it, and broker jobs no line cites are recorded as unspooled.
    """
    check_safe_id(run_id, "run id")
    cfg = _run_config(config)
    if reserved := sorted(k for k in CHILD_CONFIG_KEYS if k in cfg):
        raise OnodaError(f"config keys {reserved} are set by ingest for RLM child runs")
    broker_jobs = _broker_jobs(broker_journal) if broker_journal is not None else None
    plan = _prepare(
        store,
        Path(spool_dir),
        run_id=run_id,
        task=task,
        cfg=cfg,
        key=key,
        expected_session_dir=(
            Path(expected_session_dir) if expected_session_dir is not None else None
        ),
        broker_jobs=broker_jobs,
    )
    if plan.noop:
        return _result(store, run_id, noop=True)
    for name, digest in task.inputs.items():
        if not store.has_object(digest):
            raise MissingObjectError(digest, f"task input {name!r} -> {digest} is not committed")
    store.put_record(task)
    unspooled = tuple(sorted(set(broker_jobs or ()) - _cited_jobs(plan)))
    _write(store, plan, task, tuple(labels), unspooled)
    return _result(store, run_id, noop=False)


def _result(store: Store, run_id: str, *, noop: bool) -> IngestResult:
    events = store.events(run_id)
    checkpoints = store.checkpoints(run_id)
    ends = [e for e in events if e.type == "pi.spool-end"]
    claim = [e for e in events if e.type == "pi.claim"]
    return IngestResult(
        run_id=run_id,
        spool=store.run_record(run_id).config["spool"],
        complete=any(cp.boundary == "finalized" for _, _, cp in checkpoints),
        status=ends[-1].payload["status"] if ends else None,
        noop=noop,
        invocations=tuple(
            e.payload["invocation_id"] for e in events if e.type == "invocation.committed"
        ),
        checkpoints=tuple(digest for _, digest, _ in checkpoints),
        claim=claim[-1].payload_refs["claim"] if claim else None,
        observation_pack={
            e.payload["obs_id"]: e.payload_refs["observation"]
            for e in events
            if e.type == "context.observation-archived"
        },
        children=tuple(child for _, child in child_links(store, run_id) if child is not None),
        rlm_state={
            e.payload["name"]: e.payload_refs["value"] for e in events if e.type == "rlm.state"
        },
    )


def run_claim(store: Store, run_id: str) -> Claim | None:
    """The agent's last claim in an ingested run, or None if it never reported."""
    claims = [e for e in store.events(run_id) if e.type == "pi.claim" and "schema" not in e.payload]
    if not claims:
        return None
    return Claim.model_validate_json(store.get_bytes(claims[-1].payload_refs["claim"]))


def run_vte_claim(store: Store, run_id: str) -> VteClaim | None:
    """The agent's last VTE claim in an ingested run, or None if it never reported one."""
    claims = [
        e
        for e in store.events(run_id)
        if e.type == "pi.claim" and e.payload.get("schema") == VTE_CLAIM_SCHEMA
    ]
    if not claims:
        return None
    return VteClaim.model_validate_json(store.get_bytes(claims[-1].payload_refs["claim"]))


def _turn(message: Any, replay_key: str | None, cost_usd: str | None) -> ScriptTurn:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks = []
    for block in content or []:
        if block.get("type") == "text":
            blocks.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "toolCall":
            blocks.append(
                {
                    "type": "toolCall",
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": block.get("arguments") or {},
                }
            )
        # thinking blocks are provider-internal and not part of a scripted turn
    turn: dict[str, Any] = {"content": blocks, "expect_replay_key": replay_key}
    if cost_usd is not None and Decimal(cost_usd) != 0:
        turn["usage_cost"] = cost_usd  # the recorded (synthetic) cost, so a cost cap replays
    return ScriptTurn.model_validate(turn)


def recorded_script(store: Store, run_id: str) -> Script:
    """Rebuild the scripted-provider input from an ingested run's responses.

    One turn per answered request, in journal order, each expecting the
    recorded request's replay key: agent requests become `turns`, reducer
    requests `reducer_turns`, compaction-summary requests `summary_turns`. An
    error or aborted response is not a turn: the script ends there instead,
    which reproduces the recorded ending (the provider failing at that
    request). Replaying it re-runs Pi under the recorded responses and fails
    closed on any context drift (A34).

    RLM: `children` holds each child run's recorded script, rebuilt the same
    way (recursively), in child-index order, so a child's scripted provider
    also fails closed on drift. A refused call spawned nothing and has no
    entry; a child that wrote no spool gets an empty script so later indices
    still line up. Children of an incomplete child are included if recorded.
    """
    store.run_record(run_id)
    by_role: dict[str, list[ScriptTurn]] = {"agent": [], "reducer": [], "compaction-summary": []}
    for event in store.events(run_id):
        if event.type != "invocation.committed":
            continue
        receipt = store.receipt(event.payload["invocation_id"])
        if receipt.invocation_kind != "decision" or "message" not in receipt.outputs:
            continue
        if receipt.metadata.get("stop_reason") in ("error", "aborted"):
            continue
        turn = _turn(
            store.get_json(receipt.outputs["message"]),
            receipt.metadata["replay_key"],
            receipt.usage.cost_usd,
        )
        by_role[receipt.metadata["role"]].append(turn)
    return Script.model_validate(
        {
            "source": f"replay:{run_id}",
            "turns": by_role["agent"],
            "reducer_turns": by_role["reducer"],
            "summary_turns": by_role["compaction-summary"],
            "children": [
                recorded_script(store, child)
                if child is not None
                else Script(source=f"replay:{run_id}:child-{index:04d}:no-spool", turns=())
                for index, child in child_links(store, run_id)
            ],
        }
    )
