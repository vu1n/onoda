"""A65: the job sandbox that confines agent-submitted ORCA jobs.

The unit tests build wrappers and profiles and run on any host. The probe
tests run a stand-in (a labelled Python script, never ORCA) through the broker
inside this host's real sandbox (bwrap on Linux, Seatbelt on macOS) and skip
where there is none. The darwin tests need macOS; the real-ORCA test also
needs `ONODA_TEST_ORCA_BINARY` (and `ONODA_TEST_MPI_HOME` for MPI).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

import pytest
from test_orca_broker import INPUT, broker, runs, sandbox_for, scheduler, stand_in, submit

from onoda.errors import CapabilityError
from onoda.science import orca_sandbox
from onoda.science.orca_broker import OrcaBroker, read_journal
from onoda.science.orca_local import OrcaLocalScheduler, OrcaSettings
from onoda.science.orca_sandbox import (
    BwrapJobSandbox,
    SeatbeltJobSandbox,
    detect_job_sandbox,
    host_job_sandbox,
)

on_darwin = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")


def balanced(profile: str) -> bool:
    depth = 0
    for tok in re.sub(r'"(\\.|[^"\\])*"|#"[^"]*"', '""', profile):
        depth += {"(": 1, ")": -1}.get(tok, 0)
        if depth < 0:
            return False
    return depth == 0


@pytest.fixture
def host_sandbox(tmp_path):
    """The stand-in's binary and this host's working job sandbox; skip without one."""
    binary = stand_in(tmp_path)
    sb = sandbox_for(binary)
    if sb is None:
        pytest.skip("no working job sandbox on this host (bwrap or Seatbelt)")
    return binary, sb


# ------------------------------------------------------------------ units


def test_bwrap_binds_only_the_job_dir_read_write(tmp_path):
    orca, mpi, job = tmp_path / "orca", tmp_path / "mpi", tmp_path / "work" / "job-00001"
    for d in (orca, mpi / "bin", job):
        d.mkdir(parents=True)
    sb = BwrapJobSandbox(orca_home=orca, mpi_home=mpi, bwrap="/usr/bin/bwrap")
    argv, env = sb.wrap(job)
    assert argv[0] == "/usr/bin/bwrap" and argv[-1] == "--"
    for flag in ("--unshare-all", "--die-with-parent", "--clearenv"):
        assert flag in argv
    pairs = list(zip(argv, argv[1:], argv[2:], strict=False))
    assert [(a, b) for f, a, b in pairs if f == "--bind"] == [(str(job), str(job))]
    ro = {a for f, a, b in pairs if f == "--ro-bind"}
    assert {str(orca), str(mpi)} <= ro and str(tmp_path) not in ro
    assert not any(str(tmp_path / "work") == p for p in ro)
    setenv = {a: b for f, a, b in pairs if f == "--setenv"}
    assert setenv["HOME"] == setenv["TMPDIR"] == str(job.resolve())
    assert setenv["PATH"].split(":")[:2] == [str(mpi / "bin"), str(orca)]
    assert setenv["OMPI_MCA_btl_vader_single_copy_mechanism"] == "none"
    assert set(setenv) == {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "OMPI_MCA_btl_vader_single_copy_mechanism",
    }
    assert env == {"PATH": orca_sandbox.SYSTEM_PATH}
    assert "--share-net" not in argv
    assert sb.exposes(orca / "x") and not sb.exposes(tmp_path / "cache")


def test_the_seatbelt_profile_confines_exec_files_and_network(tmp_path):
    orca, mpi = tmp_path / "orca", tmp_path / "mpi"
    job = tmp_path / "jobs" / "job-00001"
    sb = SeatbeltJobSandbox(orca_home=orca, mpi_home=mpi)
    profile = sb.profile(job)
    assert balanced(profile)
    assert profile.splitlines()[1:3] == ["(version 1)", "(deny default)"]
    # Exec only under ORCA_HOME and MPI_HOME, and the shell.
    execs = re.findall(r"\(allow process-exec\n((?:  \((?:subpath|literal) \"[^\"]*\"\)\n?)+)\)",
                       profile)  # fmt: skip
    named = {v for body in execs for v in re.findall(r'"([^"]*)"', body)}
    assert {str(orca), str(mpi), "/bin/sh"} <= named
    shells = ("/bin/sh", "/private/var/select/sh")
    assert named - {str(orca), str(mpi)} <= {*shells, *(str(Path(p).resolve()) for p in shells)}
    assert "(allow process-exec process-fork)" not in profile
    # Writes only to the job dir (plus the /dev nodes).
    (rw,) = re.findall(r'\(allow file-write\*\n((?:  \(subpath "[^"]*"\)\n?)+)\)', profile)
    assert re.findall(r'"([^"]*)"', rw) == [str(job)]
    assert f'(subpath "{tmp_path / "jobs"}")' not in profile
    # Network: denied, then loopback and unix sockets in the job dir (last match wins).
    deny = profile.index("(deny network*)")
    assert profile.index('(allow network* (local ip "localhost:*"))') > deny
    assert profile.index(f'(allow network* (subpath "{job}"))') > deny
    assert "(allow network*)" not in profile and "(allow sysctl-read)" not in profile
    assert "kern.procargs" not in profile and '"kern."' not in profile
    argv, _ = sb.wrap(job)
    assert argv[:6] == ["/usr/bin/env", "-i", argv[2], f"HOME={job}", f"TMPDIR={job}",
                        "LANG=en_US.UTF-8"]  # fmt: skip
    assert argv[2] == f"PATH={mpi / 'bin'}:{orca}:{orca_sandbox.SYSTEM_PATH}"
    assert argv[6:] == ["/usr/bin/sandbox-exec", "-p", profile]
    assert sb.exposes(orca / "orca") and not sb.exposes(tmp_path / "cache")


def test_detect_picks_the_backend_and_refuses_otherwise(tmp_path, monkeypatch):
    binary = stand_in(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sys, "platform", "linux")
    sb = detect_job_sandbox(binary)
    assert isinstance(sb, BwrapJobSandbox) and sb.orca_home == binary.parent.resolve()
    assert sb.extra_ro == ()  # only a stand-in sees this Python
    assert detect_job_sandbox(binary, stand_in=True).extra_ro
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(detect_job_sandbox(binary), SeatbeltJobSandbox)
    with pytest.raises(CapabilityError, match="MPI home"):
        detect_job_sandbox(binary, mpi_home=tmp_path / "missing")
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(CapabilityError, match="Linux or macOS"):
        detect_job_sandbox(binary)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(CapabilityError, match="bwrap"):
        detect_job_sandbox(binary)
    # Without a sandbox only a stand-in runs (unconfined); real ORCA is refused.
    scratch = tmp_path / "scratch"
    assert host_job_sandbox(binary, scratch=scratch, stand_in=True) is None
    with pytest.raises(CapabilityError, match="bwrap"):
        host_job_sandbox(binary, scratch=scratch, stand_in=False)


def real_orca_scheduler(tmp_path: Path, binary: Path) -> OrcaLocalScheduler:
    return OrcaLocalScheduler(
        OrcaSettings(
            enabled=True,
            binary=str(binary),
            work_root=str(tmp_path / "work"),
            max_wall_seconds=10,
            stand_in=False,
        )
    )


def test_the_broker_refuses_real_orca_without_a_working_sandbox(tmp_path):
    binary = stand_in(tmp_path)
    sched = real_orca_scheduler(tmp_path, binary)
    with pytest.raises(CapabilityError, match="job sandbox"):
        OrcaBroker(tmp_path / "r1" / "broker.jsonl", scheduler=sched, job_sandbox=None)
    broken = BwrapJobSandbox(orca_home=binary.parent, bwrap=str(tmp_path / "no-bwrap"))
    with pytest.raises(CapabilityError, match="does not start"):
        OrcaBroker(tmp_path / "r2" / "broker.jsonl", scheduler=sched, job_sandbox=broken)
    assert runs(binary) == 0


def test_the_broker_refuses_a_cache_the_sandbox_could_read(tmp_path, host_sandbox):
    binary, sb = host_sandbox
    with pytest.raises(CapabilityError, match="cache"):
        broker(tmp_path, scheduler=scheduler(tmp_path, binary), job_sandbox=sb,
               cache_dir=binary.parent / "cache")  # fmt: skip


# ------------------------------------------------------- this host's sandbox

PROBE = """#!/usr/bin/env {python}
import json, os, socket, subprocess
open("ran.stand-in", "w").write("")
print("STAND-IN EXECUTABLE - NOT ORCA")

def attempt(f):
    try:
        f()
        return "ok"
    except Exception as exc:
        return type(exc).__name__

def loopback():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    c = socket.create_connection(s.getsockname(), timeout=5); c.close(); s.close()

def unix():
    s = socket.socket(socket.AF_UNIX); s.bind("u.sock"); s.listen(1)
    c = socket.socket(socket.AF_UNIX); c.connect("u.sock"); c.close(); s.close()

def outside_net():
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()

def listing(path):
    try:
        return sorted(os.listdir(path))
    except Exception as exc:
        return type(exc).__name__

def exec_outside():
    subprocess.run(["/usr/bin/true"], check=True)

print(json.dumps({{
    "read_secret": attempt(lambda: open({secret!r}).read()),
    "list_cache": attempt(lambda: os.listdir({cache!r})),
    "read_cache": attempt(lambda: open({cached!r}).read()),
    "write_outside": attempt(lambda: open({escape!r}, "w").write("x")),
    "work_root": listing({work!r}),
    "loopback": attempt(loopback),
    "unix_socket": attempt(unix),
    "network": attempt(outside_net),
    "exec_outside": attempt(exec_outside),
    "home": os.environ.get("HOME"),
    "tmpdir": os.environ.get("TMPDIR"),
    "cwd": os.getcwd(),
    "secret_env": "ONODA_PROBE_SECRET" in os.environ,
}}))
"""


def probe(tmp_path: Path, binary_dir: Path) -> tuple[Path, Path]:
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret\n")
    cache = tmp_path / "cache"
    (cache / "entry").mkdir(parents=True)
    (cache / "entry" / "log").write_text("cached log\n")
    body = PROBE.format(
        python=sys.executable,
        secret=str(secret),
        cache=str(cache),
        cached=str(cache / "entry" / "log"),
        escape=str(tmp_path / "escaped.txt"),
        work=str(tmp_path / "work"),
    )
    binary = binary_dir / "orca_probe.py"
    binary.write_text(body)
    binary.chmod(0o755)
    return binary, cache


def run_probe(tmp_path: Path, binary: Path, sb, cache: Path, monkeypatch) -> tuple[dict, object]:
    monkeypatch.setenv("ONODA_PROBE_SECRET", "probe-secret")
    b = broker(tmp_path, scheduler=scheduler(tmp_path, binary), job_sandbox=sb, cache_dir=cache)
    reply = b.submit(submit(INPUT))
    assert reply.status == "completed", base64.b64decode(reply.output_b64)
    out = base64.b64decode(reply.output_b64).decode().splitlines()
    return json.loads(out[-1]), read_journal(b.journal_path)[-1]


def test_a_job_in_the_sandbox_reaches_only_its_job_dir(tmp_path, host_sandbox, monkeypatch):
    binary, sb = host_sandbox
    binary, cache = probe(tmp_path, binary.parent)
    seen, entry = run_probe(tmp_path, binary, sb, cache, monkeypatch)
    job_dir = str((tmp_path / "work" / entry.scheduler_job_id).resolve())
    assert entry.sandbox == sb.kind and entry.executed_here
    assert seen["read_secret"] != "ok"
    assert seen["list_cache"] != "ok" and seen["read_cache"] != "ok"  # F2: never the cache
    # bwrap gives the job a private /tmp, so a write there never reaches the host.
    assert not (tmp_path / "escaped.txt").exists()
    # Not even the other jobs: at most its own directory shows under the work root.
    assert seen["work_root"] in ("PermissionError", [entry.scheduler_job_id])
    assert seen["network"] != "ok"
    assert seen["loopback"] == "ok" and seen["unix_socket"] == "ok"  # what MPI needs
    assert seen["home"] == seen["tmpdir"] == seen["cwd"] == job_dir
    assert seen["secret_env"] is False
    assert runs(binary) == 1


def test_without_the_sandbox_the_same_probe_escapes(tmp_path, monkeypatch):
    """The probe is a real test: run unconfined (a stand-in only), it reads what it should not."""
    binary, cache = probe(tmp_path, stand_in(tmp_path).parent)
    seen, entry = run_probe(tmp_path, binary, None, cache, monkeypatch)
    assert entry.sandbox is None
    assert seen["read_secret"] == "ok" and seen["read_cache"] == "ok"


# ------------------------------------------------------------ darwin only


@on_darwin
def test_darwin_job_sandbox_execs_only_orca_mpi_and_the_shell(tmp_path, host_sandbox, monkeypatch):
    binary, sb = host_sandbox
    assert isinstance(sb, SeatbeltJobSandbox)
    binary, cache = probe(tmp_path, binary.parent)
    seen, _ = run_probe(tmp_path, binary, sb, cache, monkeypatch)
    assert seen["exec_outside"] != "ok"


@on_darwin
@pytest.mark.skipif(
    "ONODA_TEST_ORCA_BINARY" not in os.environ, reason="set ONODA_TEST_ORCA_BINARY to real ORCA"
)
def test_darwin_real_orca_runs_in_the_job_sandbox(tmp_path):
    """Real ORCA (the operator's), two MPI processes, inside the Seatbelt job sandbox."""
    binary = Path(os.environ["ONODA_TEST_ORCA_BINARY"])
    mpi = os.environ.get("ONODA_TEST_MPI_HOME")
    work = Path("/tmp") / f"onoda-orca-{os.getpid()}"  # short: OpenMPI's sockets live in TMPDIR
    sb = host_job_sandbox(binary, scratch=work, stand_in=False, mpi_home=mpi)
    sched = OrcaLocalScheduler(
        OrcaSettings(enabled=True, binary=str(binary), work_root=str(work), max_wall_seconds=600)
    )
    b = OrcaBroker(tmp_path / "broker.jsonl", scheduler=sched, job_sandbox=sb)
    pal = "%pal nprocs 2 end\n" if mpi else ""
    text = f"! HF def2-SVP\n%maxcore 500\n{pal}* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    reply = b.submit(submit(text))
    log = base64.b64decode(reply.output_b64).decode(errors="replace")
    assert reply.status == "completed", log[-3000:]
    assert "****ORCA TERMINATED NORMALLY****" in log
    assert read_journal(b.journal_path)[-1].sandbox == "seatbelt"
