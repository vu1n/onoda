"""The job sandbox for agent-submitted ORCA jobs (A65).

The agent writes the ORCA input, so the input is untrusted: ORCA reads and
writes files it names (`%moinp`, `*xyzfile`, `%base`) and can start other
programs (`%compound` with `Sys_Cmd`, `ExtOpt`/`ProgExt`). The broker
(`orca_broker`) therefore runs every job inside this sandbox; the input
checks in the broker are a tripwire, this is the boundary.

A job sees, read-only, the system directories, ORCA_HOME (the directory of the
ORCA binary) and the MPI prefix when one is configured; its own job directory
is the only place it can write and the only non-system data it can read. It
has no network beyond loopback (OpenMPI's ranks talk over it), its
environment is cleared to `PATH`, `HOME` and `TMPDIR` (both the job dir) and
`LANG`, and it never sees the broker's cache, journal, the stores or the
repository, which are not named.

- **Linux** (`BwrapJobSandbox`): `bwrap --unshare-all` (new user, pid, ipc,
  uts, cgroup and network namespaces; the network namespace has only `lo`),
  `--die-with-parent`, `--clearenv`, a private `/tmp` and `/proc`, and
  read-only binds of the allowed paths with the job dir as the only
  read-write bind.
- **macOS** (`SeatbeltJobSandbox`): `env -i` then `sandbox-exec` with a
  generated profile: `(deny default)`; reads of the system paths, ORCA_HOME,
  MPI_HOME and the job dir; writes to the job dir only; `process-exec` only of
  programs under ORCA_HOME and MPI_HOME and `/bin/sh` (ORCA starts its
  sub-programs and `mpirun` through a shell); network only on loopback and on
  unix sockets under the job dir; `sysctl-read` only of an allowlist. Seatbelt
  has no pid namespace: a job that leaves its process group (`setsid`) is not
  killed with it; it still runs under this profile.

`stand_in=True` (tests and labelled stand-in runs only) also exposes this
Python interpreter read-only and executable, since stand-ins are Python scripts.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from onoda.errors import CapabilityError
from onoda.sandbox_util import (
    DARWIN_DEV_RW,
    DARWIN_SYSTEM,
    LINUX_SYSTEM_DIRS,
    SB_ROOT_LISTING,
    SYSTEM_PATH,
    bwrap_ro,
    covering,
    existing,
    resolved,
    sb_ancestors,
    sb_rule,
    sb_str,
    sb_sysctl_rules,
)

# /etc file by file: user and group lookups (OpenMPI asks for the user name) and
# the local host name; nothing that holds secrets.
_LINUX_ETC = ("/etc/passwd", "/etc/group", "/etc/hosts", "/etc/nsswitch.conf", "/etc/localtime")
# hwloc (inside OpenMPI) reads the CPU and NUMA topology here.
_LINUX_SYS = ("/sys/devices/system/cpu", "/sys/devices/system/node")
_DARWIN_ETC = ("/etc/passwd", "/etc/group", "/etc/hosts", "/etc/localtime")
# The shell ORCA and mpirun start: /bin/sh, which on macOS re-executes the
# shell /private/var/select/sh points at.
_SHELLS = ("/bin/sh", "/private/var/select/sh")
# User and group lookups over Mach (getpwuid); no DNS, no other service.
_DARWIN_MACH = ("com.apple.system.opendirectoryd.libinfo",)
# getifaddrs() reads the interface list through this sysctl (OpenMPI's TCP
# transports enumerate interfaces); it names interfaces, not processes.
_DARWIN_JOB_SYSCTL_PREFIXES = ("net.route.0.",)


def _python_dirs() -> tuple[Path, ...]:
    # sys.executable as invoked: its link chain passes a symlinked directory
    # (uv's cpython-3.11-...-none) that Seatbelt must be allowed to stat.
    return tuple(covering(resolved([sys.prefix, sys.base_prefix, sys.executable])))


@dataclass(frozen=True)
class _JobSandboxBase:
    kind: ClassVar[str]

    orca_home: Path
    mpi_home: Path | None = None
    extra_ro: tuple[Path, ...] = ()  # readable (and, on macOS, executable): stand-ins only

    def _visible(self) -> list[Path]:
        """Host paths the job may read besides its own job dir."""
        raise NotImplementedError

    def _path(self) -> str:
        bins = [str(self.mpi_home / "bin")] if self.mpi_home else []
        return ":".join([*bins, str(self.orca_home), SYSTEM_PATH])

    def exposes(self, path: str | Path) -> bool:
        """Whether a job could read `path` (it lies under a path the sandbox names)."""
        targets = {Path(path), Path(path).resolve()}
        return any(v == t or v in t.parents for v in self._visible() for t in targets)

    def wrap(self, job_dir: Path) -> tuple[list[str], dict[str, str]]:
        """The argv prefix and environment that run a command confined to `job_dir`."""
        raise NotImplementedError

    def check(self, scratch: Path) -> None:
        """Run a trivial command in the sandbox; `CapabilityError` unless it works (A65)."""
        scratch = Path(scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sandbox-check-", dir=scratch) as d:
            job = Path(d).resolve()
            argv, env = self.wrap(job)
            try:
                done = subprocess.run(
                    [*argv, "/bin/sh", "-c", "echo ok > check.txt"],
                    cwd=job,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CapabilityError(f"the ORCA job sandbox does not start: {exc}") from exc
            ok = done.returncode == 0 and (job / "check.txt").read_bytes() == b"ok\n"
            if not ok:
                err = done.stderr.decode(errors="replace").strip()
                raise CapabilityError(f"the ORCA job sandbox ({self.kind}) does not work: {err}")


@dataclass(frozen=True)
class BwrapJobSandbox(_JobSandboxBase):
    """Linux: bubblewrap, every namespace unshared, the job dir the only rw bind."""

    kind: ClassVar[str] = "bwrap"

    bwrap: str = "bwrap"

    def _visible(self) -> list[Path]:
        homes = [self.orca_home, *([self.mpi_home] if self.mpi_home else []), *self.extra_ro]
        return existing([*LINUX_SYSTEM_DIRS, *_LINUX_ETC, *_LINUX_SYS, *homes])

    def wrap(self, job_dir: Path) -> tuple[list[str], dict[str, str]]:
        job = Path(job_dir).resolve()
        env = ["--setenv", "PATH", self._path()]
        for name, value in (("HOME", str(job)), ("TMPDIR", str(job)), ("LANG", "C.UTF-8")):
            env += ["--setenv", name, value]
        if self.mpi_home is not None:
            # Cross-memory attach (process_vm_readv) is a ptrace-class call that
            # the new user namespace may refuse; shared memory copies still work.
            env += ["--setenv", "OMPI_MCA_btl_vader_single_copy_mechanism", "none"]
        argv = [
            self.bwrap,
            "--unshare-all",  # the new network namespace has only loopback
            "--die-with-parent",
            "--clearenv",
            *env,
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            *bwrap_ro(self._visible()),
            "--bind",
            str(job),
            str(job),
            "--chdir",
            str(job),
            "--",
        ]
        return argv, {"PATH": SYSTEM_PATH}


@dataclass(frozen=True)
class SeatbeltJobSandbox(_JobSandboxBase):
    """macOS: `sandbox-exec` with a deny-default profile generated per job."""

    kind: ClassVar[str] = "seatbelt"

    sandbox_exec: str = "/usr/bin/sandbox-exec"
    env: str = "/usr/bin/env"
    extra_exec: tuple[Path, ...] = ()  # stand-ins: their interpreter (and /usr/bin/env)

    def _homes(self) -> list[Path]:
        return [self.orca_home, *([self.mpi_home] if self.mpi_home else [])]

    def _visible(self) -> list[Path]:
        return resolved([*DARWIN_SYSTEM, *_DARWIN_ETC, *self._homes(), *self.extra_ro])

    def profile(self, job_dir: Path) -> str:
        job = Path(job_dir).resolve()
        read = resolved([*self._visible(), job])
        exec_dirs = resolved([*self._homes(), *self.extra_ro])
        exec_files = resolved([*_SHELLS, *self.extra_exec])
        rules = [
            ";; onoda ORCA job sandbox (A65); generated, do not edit",
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            sb_rule("allow process-exec", "subpath", exec_dirs),
            sb_rule("allow process-exec", "literal", exec_files),
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            *sb_sysctl_rules(),
            sb_rule("allow sysctl-read", "sysctl-name-prefix", _DARWIN_JOB_SYSCTL_PREFIXES),
            sb_rule("allow file-read-metadata", "literal", sb_ancestors(read)),
            SB_ROOT_LISTING,
            sb_rule("allow file-read*", "subpath", read),
            '(allow file-read* (subpath "/dev"))',
            sb_rule("allow file-write* file-ioctl", "literal", DARWIN_DEV_RW),
            '(allow file-write* (regex #"^/dev/fd/"))',
            sb_rule("allow file-write*", "subpath", resolved([job])),
            sb_rule("allow mach-lookup", "global-name", _DARWIN_MACH),
            "(deny network*)",
            # Last match wins: loopback (OpenMPI's ranks) and unix sockets in the job dir.
            '(allow network* (local ip "localhost:*"))',
            '(allow network* (remote ip "localhost:*"))',
            f"(allow network* (subpath {sb_str(job)}))",
        ]
        return "\n".join(r for r in rules if r) + "\n"

    def wrap(self, job_dir: Path) -> tuple[list[str], dict[str, str]]:
        job = Path(job_dir).resolve()
        argv = [
            self.env,
            "-i",
            f"PATH={self._path()}",
            f"HOME={job}",
            f"TMPDIR={job}",
            "LANG=en_US.UTF-8",
            self.sandbox_exec,
            "-p",
            self.profile(job),
        ]
        return argv, {"PATH": SYSTEM_PATH}


JobSandbox = BwrapJobSandbox | SeatbeltJobSandbox


def detect_job_sandbox(
    binary: str | Path, *, mpi_home: str | Path | None = None, stand_in: bool = False
) -> JobSandbox:
    """This host's job sandbox for `binary` (unchecked), or `CapabilityError` saying why none.

    ORCA_HOME is the binary's directory. `stand_in` exposes this Python
    interpreter too, for labelled stand-in scripts; never for real ORCA.
    """
    orca_home = Path(binary).resolve().parent
    mpi = Path(mpi_home).resolve() if mpi_home is not None else None
    if mpi is not None and not mpi.is_dir():
        raise CapabilityError(f"MPI home {mpi_home!r} is not a directory")
    extra_ro = _python_dirs() if stand_in else ()
    if sys.platform == "darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            raise CapabilityError("the ORCA job sandbox needs Seatbelt (`sandbox-exec`) (A65)")
        extra_exec = (Path("/usr/bin/env"), Path(sys.executable)) if stand_in else ()
        return SeatbeltJobSandbox(
            orca_home=orca_home,
            mpi_home=mpi,
            extra_ro=extra_ro,
            sandbox_exec=sandbox_exec,
            extra_exec=extra_exec,
        )
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise CapabilityError("the ORCA job sandbox needs bubblewrap (`bwrap`) on PATH (A65)")
        return BwrapJobSandbox(orca_home=orca_home, mpi_home=mpi, extra_ro=extra_ro, bwrap=bwrap)
    raise CapabilityError(
        f"the ORCA job sandbox needs Linux or macOS (A65); this is {sys.platform}"
    )


def host_job_sandbox(
    binary: str | Path,
    *,
    scratch: Path,
    stand_in: bool,
    mpi_home: str | Path | None = None,
) -> JobSandbox | None:
    """A checked job sandbox for `binary`; None only for a stand-in on a host without one.

    Real ORCA never runs unconfined: without a working sandbox this raises
    `CapabilityError` (fail closed, A65).
    """
    try:
        sandbox = detect_job_sandbox(binary, mpi_home=mpi_home, stand_in=stand_in)
        sandbox.check(scratch)
    except CapabilityError:
        if stand_in:
            return None
        raise
    return sandbox
