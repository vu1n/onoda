"""M2-003: local ORCA solver adapter fails closed and runs a stand-in executable.

The "binary" used here is a small Python script, clearly labelled as a
stand-in and never presented as ORCA, that mimics ORCA's calling convention
(argv[1] is the .inp file, main output goes to stdout).
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from onoda.errors import CapabilityError, OnodaError, SubmissionUncertain
from onoda.science.orca_local import OrcaLocalScheduler, OrcaSettings, read_produced

STAND_IN_MARKER = "STAND-IN EXECUTABLE - NOT ORCA"


def _write_executable(path: Path, script: str) -> Path:
    path.write_text(script)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_stand_in(
    tmp_path: Path, *, name: str = "orca_stand_in.py", body: str | None = None
) -> Path:
    if body is None:
        body = f"""#!/usr/bin/env {sys.executable}
import sys
print("{STAND_IN_MARKER}")
print("job input:", open(sys.argv[1]).read())
sys.exit(0)
"""
    return _write_executable(tmp_path / name, body)


def make_failing_stand_in(tmp_path: Path) -> Path:
    body = f"""#!/usr/bin/env {sys.executable}
import sys
print("{STAND_IN_MARKER}")
print("deliberate failure", file=sys.stderr)
sys.exit(7)
"""
    return _write_executable(tmp_path / "orca_fail.py", body)


def make_slow_stand_in(tmp_path: Path, *, sleep_seconds: float = 3.0) -> Path:
    body = f"""#!/usr/bin/env {sys.executable}
import sys, time
print("{STAND_IN_MARKER}")
time.sleep({sleep_seconds})
sys.exit(0)
"""
    return _write_executable(tmp_path / "orca_slow.py", body)


def base_inputs() -> dict[str, bytes]:
    return {"input": b"! HF def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"}


# --- disabled / unresolved-setting refusals -------------------------------------------


def test_disabled_by_default_refuses():
    settings = OrcaSettings()
    assert settings.enabled is False
    with pytest.raises(CapabilityError, match="disabled"):
        OrcaLocalScheduler(settings)


def test_enabled_but_no_binary_refuses(tmp_path):
    settings = OrcaSettings(enabled=True, work_root=str(tmp_path / "work"), max_wall_seconds=10)
    with pytest.raises(CapabilityError, match="binary"):
        OrcaLocalScheduler(settings)


def test_enabled_but_no_work_root_refuses(tmp_path):
    stand_in = make_stand_in(tmp_path)
    settings = OrcaSettings(enabled=True, binary=str(stand_in), max_wall_seconds=10)
    with pytest.raises(CapabilityError, match="work_root"):
        OrcaLocalScheduler(settings)


def test_enabled_but_no_max_wall_seconds_refuses(tmp_path):
    stand_in = make_stand_in(tmp_path)
    settings = OrcaSettings(enabled=True, binary=str(stand_in), work_root=str(tmp_path / "work"))
    with pytest.raises(CapabilityError, match="max_wall_seconds"):
        OrcaLocalScheduler(settings)


def test_non_executable_binary_refuses(tmp_path):
    not_exec = tmp_path / "not_exec.py"
    not_exec.write_text("print('hi')")
    # explicitly ensure no execute bits
    not_exec.chmod(0o644)
    settings = OrcaSettings(
        enabled=True,
        binary=str(not_exec),
        work_root=str(tmp_path / "work"),
        max_wall_seconds=10,
    )
    with pytest.raises(CapabilityError, match="executable"):
        OrcaLocalScheduler(settings)


def test_missing_binary_file_refuses(tmp_path):
    settings = OrcaSettings(
        enabled=True,
        binary=str(tmp_path / "does_not_exist"),
        work_root=str(tmp_path / "work"),
        max_wall_seconds=10,
    )
    with pytest.raises(CapabilityError):
        OrcaLocalScheduler(settings)


def test_env_vars_never_implicitly_read(tmp_path, monkeypatch):
    stand_in = make_stand_in(tmp_path)
    monkeypatch.setenv("ORCA_BINARY", str(stand_in))
    monkeypatch.setenv("ORCA_ENABLED", "1")
    monkeypatch.setenv("ORCA_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("ORCA_MAX_WALL_SECONDS", "10")
    settings = OrcaSettings()
    with pytest.raises(CapabilityError, match="disabled"):
        OrcaLocalScheduler(settings)


# --- happy path / idempotency / metadata ----------------------------------------------


def _enabled_scheduler(
    tmp_path, binary: Path, *, stand_in: bool = True, max_wall_seconds: int = 10
):
    settings = OrcaSettings(
        enabled=True,
        binary=str(binary),
        work_root=str(tmp_path / "work"),
        max_wall_seconds=max_wall_seconds,
        stand_in=stand_in,
    )
    return OrcaLocalScheduler(settings)


def test_happy_path_completes_and_collects(tmp_path):
    stand_in = make_stand_in(tmp_path)
    sched = _enabled_scheduler(tmp_path, stand_in)

    job_id = sched.submit("key-1", "orca", base_inputs(), {})
    assert sched.status(job_id) == "completed"

    outputs = sched.collect(job_id)
    assert "log" in outputs and "stderr" in outputs
    assert outputs["log"].startswith(STAND_IN_MARKER.encode())
    assert sched.executions == 1

    meta = sched.job_metadata(job_id)
    assert meta["labels"] == ["stand-in", "not-orca"]
    assert meta["stand_in"] is True
    assert meta["returncode"] == 0
    assert meta["binary_sha256"].startswith("sha256:")
    import hashlib

    expected = "sha256:" + hashlib.sha256(stand_in.read_bytes()).hexdigest()
    assert meta["binary_sha256"] == expected
    assert isinstance(meta["max_wall_seconds"], int)
    assert isinstance(meta["duration_ms"], int)
    assert all(not isinstance(v, float) for v in meta.values())


def test_wrong_program_name_raises(tmp_path):
    stand_in = make_stand_in(tmp_path)
    sched = _enabled_scheduler(tmp_path, stand_in)
    with pytest.raises(OnodaError):
        sched.submit("key-x", "not-orca", base_inputs(), {})


def test_repeated_key_returns_same_job_no_rerun(tmp_path):
    stand_in = make_stand_in(tmp_path)
    sched = _enabled_scheduler(tmp_path, stand_in)

    job_id_1 = sched.submit("dup-key", "orca", base_inputs(), {})
    assert sched.executions == 1
    job_id_2 = sched.submit("dup-key", "orca", base_inputs(), {})
    assert job_id_2 == job_id_1
    assert sched.executions == 1


def test_repeated_key_after_failure_does_not_rerun(tmp_path):
    failing = make_failing_stand_in(tmp_path)
    sched = _enabled_scheduler(tmp_path, failing)

    job_id_1 = sched.submit("fail-key", "orca", base_inputs(), {})
    assert sched.status(job_id_1) == "failed"
    assert sched.executions == 1

    job_id_2 = sched.submit("fail-key", "orca", base_inputs(), {})
    assert job_id_2 == job_id_1
    assert sched.executions == 1  # retries are the caller's decision with a new key


def test_nonzero_exit_is_failed_with_returncode(tmp_path):
    failing = make_failing_stand_in(tmp_path)
    sched = _enabled_scheduler(tmp_path, failing)

    job_id = sched.submit("key-fail", "orca", base_inputs(), {})
    assert sched.status(job_id) == "failed"
    meta = sched.job_metadata(job_id)
    assert meta["returncode"] == 7
    assert meta["reason"] == "nonzero_exit"

    outputs = sched.collect(job_id)
    assert b"deliberate failure" in outputs["stderr"]


def test_timeout_produces_failed_status_with_timeout_reason(tmp_path):
    slow = make_slow_stand_in(tmp_path, sleep_seconds=3.0)
    sched = _enabled_scheduler(tmp_path, slow, max_wall_seconds=1)

    job_id = sched.submit("key-slow", "orca", base_inputs(), {})
    assert sched.status(job_id) == "failed"
    meta = sched.job_metadata(job_id)
    assert meta["reason"] == "timeout"


def test_missing_input_key_raises(tmp_path):
    stand_in = make_stand_in(tmp_path)
    sched = _enabled_scheduler(tmp_path, stand_in)
    with pytest.raises(OnodaError):
        sched.submit("key-noinput", "orca", {}, {})


# --- crash mid-run / submission_uncertain ----------------------------------------------


def test_crash_mid_run_reports_submission_uncertain(tmp_path):
    stand_in = make_stand_in(tmp_path)
    work_root = tmp_path / "work"
    work_root.mkdir(parents=True)

    # Hand-write a submitted-but-unfinished job, simulating a controller crash
    # between persisting intent and the subprocess finishing.
    state = {
        "jobs": {
            "job-00001": {
                "key": "crash-key",
                "program": "orca",
                "status": "submitted",
                "command": [str(stand_in), "job.inp"],
                "binary": str(stand_in),
                "binary_sha256": "sha256:deadbeef",
                "max_wall_seconds": 10,
                "stand_in": True,
                "returncode": None,
                "reason": None,
                "duration_ms": None,
            }
        },
        "by_key": {"crash-key": "job-00001"},
        "executions": 0,
    }
    (work_root / "scheduler.json").write_text(json.dumps(state))

    settings = OrcaSettings(
        enabled=True,
        binary=str(stand_in),
        work_root=str(work_root),
        max_wall_seconds=10,
        stand_in=True,
    )
    sched = OrcaLocalScheduler(settings)

    with pytest.raises(SubmissionUncertain):
        sched.status("job-00001")
    with pytest.raises(SubmissionUncertain):
        sched.lookup("crash-key")
    with pytest.raises(SubmissionUncertain):
        sched.collect("job-00001")

    # Resubmitting under the SAME key must not silently re-run it either.
    with pytest.raises(SubmissionUncertain):
        sched.submit("crash-key", "orca", base_inputs(), {})
    assert sched.executions == 0


def test_unlaunchable_binary_fails_instead_of_sticking_at_submitted(tmp_path):
    binary = tmp_path / "no-shebang"
    binary.write_text("this is not a program\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    sched = _enabled_scheduler(tmp_path, binary)
    job = sched.submit("k", "orca", base_inputs(), {})
    assert sched.status(job) == "failed"
    assert sched.job_metadata(job)["reason"].startswith("launch_error")
    assert sched.executions == 0
    assert sched.lookup("k") == job


# --- nothing a job started outlives it; produced files are plain files --------------


def _alive(pid: int) -> bool:
    """True while `pid` runs; a zombie (killed, not yet reaped) counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_a_background_process_does_not_outlive_its_job(tmp_path):
    body = "#!/bin/sh\nsleep 60 >/dev/null 2>&1 &\necho $! > bg.pid\nexit 0\n"
    sched = _enabled_scheduler(tmp_path, _write_executable(tmp_path / "orca_bg.sh", body))
    job = sched.submit("k", "orca", base_inputs(), {})
    assert sched.status(job) == "completed"
    pid = int((sched.root / job / "bg.pid").read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    alive = _alive(pid)
    if alive:
        os.kill(pid, signal.SIGKILL)
    assert not alive


def test_produced_files_are_regular_files_with_one_link(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret\n")
    body = f"""#!/usr/bin/env {sys.executable}
import os
open("job.gbw", "w").write("orbitals")
os.link({str(secret)!r}, "job_hard.txt")
os.symlink({str(secret)!r}, "job_sym.txt")
os.mkfifo("job_fifo.txt")
"""
    sched = _enabled_scheduler(tmp_path, make_stand_in(tmp_path, body=body))
    job = sched.submit("k", "orca", base_inputs(), {})
    assert set(sched.produced_files(job)) == {"job.gbw"}
    job_dir = sched.root / job
    assert read_produced(job_dir / "job.gbw") == b"orbitals"
    for name in ("job_hard.txt", "job_sym.txt", "job_fifo.txt", "missing"):
        with pytest.raises(OnodaError):
            read_produced(job_dir / name)


# --- output size cap -----------------------------------------------------------------

MIB = 1 << 20


def _writer(tmp_path: Path, target: str, mib: int) -> Path:
    """A stand-in that writes `mib` MiB to `target` ("stdout" or a file name)."""
    body = f"""#!/usr/bin/env {sys.executable}
import sys
out = sys.stdout if {target!r} == "stdout" else open({target!r}, "w")
print("{STAND_IN_MARKER}", file=out)
for _ in range({mib} * 16):
    out.write("x" * 65535 + "\\n")
out.flush()
"""
    return make_stand_in(tmp_path, name=f"orca_write_{target}.py", body=body)


@pytest.mark.parametrize("target", ["stdout", "job.gbw"])
def test_output_past_the_cap_fails_as_output_too_large(tmp_path, target):
    sched = _enabled_scheduler(tmp_path, _writer(tmp_path, target, 3))
    job = sched.submit("k", "orca", base_inputs(), {}, max_file_bytes=MIB)
    assert sched.status(job) == "failed"
    assert sched.job_metadata(job)["reason"] == "output_too_large"
    for f in (sched.root / job).iterdir():
        assert f.stat().st_size <= MIB, f.name
    # Under the cap the same job completes.
    ok = sched.submit("k2", "orca", base_inputs(), {}, max_file_bytes=8 * MIB)
    assert sched.status(ok) == "completed"


def test_a_job_killed_by_sigxfsz_fails_as_output_too_large(tmp_path):
    body = "#!/bin/sh\nhead -c 3000000 /dev/zero > job.gbw\necho after\n"
    sched = _enabled_scheduler(tmp_path, _write_executable(tmp_path / "orca_dd.sh", body))
    job = sched.submit("k", "orca", base_inputs(), {}, max_file_bytes=MIB)
    meta = sched.job_metadata(job)
    assert (sched.status(job), meta["reason"]) == ("failed", "output_too_large")
    assert (sched.root / job / "job.gbw").stat().st_size == MIB
