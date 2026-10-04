"""Local ORCA solver adapter (BUILD_SPEC §6 M2-003).

Same surface as `onoda.jobs.MockScheduler` (`submit/lookup/status/collect/
cancel/executions`), but backed by a user-provided ORCA binary invoked as a
real subprocess, in a job directory outside the run store. Every setting
(`enabled`, `binary`, `work_root`, `max_wall_seconds`) defaults to a value
that refuses to run; `OrcaSettings.resolve()` raises `CapabilityError` before
anything is launched unless all of them are explicitly configured (A7:
ORCA is licensed software; this adapter never installs, downloads, or
redistributes it, only invokes a binary the operator already has).

`submit()` is synchronous: it writes the input, launches the process, waits
(bounded by `max_wall_seconds`), and returns only once the job has reached a
terminal state. There is no separate "poll until done" step.

Durable state lives in `<work_root>/scheduler.json` (atomic tmp+replace,
mirroring `MockScheduler`). Submission intent -- key, command, binary
sha256, status "submitted" -- is written *before* the subprocess is
launched, so a crash mid-run leaves a `submitted` record with no terminal
marker. `status()`/`lookup()` on such a job raise `SubmissionUncertain`
rather than silently re-running it; a caller who wants to retry must submit
under a new key, since a repeated key always returns the original job_id
without re-executing anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from onoda.errors import CapabilityError, OnodaError, SubmissionUncertain

_STAND_IN_LABELS = ["stand-in", "not-orca"]
_ORCA_LABELS = ["orca-local"]

# Extra input files (e.g. an earlier job's `.gbw`) are written next to `job.inp`
# under plain names; the job's own driver files can never be supplied. The broker
# protocol (`orca_broker`) uses the same rule for the file names it passes on.
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB_FILES = frozenset({"job.inp", "job.out", "job.err"})


class JobWrapper(Protocol):
    """Confines one job: an argv prefix and the environment it starts with (A65).

    `orca_sandbox.JobSandbox` implements it; the scheduler names no backend.
    """

    kind: str

    def wrap(self, job_dir: Path) -> tuple[list[str], dict[str, str]]: ...


class OrcaSettings(BaseModel):
    """Every field defaults to a value that keeps live execution disabled.

    `resolve()` must be called before use; it is the single fail-closed gate.
    Nothing here is ever read from an environment variable implicitly, and
    enablement is never inferred from a binary merely being present on PATH.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    binary: str | None = None
    work_root: str | None = None
    max_wall_seconds: int | None = None
    stand_in: bool = False

    # Context: doc://onoda/live-off-by-default@0001#live-settings-explicit — ORCA execution is off unless every setting is explicit; fail closed
    def resolve(self) -> ResolvedOrcaSettings:
        if not self.enabled:
            raise CapabilityError("live ORCA execution is disabled")
        if self.binary is None:
            raise CapabilityError("ORCA binary path is not configured")
        if self.work_root is None:
            raise CapabilityError("ORCA work_root is not configured")
        if self.max_wall_seconds is None:
            raise CapabilityError("ORCA max_wall_seconds is not configured")
        if self.max_wall_seconds <= 0:
            raise CapabilityError("ORCA max_wall_seconds must be positive")
        binary_path = Path(self.binary)
        if not binary_path.is_file():
            raise CapabilityError(f"ORCA binary {self.binary!r} is not a file")
        if not os.access(binary_path, os.X_OK):
            raise CapabilityError(f"ORCA binary {self.binary!r} is not executable")
        return ResolvedOrcaSettings(
            binary=str(binary_path.resolve()),
            work_root=str(Path(self.work_root).resolve()),
            max_wall_seconds=self.max_wall_seconds,
            stand_in=self.stand_in,
        )


class ResolvedOrcaSettings(BaseModel):
    """Output of `OrcaSettings.resolve()`: every field is concrete and usable."""

    model_config = ConfigDict(frozen=True)

    binary: str
    work_root: str
    max_wall_seconds: int
    stand_in: bool


def job_labels(stand_in: bool) -> list[str]:
    """The labels a job's metadata carries: a stand-in is labelled as not ORCA."""
    return list(_STAND_IN_LABELS if stand_in else _ORCA_LABELS)


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _plain_file(st: os.stat_result) -> bool:
    """A regular file with one link: not a device, FIFO, directory or hard link to elsewhere."""
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _file_size_limit(max_bytes: int | None):
    """A preexec_fn setting `RLIMIT_FSIZE` in the child, or None for no cap."""
    if max_bytes is None:
        return None

    def limit() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))

    return limit


def _hit_file_limit(returncode: int, job_dir: Path, max_bytes: int | None) -> bool:
    """Whether the job ran into `RLIMIT_FSIZE`.

    Killed by SIGXFSZ (directly, or as bwrap reports a signalled child), or,
    since a program that ignores the signal (Python does) only sees a failed
    write, a file in the job dir that reached the cap.
    """
    if max_bytes is None:
        return False
    if returncode in (-signal.SIGXFSZ, 128 + signal.SIGXFSZ):
        return True
    return any(p.lstat().st_size >= max_bytes for p in job_dir.iterdir())


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # the group is already empty


def read_produced(path: str | os.PathLike[str]) -> bytes:
    """The bytes of a file a job produced, refusing anything but a plain file.

    A job could leave a symlink or a hard link to a host file, or a FIFO that
    blocks the reader; `O_NOFOLLOW`, `O_NONBLOCK` and a check of the open
    file's own `fstat` refuse each (`OnodaError`), without a gap between the
    check and the read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise OnodaError(f"{path}: not readable as a produced file: {exc}") from exc
    with os.fdopen(fd, "rb") as fh:
        if not _plain_file(os.fstat(fd)):
            raise OnodaError(f"{path}: not a regular file with a single link")
        return fh.read()


class OrcaLocalScheduler:
    """Runs ORCA jobs synchronously via subprocess, with MockScheduler's surface."""

    def __init__(self, settings: OrcaSettings):
        self._settings = settings.resolve()
        self.root = Path(self._settings.work_root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def settings(self) -> ResolvedOrcaSettings:
        return self._settings

    @property
    def _state_path(self) -> Path:
        return self.root / "scheduler.json"

    def _load(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"jobs": {}, "by_key": {}, "executions": 0}
        return json.loads(self._state_path.read_text())

    def _save(self, state: dict[str, Any]) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True))
        os.replace(tmp, self._state_path)

    def submit(
        self,
        key: str,
        program: str,
        inputs: dict[str, bytes],
        config: dict[str, Any],
        *,
        wrapper: JobWrapper | None = None,
        max_file_bytes: int | None = None,
    ) -> str:
        """Accept a job once per idempotency key; a repeated key never re-runs.

        `inputs["input"]` becomes `job.inp`; every other entry is written next
        to it under its own name (a plain file name, never one of the job's
        driver files), so the input can read it, e.g. `%moinp "j0001.gbw"`.
        `wrapper` confines the job (the broker always passes its job sandbox,
        A65); without one the job runs as this user, for trusted input only.
        `max_file_bytes` caps every file the job writes (`RLIMIT_FSIZE`); a job
        that reaches it fails with reason `output_too_large`.
        """
        if program != "orca":
            raise OnodaError(f"unknown program {program!r}")
        if "input" not in inputs:
            raise OnodaError("ORCA submission requires an 'input' entry (.inp bytes)")
        extra = {name: data for name, data in inputs.items() if name != "input"}
        for name in extra:
            if not FILE_NAME_RE.match(name) or name in _JOB_FILES:
                raise OnodaError(f"extra input file name {name!r} is not allowed")

        state = self._load()
        if key in state["by_key"]:
            existing = state["by_key"][key]
            if state["jobs"][existing]["status"] == "submitted":
                raise SubmissionUncertain(
                    f"job {existing!r} for key {key!r} is submitted with no terminal "
                    "marker (controller crashed mid-run); reconcile before retrying"
                )
            return existing

        job_id = f"job-{len(state['jobs']) + 1:05d}"
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        inp_path = job_dir / "job.inp"
        inp_path.write_bytes(inputs["input"])
        for name, data in extra.items():
            (job_dir / name).write_bytes(data)

        binary = self._settings.binary
        command = [binary, "job.inp"]
        binary_sha256 = sha256_file(binary)

        # Persist submission intent BEFORE launching, so a crash mid-run is
        # visible as `submitted` with no terminal marker, never a silent re-run.
        state["jobs"][job_id] = {
            "key": key,
            "program": program,
            "status": "submitted",
            "command": command,
            "binary": binary,
            "binary_sha256": binary_sha256,
            "max_wall_seconds": self._settings.max_wall_seconds,
            "stand_in": self._settings.stand_in,
            "returncode": None,
            "reason": None,
            "duration_ms": None,
            "extra_inputs": sorted(extra),
            "sandbox": wrapper.kind if wrapper is not None else None,
            "max_file_bytes": max_file_bytes,
        }
        state["by_key"][key] = job_id
        self._save(state)

        out_path = job_dir / "job.out"
        err_path = job_dir / "job.err"
        prefix: list[str] = []
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if wrapper is not None:
            prefix, env = wrapper.wrap(job_dir)

        start = time.monotonic()
        outcome: dict[str, Any]
        try:
            with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
                # Own process group, so a timeout also stops ORCA's child
                # programs (orca_scf, mpirun, ...) instead of only the driver.
                proc = subprocess.Popen(
                    [*prefix, *command],
                    cwd=job_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                    env=env,
                    shell=False,
                    start_new_session=True,
                    preexec_fn=_file_size_limit(max_file_bytes),
                )
                try:
                    returncode = proc.wait(timeout=self._settings.max_wall_seconds)
                except subprocess.TimeoutExpired:
                    _kill_group(proc.pid)
                    proc.wait()
                    outcome = {"status": "failed", "reason": "timeout", "executed": True}
                else:
                    # Whatever the job left running in its group (a backgrounded
                    # helper, a stray MPI rank) must not outlive it, nor touch
                    # the job directory while its products are read.
                    _kill_group(proc.pid)
                    reason = None if returncode == 0 else "nonzero_exit"
                    if _hit_file_limit(returncode, job_dir, max_file_bytes):
                        reason = "output_too_large"
                    outcome = {
                        "status": "completed" if reason is None else "failed",
                        "reason": reason,
                        "returncode": returncode,
                        "executed": True,
                    }
        except OSError as exc:
            # The binary could not be launched; nothing ran, and the key must
            # not stay `submitted` forever.
            outcome = {"status": "failed", "reason": f"launch_error: {exc}", "executed": False}

        state = self._load()
        job = state["jobs"][job_id]
        job["duration_ms"] = int((time.monotonic() - start) * 1000)
        job["status"] = outcome["status"]
        job["reason"] = outcome["reason"]
        job["returncode"] = outcome.get("returncode")
        if outcome["executed"]:
            state["executions"] += 1
        self._save(state)

        return job_id

    def lookup(self, key: str) -> str | None:
        state = self._load()
        job_id = state["by_key"].get(key)
        if job_id is not None and state["jobs"][job_id]["status"] == "submitted":
            raise SubmissionUncertain(
                f"job {job_id!r} for key {key!r} is submitted with no terminal marker "
                "(controller crashed mid-run); reconcile before retrying"
            )
        return job_id

    def status(self, job_id: str) -> str:
        job = self._load()["jobs"][job_id]
        if job["status"] == "submitted":
            raise SubmissionUncertain(
                f"job {job_id!r} is submitted with no terminal marker "
                "(controller crashed mid-run); reconcile before retrying"
            )
        return job["status"]

    def collect(self, job_id: str) -> dict[str, bytes]:
        job = self._load()["jobs"][job_id]
        if job["status"] == "submitted":
            raise SubmissionUncertain(f"job {job_id!r} has no terminal marker")
        if job["status"] not in ("completed", "failed"):
            raise OnodaError(f"job {job_id} is {job['status']}, not collectable")
        job_dir = self.root / job_id
        out = (job_dir / "job.out").read_bytes() if (job_dir / "job.out").exists() else b""
        err = (job_dir / "job.err").read_bytes() if (job_dir / "job.err").exists() else b""
        return {"log": out, "stderr": err}

    def produced_files(self, job_id: str) -> dict[str, Path]:
        """Files the job left in its directory, other than its inputs and logs.

        Only regular files with a single link directly in the job directory
        count (ORCA names its products after the input's basename: `job.gbw`,
        `job_property.txt`, ...); read them with `read_produced`.
        """
        job = self._load()["jobs"][job_id]
        if job["status"] == "submitted":
            raise SubmissionUncertain(f"job {job_id!r} has no terminal marker")
        skip = _JOB_FILES | set(job.get("extra_inputs", ()))
        job_dir = self.root / job_id
        return {
            p.name: p
            for p in sorted(job_dir.iterdir())
            if p.name not in skip and _plain_file(p.lstat())
        }

    def cancel(self, job_id: str) -> None:
        # submit() runs synchronously to completion, so there is never a job
        # actually mid-flight to cancel by the time a caller can call this.
        state = self._load()
        if state["jobs"][job_id]["status"] == "submitted":
            state["jobs"][job_id]["status"] = "cancelled"
            self._save(state)

    def job_metadata(self, job_id: str) -> dict[str, Any]:
        job = self._load()["jobs"][job_id]
        return {
            "command": job["command"],
            "binary": job["binary"],
            "binary_sha256": job["binary_sha256"],
            "returncode": job["returncode"],
            "max_wall_seconds": job["max_wall_seconds"],
            "duration_ms": job["duration_ms"],
            "stand_in": job["stand_in"],
            "labels": job_labels(job["stand_in"]),
            "reason": job["reason"],
            "sandbox": job.get("sandbox"),
        }

    @property
    def executions(self) -> int:
        """Physical subprocess executions performed; duplicates would show up here."""
        return self._load()["executions"]
