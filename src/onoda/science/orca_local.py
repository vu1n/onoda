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
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from onoda.errors import CapabilityError, OnodaError, SubmissionUncertain

_STAND_IN_LABELS = ["stand-in", "not-orca"]
_ORCA_LABELS = ["orca-local"]


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


def _sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


class OrcaLocalScheduler:
    """Runs ORCA jobs synchronously via subprocess, with MockScheduler's surface."""

    def __init__(self, settings: OrcaSettings):
        self._settings = settings.resolve()
        self.root = Path(self._settings.work_root)
        self.root.mkdir(parents=True, exist_ok=True)

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
        self, key: str, program: str, inputs: dict[str, bytes], config: dict[str, Any]
    ) -> str:
        """Accept a job once per idempotency key; a repeated key never re-runs."""
        if program != "orca":
            raise OnodaError(f"unknown program {program!r}")
        if "input" not in inputs:
            raise OnodaError("ORCA submission requires an 'input' entry (.inp bytes)")

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

        binary = self._settings.binary
        command = [binary, "job.inp"]
        binary_sha256 = _sha256_file(binary)

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
        }
        state["by_key"][key] = job_id
        self._save(state)

        out_path = job_dir / "job.out"
        err_path = job_dir / "job.err"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

        start = time.monotonic()
        outcome: dict[str, Any]
        try:
            with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
                # Own process group, so a timeout also stops ORCA's child
                # programs (orca_scf, mpirun, ...) instead of only the driver.
                proc = subprocess.Popen(
                    command,
                    cwd=job_dir,
                    stdout=out_f,
                    stderr=err_f,
                    env=env,
                    shell=False,
                    start_new_session=True,
                )
                try:
                    returncode = proc.wait(timeout=self._settings.max_wall_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    outcome = {"status": "failed", "reason": "timeout", "executed": True}
                else:
                    outcome = {
                        "status": "completed" if returncode == 0 else "failed",
                        "reason": None if returncode == 0 else "nonzero_exit",
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

    def cancel(self, job_id: str) -> None:
        # submit() runs synchronously to completion, so there is never a job
        # actually mid-flight to cancel by the time a caller can call this.
        state = self._load()
        if state["jobs"][job_id]["status"] == "submitted":
            state["jobs"][job_id]["status"] = "cancelled"
            self._save(state)

    def job_metadata(self, job_id: str) -> dict[str, Any]:
        job = self._load()["jobs"][job_id]
        labels = _STAND_IN_LABELS if job["stand_in"] else _ORCA_LABELS
        return {
            "command": job["command"],
            "binary": job["binary"],
            "binary_sha256": job["binary_sha256"],
            "returncode": job["returncode"],
            "max_wall_seconds": job["max_wall_seconds"],
            "duration_ms": job["duration_ms"],
            "stand_in": job["stand_in"],
            "labels": labels,
            "reason": job["reason"],
        }

    @property
    def executions(self) -> int:
        """Physical subprocess executions performed; duplicates would show up here."""
        return self._load()["executions"]
