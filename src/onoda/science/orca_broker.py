"""Host-side ORCA broker for VTE tasks (A59).

The Pi sandbox cannot run ORCA, so `orca_submit` sends a `BrokerSubmit` over
a Unix socket in the Pi work directory to this broker, which owns the
`OrcaLocalScheduler`. One server thread answers one JSON line per connection
with one `BrokerReply` line. Per submission the broker:

1. refuses once `max_executions` submissions got a job (cache hits count);
2. resolves `attach` entries to files that earlier jobs *of this broker*
   produced, and places each next to the new input as `<job_id><rest>`
   (`j0001` + `job.gbw` -> `j0001.gbw`), so it never collides with the files
   ORCA writes for `job.inp` and the input names it unambiguously;
3. computes the cache key, sha256 of (input bytes, placed attachments by
   name and sha256, ORCA binary sha256, stand-in flag), so a stand-in's
   outputs and real ORCA outputs never share a cache entry;
4. serves a hit from the shared cache directory as acquired (`cache_hit`,
   `executed_here: false`), or runs a new scheduler job under a fresh
   scheduler key (a specification digest is never an execution id), and
   caches it if it completed.

Job ids come from a counter that advances when an id is assigned, so no id
(and no scheduler key) is ever reused. If anything fails after the scheduler
answered, the job is journaled as `failed` with reason `broker_error`, its
real `executed_here`, and no output or files. If an exception escapes before
that is known (the scheduler itself raised), the broker refuses every later
submission (`broker_failed`): fail closed.

Every request and reply is appended (fsynced) to `broker.jsonl` before the
reply is sent; that journal is the authoritative execution record ingest
checks the spooled `task.execute` lines against. In replay mode
(`BrokerReplay`) the broker has no scheduler and no cache: it serves only the
outputs a journal recorded, in order per key, and refuses anything else.
Each replayed reply repeats the recorded `cache_hit` and `reason`, so the
agent is shown exactly what it was shown (a recorded execution replays with
neither flag set); `executed_here` is always false.

Every live job runs in a job sandbox (`orca_sandbox`, A65) that sees only its
job directory, ORCA_HOME and the MPI prefix; the broker refuses to start for
real ORCA without one that works. Before anything runs, `input_problem`
refuses (`input_rejected`) inputs that name paths or files that are not
attachments, write elsewhere, start other programs, or exceed the broker's
`BrokerLimits`; that check is a tripwire, the sandbox is the boundary.

Only `completed` jobs are cached. ORCA exits 0 on many failed calculations,
so a cached output is not a converged one; that is for the parser and grader.

The wire protocol (`BrokerSubmit` -> `BrokerReply`, JSON Lines, one request
per connection) is defined here and mirrored by `pi/extensions/lib/spool.ts`;
`adapters.pi.vte_contract` re-exports it for the Pi side.
"""

from __future__ import annotations

import base64
import io
import os
import re
import socketserver
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, BinaryIO, Literal

from pydantic import AfterValidator, Field, ValidationError, model_validator

from onoda.canonical import DIGEST_PREFIX, digest_of, sha256_digest
from onoda.errors import CapabilityError, IntegrityError, OnodaError
from onoda.records import Frozen as _Frozen
from onoda.science.orca_local import (
    FILE_NAME_RE,
    OrcaLocalScheduler,
    read_produced,
    sha256_file,
)
from onoda.science.orca_sandbox import JobSandbox

BROKER_PROTOCOL = "onoda.orca-broker/1"
JOURNAL_NAME = "broker.jsonl"
JOURNAL_SCHEMA = "onoda.orca-broker-journal/1"
CACHE_KEY_SCHEMA = "onoda.orca-broker-key/2"  # /2 adds the stand-in flag
CACHE_ENTRY_SCHEMA = "onoda.orca-broker-cache/1"
MAX_REQUEST_BYTES = 1 << 20  # one JSON line; a 64 KiB input escapes to well under this
MAX_JOBS = 9999  # job ids are j0001..j9999

# Submissions per run, cache hits included (A59). The paper caps attempts at 8
# per target specification; a small molecule here has about three.
DEFAULT_MAX_EXECUTIONS = 24

# Broker job ids are per run (`j0001`, ...), never the scheduler's own ids.
JOB_ID_RE = re.compile(r"^j\d{4}$")
# `FILE_NAME_RE` (the scheduler's rule): a file an earlier job produced, named as
# the job directory holds it (e.g. `job.gbw`).
MAX_INPUT_CHARS = 65536


def _job_id(v: str) -> str:
    if not JOB_ID_RE.match(v):
        raise ValueError(f"job id {v!r} is not a broker job id (j0001, ...)")
    return v


def _plain_file_name(v: str) -> str:
    if not FILE_NAME_RE.match(v):
        raise ValueError(f"file name {v!r} is not a plain file name")
    return v


# Shared by the broker protocol and `vte_contract` (`VteTaskBundle`, `ClaimedVte`).
JobId = Annotated[str, AfterValidator(_job_id)]
PlainFileName = Annotated[str, AfterValidator(_plain_file_name)]

RefusalReason = Literal[
    "max_executions",
    "invalid_request",
    "input_rejected",
    "unknown_attachment",
    "duplicate_attachment",
    "attachment_changed",
    "cache_corrupt",
    "replay_miss",
    "broker_failed",
]


@dataclass(frozen=True)
class BrokerLimits:
    """Per-job resource caps the broker enforces on agent inputs (A65)."""

    max_nprocs: int = 4  # `%pal nprocs` / `! PALn`
    max_maxcore_mb: int = 4000  # `%maxcore`, MB per process

    def __post_init__(self) -> None:
        if self.max_nprocs < 1 or self.max_maxcore_mb < 1:
            raise OnodaError("broker limits must be positive")


DEFAULT_LIMITS = BrokerLimits()

# Input tripwire (A65). The job sandbox (`orca_sandbox`) is the boundary; these
# checks make an attempt to leave it visible as an `input_rejected` refusal in
# the journal before ORCA starts, and keep ordinary inputs inside the job dir
# when the sandbox would only have stopped them part-way. Matching is
# case-insensitive over the whole text, comments included: ORCA ends a `#`
# comment at the next `#`, so text after a comment marker may still be input.
#
# - `%base` names the output basename, which may be a path anywhere;
#   `%compound` runs a script whose `Sys_Cmd` runs shell commands;
#   `ExtOpt`/`ProgExt` run an external program as the optimizer or method;
#   `$new_job` chains jobs that would bypass the per-job checks.
_FORBIDDEN = re.compile(r"%base\b|%compound\b|sys_cmd|progext|extopt|\$new_job", re.I)
# Any quoted string that looks like a path, whatever directive takes it.
_QUOTED = re.compile(r'"([^"\n]*)"')
_PATHLIKE = re.compile(r"[/\\]|\.\.")
# Directives that read a file: the argument must name a file in the job dir,
# i.e. an attachment (the job dir holds only the input and its attachments).
_FILE_ARGS = re.compile(
    r'%moinp\b[ \t]*("[^"\n]*"|\S*)'
    r'|^[ \t]*\*[ \t]*(?:xyzfile|gzmtfile|pdbfile)\b[ \t]+\S+[ \t]+\S+[ \t]*("[^"\n]*"|\S*)'
    r'|\binhessname\b[ \t]*("[^"\n]*"|\S*)',
    re.I | re.M,
)
_NPROCS = re.compile(r"\bnprocs\b[ \t]*(\S*)", re.I)
_SIMPLE_LINE = re.compile(r"^[ \t]*!.*$", re.M)
_PAL_KEYWORD = re.compile(r"\bpal(\S*)", re.I)
_MAXCORE = re.compile(r"%maxcore\b[ \t]*(\S*)", re.I)


def input_problem(text: str, attached: Iterable[str], limits: BrokerLimits) -> str | None:
    """Why the broker refuses this ORCA input (A65), or None when it passes.

    `attached` are the names placed next to the input (`j0001.gbw`, ...).
    """
    if m := _FORBIDDEN.search(text):
        return f"{m.group(0)!r} is not allowed"
    for m in _QUOTED.finditer(text):
        if _PATHLIKE.search(m.group(1)):
            return f"quoted path {m.group(0)!r}"
    names = set(attached)
    for m in _FILE_ARGS.finditer(text):
        arg = next(g for g in m.groups() if g is not None).strip('"')
        if not FILE_NAME_RE.match(arg) or arg not in names:
            return f"{m.group(0).strip()!r} does not name an attached file"
    for m in _NPROCS.finditer(text):
        if not m.group(1).isdigit() or int(m.group(1)) > limits.max_nprocs:
            return f"{m.group(0).strip()!r}: at most {limits.max_nprocs} processes"
    for line in _SIMPLE_LINE.finditer(text):
        for m in _PAL_KEYWORD.finditer(line.group(0)):
            if not m.group(1).isdigit() or int(m.group(1)) > limits.max_nprocs:
                return f"{m.group(0)!r}: at most {limits.max_nprocs} processes"
    for m in _MAXCORE.finditer(text):
        if not m.group(1).isdigit() or int(m.group(1)) > limits.max_maxcore_mb:
            return f"{m.group(0).strip()!r}: at most {limits.max_maxcore_mb} MB"
    return None


def _check_outcome(
    status: str | None, job_id: str | None, executed_here: bool, cache_hit: bool
) -> None:
    """Shared by `BrokerReply` and `BrokerJournalEntry`: what a submission's outcome allows."""
    if status == "refused" and (job_id is not None or executed_here or cache_hit):
        raise ValueError("a refused submission has no job and ran nothing")
    if cache_hit and executed_here:
        raise ValueError("a cache hit is never an execution")


class BrokerAttach(_Frozen):
    """A file an earlier job of this run produced, copied next to the new input."""

    job_id: JobId
    name: PlainFileName


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
        if self.status != "refused":
            if self.job_id is None or not JOB_ID_RE.match(self.job_id):
                raise ValueError("a completed or failed submission names its job id")
            if self.output_sha256 is None or self.input_sha256 is None:
                raise ValueError("a completed or failed submission pins input and output")
        _check_outcome(self.status, self.job_id, self.executed_here, self.cache_hit)
        return self


class ProducedFile(_Frozen):
    """A file a job produced that a later submission may attach."""

    name: PlainFileName
    sha256: str


class BrokerJournalEntry(_Frozen):
    """One line of `broker.jsonl`: a request as received or the reply sent for it.

    A request entry pins the raw line (`request_sha256`) and, when it parsed,
    the input and attachments. A reply entry carries what the agent was told
    plus the host-side facts: cache key, scheduler job, ORCA binary, duration,
    whether the binary is a labelled stand-in, and the files the job produced.
    """

    schema_: Literal["onoda.orca-broker-journal/1"] = Field(JOURNAL_SCHEMA, alias="schema")
    seq: int = Field(ge=1)
    event: Literal["request", "reply"]
    tool_call_id: str | None = None
    # request
    request_sha256: str | None = None
    input_sha256: str | None = None
    attach: tuple[BrokerAttach, ...] = ()
    # reply
    status: Literal["completed", "failed", "refused"] | None = None
    reason: str | None = None
    key: str | None = None
    job_id: str | None = None
    executed_here: bool = False
    cache_hit: bool = False
    replay: bool = False
    output_sha256: str | None = None
    files: tuple[ProducedFile, ...] = ()
    binary_sha256: str | None = None
    stand_in: bool | None = None
    scheduler_job_id: str | None = None
    duration_ms: int | None = None
    sandbox: str | None = None  # the job sandbox an executed job ran in (A65)

    @model_validator(mode="after")
    def _consistent(self) -> BrokerJournalEntry:
        if self.event == "request":
            if self.request_sha256 is None or self.status is not None:
                raise ValueError("a request entry pins its raw line and has no status")
        elif self.status is None:
            raise ValueError("a reply entry has a status")
        elif self.status != "refused" and (
            self.job_id is None or self.key is None or self.output_sha256 is None
        ):
            raise ValueError("a completed or failed reply names its job, key and output")
        _check_outcome(self.status, self.job_id, self.executed_here, self.cache_hit)
        if self.replay and self.executed_here:
            raise ValueError("replay never executes")
        return self


def parse_journal(data: bytes, where: str) -> tuple[BrokerJournalEntry, ...]:
    """Parse broker journal bytes; refuses a malformed line or a gap in `seq`.

    `where` names the journal in errors (a path or a store digest).
    """
    entries = []
    for n, line in enumerate(io.BytesIO(data), start=1):
        try:
            entry = BrokerJournalEntry.model_validate_json(line)
        except ValidationError as exc:
            raise IntegrityError(f"{where}: line {n} is not a broker journal entry: {exc}") from exc
        if entry.seq != n:
            raise IntegrityError(f"{where}: line {n} has seq {entry.seq}")
        entries.append(entry)
    return tuple(entries)


def read_journal(path: str | os.PathLike[str]) -> tuple[BrokerJournalEntry, ...]:
    """`parse_journal` over the file at `path`."""
    with open(path, "rb") as fh:
        return parse_journal(fh.read(), str(path))


def attachable(name: str) -> bool:
    """ORCA names its products after the input's basename (`job.gbw`, `job_property.txt`)."""
    return bool(FILE_NAME_RE.match(name)) and name.startswith(("job.", "job_"))


def placed_name(job_id: str, name: str) -> str:
    """The name an attached file gets next to the new input: `j0001` + `.gbw`."""
    return job_id + name[len("job") :]


def cache_key(
    input_sha256: str, attached: Iterable[tuple[str, str]], binary_sha256: str, stand_in: bool
) -> str:
    """sha256 of (input bytes, attached files by placed name and sha256, ORCA binary, stand-in).

    The binary's sha256 already differs between a stand-in and ORCA in practice;
    the flag makes it explicit that their outputs never share an entry.
    """
    return digest_of(
        {
            "schema": CACHE_KEY_SCHEMA,
            "input": input_sha256,
            "attach": [[name, sha] for name, sha in sorted(attached)],
            "binary": binary_sha256,
            "stand_in": stand_in,
        }
    )


@dataclass(frozen=True)
class ReplayJob:
    """A recorded job outcome: its status, exact log bytes and produced files."""

    status: Literal["completed", "failed"]
    output: bytes
    files: tuple[ProducedFile, ...] = ()
    stand_in: bool | None = None
    # As recorded, so the agent is shown what it was shown: an executed job
    # replays with neither flag set (replay never executes), a hit as a hit.
    cache_hit: bool = True
    reason: str | None = None


@dataclass
class BrokerReplay:
    """What a replaying broker may serve: cache key -> recorded jobs, in order.

    Each submission consumes the next recorded job for its key; a key with
    none left is a `replay_miss` and nothing is served (fail closed, A59).
    """

    binary_sha256: str
    jobs: dict[str, list[ReplayJob]] = field(default_factory=dict)
    stand_in: bool = False  # as recorded; part of every cache key

    @classmethod
    def from_journal(
        cls, entries: Iterable[BrokerJournalEntry], outputs: Mapping[str, bytes]
    ) -> BrokerReplay:
        """Build from a recorded journal plus its output bytes by `sha256:` digest."""
        binaries: set[str] = set()
        stand_ins: set[bool] = set()
        jobs: dict[str, list[ReplayJob]] = {}
        for e in entries:
            if e.event != "reply" or e.status == "refused":
                continue
            assert e.key is not None and e.output_sha256 is not None
            data = outputs.get(e.output_sha256)
            if data is None or sha256_digest(data) != e.output_sha256:
                raise OnodaError(f"replay: no bytes for output {e.output_sha256} of {e.job_id}")
            if e.binary_sha256 is not None:
                binaries.add(e.binary_sha256)
            stand_ins.add(bool(e.stand_in))
            jobs.setdefault(e.key, []).append(
                ReplayJob(e.status, data, e.files, e.stand_in, e.cache_hit, e.reason)
            )
        if len(binaries) > 1:
            raise OnodaError(f"replay: journal names {len(binaries)} ORCA binaries, need one")
        if len(stand_ins) > 1:
            raise OnodaError("replay: journal mixes stand-in and ORCA jobs")
        # A run that never got a job replays as one that serves nothing.
        return cls(
            binary_sha256=binaries.pop() if binaries else "none",
            jobs=jobs,
            stand_in=stand_ins.pop() if stand_ins else False,
        )


@dataclass(frozen=True)
class _Job:
    """A job of this broker: its produced files, with a host path unless replayed."""

    files: dict[str, tuple[str, Path | None]]  # name -> (sha256, path)


def _write_synced(path: Path, data: bytes) -> None:
    with open(path, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


class _Cache:
    """Shared, append-only cache: `<root>/<key hex>/{entry.json,log,files/<name>}`.

    An entry is staged in a temporary directory and renamed into place whole,
    so a reader sees all of it or none; an existing entry is never replaced.
    Every byte is checked against `entry.json` when served.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, key: str) -> Path:
        return self.root / key.removeprefix(DIGEST_PREFIX)

    def get(self, key: str) -> tuple[bytes, dict[str, tuple[str, Path]], bool] | None:
        d = self._dir(key)
        if not d.is_dir():
            return None
        try:
            entry = _CacheEntry.model_validate_json((d / "entry.json").read_bytes())
            log = (d / "log").read_bytes()
        except (OSError, ValidationError) as exc:
            raise _CacheCorrupt(f"cache entry {key}: {exc}") from exc
        if entry.key != key or sha256_digest(log) != entry.output_sha256:
            raise _CacheCorrupt(f"cache entry {key} does not match its log")
        files: dict[str, tuple[str, Path]] = {}
        for f in entry.files:
            # The entry is data from a shared directory: its names are checked
            # before any path is built from them.
            if not attachable(f.name):
                raise _CacheCorrupt(f"cache entry {key}: {f.name!r} is not a product name")
            p = d / "files" / f.name
            try:
                data = read_produced(p)
            except OnodaError as exc:
                raise _CacheCorrupt(f"cache entry {key}: file {f.name}: {exc}") from exc
            if sha256_digest(data) != f.sha256:
                raise _CacheCorrupt(f"cache entry {key}: file {f.name} does not match")
            files[f.name] = (f.sha256, p)
        return log, files, entry.stand_in

    def put(
        self, key: str, log: bytes, files: Mapping[str, Path], *, binary: str, stand_in: bool
    ) -> None:
        final = self._dir(key)
        if final.exists():
            return
        tmp = self.root / f".tmp-{uuid.uuid4().hex}"
        (tmp / "files").mkdir(parents=True)
        _write_synced(tmp / "log", log)
        produced = []
        for name, src in sorted(files.items()):
            data = read_produced(src)
            _write_synced(tmp / "files" / name, data)
            produced.append(ProducedFile(name=name, sha256=sha256_digest(data)))
        entry = _CacheEntry(
            key=key,
            output_sha256=sha256_digest(log),
            files=tuple(produced),
            binary_sha256=binary,
            stand_in=stand_in,
        )
        _write_synced(tmp / "entry.json", entry.model_dump_json(by_alias=True).encode())
        try:
            os.rename(tmp, final)
        except OSError:
            if not final.exists():
                raise
            # Another broker cached the same key first; keep its entry.


class _CacheEntry(_Frozen):
    schema_: Literal["onoda.orca-broker-cache/1"] = Field(CACHE_ENTRY_SCHEMA, alias="schema")
    key: str
    output_sha256: str
    files: tuple[ProducedFile, ...]
    binary_sha256: str
    stand_in: bool


class _CacheCorrupt(OnodaError):
    pass


class _Refused(Exception):
    def __init__(self, reason: RefusalReason):
        self.reason = reason


def _check_confinement(
    scheduler: OrcaLocalScheduler,
    job_sandbox: JobSandbox | None,
    cache_dir: str | os.PathLike[str] | None,
    limits: BrokerLimits | None,
) -> None:
    """Refuse a live broker that would run agent input unconfined (A65): fail closed.

    Real ORCA needs a working job sandbox that cannot read the cache; only a
    labelled stand-in may run without one (a host without bwrap/Seatbelt).
    """
    if limits is None:
        raise OnodaError("a live broker checks every input against its limits")
    if job_sandbox is None:
        if not scheduler.settings.stand_in:
            raise CapabilityError("the broker runs ORCA only inside a job sandbox (A65)")
        return
    job_sandbox.check(scheduler.root)
    if cache_dir is not None and job_sandbox.exposes(cache_dir):
        raise CapabilityError(f"the ORCA job sandbox could read the broker cache {cache_dir}")


class OrcaBroker:
    """Answers `BrokerSubmit`s for one run; see the module docstring.

    Live mode takes a `scheduler` (and optionally a shared `cache_dir`);
    replay mode takes a `replay` and never executes. The journal is created
    fresh at `journal_path` and refused if it already exists, so one broker
    journal is exactly one run's submissions.
    """

    def __init__(
        self,
        journal_path: str | os.PathLike[str],
        *,
        scheduler: OrcaLocalScheduler | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        replay: BrokerReplay | None = None,
        max_executions: int = DEFAULT_MAX_EXECUTIONS,
        job_sandbox: JobSandbox | None = None,
        limits: BrokerLimits | None = DEFAULT_LIMITS,
    ):
        if (scheduler is None) == (replay is None):
            raise OnodaError("a broker runs either a scheduler or a replay, exactly one")
        if replay is not None and (cache_dir is not None or job_sandbox is not None):
            raise OnodaError("a replaying broker serves recorded outputs only, no cache")
        if not 0 < max_executions <= MAX_JOBS:
            raise OnodaError(f"max_executions must be in 1..{MAX_JOBS}")
        self.journal_path = Path(journal_path)
        self._scheduler = scheduler
        self._replay = replay
        self._cache = _Cache(Path(cache_dir)) if cache_dir is not None else None
        self.max_executions = max_executions
        # None only when replaying a run recorded before the input checks (A65).
        self.limits = limits
        self.job_sandbox = job_sandbox
        if scheduler is not None:
            _check_confinement(scheduler, job_sandbox, cache_dir, limits)
            self.binary_sha256 = sha256_file(scheduler.settings.binary)
            self.stand_in = scheduler.settings.stand_in
        else:
            assert replay is not None
            self.binary_sha256 = replay.binary_sha256
            self.stand_in = replay.stand_in
            # Consumed as served; the caller's replay stays as given.
            self._replay = BrokerReplay(
                replay.binary_sha256,
                {k: list(v) for k, v in replay.jobs.items()},
                replay.stand_in,
            )
        self._run = uuid.uuid4().hex  # scheduler keys are per execution, never the cache key
        self._jobs: dict[str, _Job] = {}
        # Job ids come from this counter, advanced the moment an id is assigned, so an
        # id (and with it the scheduler key `orca-broker:<run>:<job id>`) is never reused.
        self._next_job = 0
        # Set when an exception escaped a submission: what ran is then not on record,
        # so every later submission is refused (fail closed).
        self._broken = False
        self._seq = 0
        self._lock = threading.Lock()
        self._server: socketserver.UnixStreamServer | None = None
        self._thread: threading.Thread | None = None
        self.socket_path: Path | None = None
        # Refuse to continue someone else's journal.
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

    # --- journal ---------------------------------------------------------------------

    def _append(self, **fields: object) -> BrokerJournalEntry:
        self._seq += 1
        entry = BrokerJournalEntry(seq=self._seq, **fields)  # type: ignore[arg-type]
        line = entry.model_dump_json(by_alias=True).encode() + b"\n"
        with open(self.journal_path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    # --- submissions -----------------------------------------------------------------

    def handle_line(self, line: bytes) -> BrokerReply:
        """Parse one request line and answer it; a malformed line is refused and journaled."""
        with self._lock:
            try:
                request = BrokerSubmit.model_validate_json(line)
            except ValidationError:
                self._append(event="request", request_sha256=sha256_digest(line))
                return self._refuse(None, "invalid_request")
            return self._submit(request, line)

    def submit(self, request: BrokerSubmit) -> BrokerReply:
        """Answer one submission; the reply is journaled before it is returned."""
        with self._lock:
            return self._submit(request, request.model_dump_json().encode())

    def _refuse(self, tool_call_id: str | None, reason: RefusalReason) -> BrokerReply:
        self._append(event="reply", tool_call_id=tool_call_id, status="refused", reason=reason)
        return BrokerReply(status="refused", reason=reason)

    def _submit(self, request: BrokerSubmit, raw: bytes) -> BrokerReply:
        input_bytes = request.input.encode("utf-8")
        input_sha = sha256_digest(input_bytes)
        self._append(
            event="request",
            tool_call_id=request.tool_call_id,
            request_sha256=sha256_digest(raw),
            input_sha256=input_sha,
            attach=request.attach,
        )
        try:
            if self._broken:
                raise _Refused("broker_failed")
            if self._next_job >= self.max_executions:
                raise _Refused("max_executions")
            attached = self._resolve(request.attach)
            if self.limits is not None and input_problem(request.input, attached, self.limits):
                raise _Refused("input_rejected")
            key = cache_key(
                input_sha,
                [(n, sha) for n, (sha, _) in attached.items()],
                self.binary_sha256,
                self.stand_in,
            )
            if self._replay is not None:
                return self._serve_replay(request, key, input_sha)
            return self._serve_live(request, key, input_sha, input_bytes, attached)
        except _Refused as r:
            return self._refuse(request.tool_call_id, r.reason)
        except BaseException:
            self._broken = True
            raise

    def _allocate(self) -> str:
        """The next job id; called once a submission is sure to get a job."""
        self._next_job += 1
        return f"j{self._next_job:04d}"

    def _resolve(self, attach: tuple[BrokerAttach, ...]) -> dict[str, tuple[str, Path | None]]:
        placed: dict[str, tuple[str, Path | None]] = {}
        for a in attach:
            job = self._jobs.get(a.job_id)
            if job is None or a.name not in job.files:
                raise _Refused("unknown_attachment")
            name = placed_name(a.job_id, a.name)
            if name in placed:
                raise _Refused("duplicate_attachment")
            placed[name] = job.files[a.name]
        return placed

    def _reply(
        self,
        request: BrokerSubmit,
        *,
        job_id: str,
        key: str,
        status: Literal["completed", "failed"],
        input_sha: str,
        log: bytes,
        files: dict[str, tuple[str, Path | None]],
        executed_here: bool,
        cache_hit: bool,
        stand_in: bool | None,
        scheduler_job_id: str | None = None,
        duration_ms: int | None = None,
        reason: str | None = None,
        sandbox: str | None = None,
    ) -> BrokerReply:
        self._jobs[job_id] = _Job(files=files)
        output_sha = sha256_digest(log)
        self._append(
            event="reply",
            tool_call_id=request.tool_call_id,
            status=status,
            reason=reason,
            key=key,
            job_id=job_id,
            executed_here=executed_here,
            cache_hit=cache_hit,
            replay=self._replay is not None,
            input_sha256=input_sha,
            output_sha256=output_sha,
            files=tuple(ProducedFile(name=n, sha256=sha) for n, (sha, _) in sorted(files.items())),
            binary_sha256=self.binary_sha256,
            stand_in=stand_in,
            scheduler_job_id=scheduler_job_id,
            duration_ms=duration_ms,
            sandbox=sandbox,
        )
        return BrokerReply(
            status=status,
            job_id=job_id,
            cache_hit=cache_hit,
            executed_here=executed_here,
            input_sha256=input_sha,
            output_sha256=output_sha,
            output_b64=base64.b64encode(log).decode("ascii"),
            files=tuple(sorted(files)),
            reason=reason,
        )

    def _serve_replay(self, request: BrokerSubmit, key: str, input_sha: str) -> BrokerReply:
        assert self._replay is not None
        queue = self._replay.jobs.get(key)
        if not queue:
            raise _Refused("replay_miss")
        rec = queue.pop(0)
        return self._reply(
            request,
            job_id=self._allocate(),
            key=key,
            status=rec.status,
            input_sha=input_sha,
            log=rec.output,
            files={f.name: (f.sha256, None) for f in rec.files},
            executed_here=False,
            cache_hit=rec.cache_hit,
            stand_in=rec.stand_in,
            reason=rec.reason,
        )

    def _serve_live(
        self,
        request: BrokerSubmit,
        key: str,
        input_sha: str,
        input_bytes: bytes,
        attached: dict[str, tuple[str, Path | None]],
    ) -> BrokerReply:
        assert self._scheduler is not None
        if self._cache is not None:
            try:
                hit = self._cache.get(key)
            except _CacheCorrupt as exc:
                raise _Refused("cache_corrupt") from exc
            if hit is not None:
                log, files, stand_in = hit
                if stand_in != self.stand_in:  # the key says otherwise
                    raise _Refused("cache_corrupt")
                return self._reply(
                    request,
                    job_id=self._allocate(),
                    key=key,
                    status="completed",
                    input_sha=input_sha,
                    log=log,
                    files=dict(files),
                    executed_here=False,
                    cache_hit=True,
                    stand_in=stand_in,
                )

        inputs = {"input": input_bytes}
        for name, (sha, path) in attached.items():
            assert path is not None  # live jobs always hold host paths
            try:
                data = read_produced(path)
            except OnodaError as exc:
                raise _Refused("attachment_changed") from exc
            if sha256_digest(data) != sha:
                raise _Refused("attachment_changed")
            inputs[name] = data

        job_id = self._allocate()
        sched = self._scheduler
        before = sched.executions
        # An exception up to here escapes and closes the broker: whether ORCA ran is unknown.
        sid = sched.submit(
            f"orca-broker:{self._run}:{job_id}", "orca", inputs, {}, wrapper=self.job_sandbox
        )
        executed = sched.executions > before
        try:
            status = sched.status(sid)
            if status not in ("completed", "failed"):
                raise OnodaError(f"scheduler job {sid} ended {status!r}")
            log = sched.collect(sid)["log"]
            meta = sched.job_metadata(sid)
            produced = {n: p for n, p in sched.produced_files(sid).items() if attachable(n)}
            files: dict[str, tuple[str, Path | None]] = {
                n: (sha256_digest(read_produced(p)), p) for n, p in produced.items()
            }
            if status == "completed" and self._cache is not None:
                self._cache.put(
                    key, log, produced, binary=self.binary_sha256, stand_in=bool(meta["stand_in"])
                )
        except Exception:
            # The scheduler answered, so whether ORCA ran is known: the job is
            # journaled as failed with no output and no files, never retried
            # under its id, and nothing it produced is cached or attachable.
            return self._reply(
                request,
                job_id=job_id,
                key=key,
                status="failed",
                input_sha=input_sha,
                log=b"",
                files={},
                executed_here=executed,
                cache_hit=False,
                stand_in=sched.settings.stand_in,
                scheduler_job_id=sid,
                reason="broker_error",
                sandbox=self.job_sandbox.kind if self.job_sandbox is not None else None,
            )
        return self._reply(
            request,
            job_id=job_id,
            key=key,
            status=status,
            input_sha=input_sha,
            log=log,
            files=files,
            executed_here=executed,
            cache_hit=False,
            stand_in=bool(meta["stand_in"]),
            scheduler_job_id=sid,
            duration_ms=meta["duration_ms"],
            reason=meta["reason"],
            sandbox=meta["sandbox"],
        )

    @property
    def jobs(self) -> int:
        """Submissions that got a job id (executed, failed or cache hit)."""
        return self._next_job

    # --- socket server ---------------------------------------------------------------

    def _send(self, wfile: BinaryIO, reply: BrokerReply) -> None:
        """Write one reply line; a separate method so tests can interpose on the send."""
        wfile.write(reply.model_dump_json().encode() + b"\n")
        wfile.flush()

    def serve(self, socket_path: str | os.PathLike[str]) -> None:
        """Listen on `socket_path` (mode 0600) in one background thread."""
        if self._server is not None:
            raise OnodaError("broker is already serving")
        path = Path(socket_path)
        if len(os.fsencode(path)) >= 104:  # sun_path: 104 bytes on macOS, 108 on Linux
            raise OnodaError(f"broker socket path is too long for AF_UNIX: {path}")
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    return
                reply = broker.handle_line(line)
                broker._send(self.wfile, reply)

        old = os.umask(0o177)
        try:
            server = socketserver.UnixStreamServer(str(path), Handler)
        finally:
            os.umask(old)
        self._server = server
        self.socket_path = path
        self._thread = threading.Thread(
            target=server.serve_forever, name="onoda-orca-broker", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None and self.socket_path is not None
        self._thread.join()
        self.socket_path.unlink(missing_ok=True)
        self._server = None
        self._thread = None

    def __enter__(self) -> OrcaBroker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
