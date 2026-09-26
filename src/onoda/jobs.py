"""Mock external job scheduler with durable, idempotent submission (BUILD_SPEC §3, §7).

`MockScheduler` stands in for an external system (local ORCA, SLURM): its state
lives in its own directory, outside the run store, so a crash in the controller
does not erase what the "remote" accepted. Submission is idempotent by key.
`lookup_available=False` models a scheduler whose job listing cannot be
queried, which is what forces `submission_uncertain` instead of a blind retry.

Programs are deterministic synthetic functions registered by name; nothing
here runs a solver. Output is labelled synthetic by the programs themselves.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from onoda.errors import OnodaError, SubmissionUncertain

Program = Callable[[dict[str, bytes], dict[str, Any], str], dict[str, bytes]]
"""(inputs by name, config, job_id) -> outputs by name."""


class MockScheduler:
    def __init__(
        self,
        root: str | os.PathLike[str],
        programs: dict[str, Program],
        *,
        lookup_available: bool = True,
        crash_hook: Callable[[str], None] | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.programs = programs
        self.lookup_available = lookup_available
        self.crash_hook = crash_hook

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
        """Accept a job once per idempotency key; a repeated key returns the same job."""
        state = self._load()
        if key in state["by_key"]:
            return state["by_key"][key]
        if program not in self.programs:
            raise OnodaError(f"unknown program {program!r}")
        job_id = f"job-{len(state['jobs']) + 1:05d}"
        outputs = self.programs[program](inputs, config, job_id)
        state["jobs"][job_id] = {
            "key": key,
            "program": program,
            "status": "completed",
            "outputs": {k: v.decode("latin-1") for k, v in outputs.items()},
        }
        state["by_key"][key] = job_id
        state["executions"] += 1
        self._save(state)
        if self.crash_hook:
            self.crash_hook("remote.accepted")
        return job_id

    def lookup(self, key: str) -> str | None:
        if not self.lookup_available:
            raise SubmissionUncertain(f"scheduler cannot confirm whether {key!r} was accepted")
        return self._load()["by_key"].get(key)

    def status(self, job_id: str) -> str:
        return self._load()["jobs"][job_id]["status"]

    def collect(self, job_id: str) -> dict[str, bytes]:
        job = self._load()["jobs"][job_id]
        if job["status"] != "completed":
            raise OnodaError(f"job {job_id} is {job['status']}, not collectable")
        return {k: v.encode("latin-1") for k, v in job["outputs"].items()}

    def cancel(self, job_id: str) -> None:
        state = self._load()
        if state["jobs"][job_id]["status"] == "running":
            state["jobs"][job_id]["status"] = "cancelled"
            self._save(state)

    @property
    def executions(self) -> int:
        """Physical executions performed; duplicates would show up here."""
        return self._load()["executions"]
