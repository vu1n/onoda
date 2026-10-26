"""The Pi sandbox (A38, A50): a bubblewrap backend and a macOS Seatbelt one.

`Sandbox` is the bubblewrap backend (Linux); `SeatbeltSandbox` (below) is
the macOS one, which keeps only the inner layer (macOS 27 refuses to nest
Seatbelt sandboxes, A60) and says where its guarantees differ.

With bwrap, two layers, both built from allowlisted bind mounts (nothing of the host is
visible unless it is named here):

- **Outer**: the whole Pi process tree (Pi, SoL-Pi, the RLM extension and its
  child Pi processes). It sees the system directories read-only, the pinned
  runtime and the Python environment read-only, the run's task bundle
  read-only, and its own Pi work directory read-write, with a private `/tmp`.
  The network is shared, because Pi has to reach its provider. The agent
  store, the evaluator store, the optimizer's run directory, other runs' work
  directories and the repository are not mounted.
- **Inner**: every command the agent runs (Pi's `bash` tool, so also Action
  Fusion's `then_run`) and the RLM kernel. It has no network, a cleared
  environment (no credential, no proxy), the workspace read-write and the
  bundle read-only, and nothing of Pi's spool, session, agent or RLM
  directories, and an empty `/proc`. The kernel additionally sees the outer
  `/tmp`, where its socket to the extension lives.
- **File tools**: Pi's own `read`, `edit` and `write` run in the Pi process,
  outside the inner layer, so the recorder's gateway blocks any whose path
  resolves (symlinks followed) outside the workspace, or for `read` also the
  bundle (`GatewayPolicy.read_roots`/`write_roots`).

The inner layer is wired through Pi's own settings: `shellPath` points at a
generated wrapper that runs `bash` inside `bwrap`, and the RLM extension's
interpreter variable points at a wrapper that runs the kernel the same way.
`defaultProjectTrust: "never"` stops a `.pi/settings.json` the agent writes
into its workspace from replacing either for a child Pi.

`Sandbox` needs Linux with `bwrap` on PATH, `SeatbeltSandbox` macOS with
`sandbox-exec`; `runtime.detect_sandbox` picks by platform. Anything else is
refused, never degraded.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from onoda.errors import CapabilityError
from onoda.sandbox_util import (
    DARWIN_DEV_RW,
    DARWIN_SYSTEM,
    LINUX_SYSTEM_DIRS,
    SB_ROOT_LISTING,
    SYSTEM_PATH,
    bwrap_ro,
    existing,
    resolved,
    sb_ancestors,
    sb_rule,
    sb_str,
    sb_sysctl_rules,
)

# The bwrap backend's kind, as runs record it; each backend also carries `kind`.
SANDBOX_KIND = "bwrap"

# Read-only system paths both layers need (`LINUX_SYSTEM_DIRS`, each mounted only if
# it exists). /etc is mounted file by file: the whole directory is not needed
# and may hold secrets.
_ETC = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/alternatives",
)


def _host_paths(runtime_dir: Path, kernel_dir: Path, extra_ro: tuple[Path, ...]) -> dict[str, Any]:
    """The host paths both backends expose read-only (node, runtime, Python, kernel)."""
    node = shutil.which("node")
    if node is None:
        raise CapabilityError("no `node` on PATH")
    runtime = Path(runtime_dir)
    return {
        "runtime_dirs": (runtime.resolve(), (runtime / "node_modules").resolve()),
        "node_prefix": Path(node).resolve().parent.parent,
        # The interpreter as invoked, unresolved: Seatbelt must name each link it
        # passes (uv's .venv/bin/python goes through a symlinked directory).
        "python_dirs": (
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            Path(sys.executable).absolute(),
        ),
        "kernel_dir": Path(kernel_dir).resolve(),
        "extra_ro": tuple(Path(p).resolve() for p in extra_ro),
    }


@dataclass(frozen=True)
class Sandbox:
    """Where `bwrap` is and which extra host paths the layers mount read-only."""

    kind: ClassVar[str] = SANDBOX_KIND

    bwrap: str
    runtime_dirs: tuple[Path, ...]  # pinned Pi runtime (and its node_modules target)
    node_prefix: Path  # the node installation Pi runs on
    python_dirs: tuple[Path, ...]  # the interpreter that runs the RLM kernel
    kernel_dir: Path  # directory holding the RLM kernel script
    extra_ro: tuple[Path, ...] = ()  # e.g. CA bundles a live run's TLS needs

    @classmethod
    def detect(
        cls, *, runtime_dir: Path, kernel_dir: Path, extra_ro: tuple[Path, ...] = ()
    ) -> Sandbox:
        """The sandbox on this host, or `CapabilityError` saying why there is none."""
        if not sys.platform.startswith("linux"):
            raise CapabilityError(f"the Pi sandbox needs Linux (A50); this is {sys.platform}")
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise CapabilityError("the Pi sandbox needs bubblewrap (`bwrap`) on PATH (A50)")
        return cls(bwrap=bwrap, **_host_paths(runtime_dir, kernel_dir, extra_ro))

    def _system(self) -> list[Path]:
        return [Path(p) for p in (*LINUX_SYSTEM_DIRS, *_ETC)]

    def _common(self) -> list[str]:
        return ["--die-with-parent", "--dev", "/dev", "--tmpfs", "/tmp"]

    def outer_argv(self, *, pi_work_dir: Path, bundle_dir: Path) -> list[str]:
        """The `bwrap` prefix for the Pi process; the Pi argv follows it."""
        ro = existing(
            [
                *self._system(),
                *self.runtime_dirs,
                self.node_prefix,
                *self.python_dirs,
                self.kernel_dir,
                *self.extra_ro,
            ]
        )
        pi_work_dir, bundle_dir = Path(pi_work_dir).resolve(), Path(bundle_dir).resolve()
        return [
            self.bwrap,
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            *self._common(),
            "--proc",
            "/proc",
            *bwrap_ro(ro),
            "--bind",
            str(pi_work_dir),
            str(pi_work_dir),
            "--ro-bind",
            str(bundle_dir),
            str(bundle_dir),
            # The wrappers are the inner boundary: Pi must not rewrite them.
            "--ro-bind",
            str(pi_work_dir / "sandbox"),
            str(pi_work_dir / "sandbox"),
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "HOME",
            "/tmp",
            "--",
        ]

    def _inner(self, *, workspace: Path, bundle_dir: Path, extra: list[str]) -> list[str]:
        ro = existing([*self._system(), *self.python_dirs, self.kernel_dir])
        return [
            self.bwrap,
            "--unshare-all",
            "--clearenv",
            "--setenv",
            "PATH",
            SYSTEM_PATH,
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "LANG",
            "C.UTF-8",
            *self._common(),
            # An empty /proc: a nested bwrap cannot mount a fresh one, and the outer
            # one would show Pi's process (and its environment) to the agent.
            "--tmpfs",
            "/proc",
            *bwrap_ro(ro),
            "--bind",
            str(workspace),
            str(workspace),
            "--ro-bind",
            str(bundle_dir),
            str(bundle_dir),
            *extra,
        ]

    def stage(self, *, pi_work_dir: Path, workspace: Path, bundle_dir: Path) -> tuple[Path, Path]:
        """Write the inner wrappers under `pi_work_dir/sandbox`; return (shell, python)."""
        d = Path(pi_work_dir) / "sandbox"
        d.mkdir(parents=True, exist_ok=True)
        workspace, bundle_dir = Path(workspace).resolve(), Path(bundle_dir).resolve()
        shell = self._inner(workspace=workspace, bundle_dir=bundle_dir, extra=[])
        # The kernel's socket lives in the outer /tmp (the extension's tmpdir()).
        python = self._inner(
            workspace=workspace, bundle_dir=bundle_dir, extra=["--bind", "/tmp", "/tmp"]
        )
        shell_path = d / "shell"
        python_path = d / "python"
        _write_wrapper(
            shell_path, [*shell, "--chdir", '"$PWD"', "--", "/bin/bash"], passthrough=True
        )
        _write_wrapper(
            python_path,
            [*python, "--chdir", '"$PWD"', "--", str(Path(sys.executable))],
            passthrough=True,
        )
        return shell_path, python_path

    @staticmethod
    def pi_settings(shell_path: Path) -> dict[str, str]:
        """The settings.json entries that route tool commands into the inner layer."""
        return {"shellPath": str(shell_path), "defaultProjectTrust": "never"}


def _write_wrapper(path: Path, argv: list[str], *, passthrough: bool) -> None:
    """A /bin/sh script that execs `argv` (words quoted, `"$PWD"` left live) plus its args."""
    words = [w if w == '"$PWD"' else shlex.quote(w) for w in argv]
    tail = ' "$@"' if passthrough else ""
    path.unlink(missing_ok=True)  # read-only (0555) from an earlier staging
    path.write_text(f"#!/bin/sh\nexec {' '.join(words)}{tail}\n")
    path.chmod(0o555)


def write_settings(agent_dir: Path, settings: dict[str, str]) -> None:
    (Path(agent_dir) / "settings.json").write_text(json.dumps(settings, sort_keys=True))


# ------------------------------------------------------------------ Seatbelt

SEATBELT_KIND = "seatbelt"

# Read-only paths the Seatbelt profiles need besides `DARWIN_SYSTEM`.
_DARWIN_ETC = (
    "/etc/ssl",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/services",
    "/etc/protocols",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/private/var/db/timezone",
)
_DARWIN_LANG = "en_US.UTF-8"  # macOS has no C.UTF-8 locale on every release


# one layer on macOS: inner commands confined, Pi's node process is not
@dataclass(frozen=True)
class SeatbeltSandbox:
    """The macOS backend: `Sandbox`'s inner layer as Seatbelt profiles; no outer layer.

    macOS has one layer, not two. A process under a deny-default Seatbelt
    profile cannot apply a second one: on macOS 27, `sandbox-exec` inside it
    fails with `sandbox_apply: Operation not permitted`, e.g.
    `sandbox-exec -p '(version 1)(deny default)(allow process-exec process-fork)
    (allow file-read*)' sandbox-exec -p '(version 1)(allow default)' /bin/echo x`.
    Pi has to start the inner wrappers, so the Pi process runs unconfined
    (`outer_argv` only sets its TMPDIR and HOME) and the inner layer stays:

    - **Inner** (Pi's `bash` tool, so `then_run`, and the RLM kernel): runs
      under `sandbox-exec` with a profile generated for the run that starts
      from `(deny default)`: every network operation denied, an environment
      cleared by `env -i` before `sandbox-exec` (no credential, no proxy),
      reads of the system directories, the Python environment, the kernel dir
      and the bundle, the workspace and a private `inner-tmp` read-write, and
      nothing of Pi's spool, session, agent, sandbox or RLM directories, nor
      any broker socket in the work dir (none is named in the profile, and the
      default is deny). The kernel additionally reaches the Pi process's `tmp`,
      where the extension's socket lives, as with bwrap's shared `/tmp`; it may
      connect to unix sockets there and nowhere else. No Mach service is
      reachable from it.
    - **File tools** and the `settings.json` wiring are exactly `Sandbox`'s:
      the recorder's gateway keeps Pi's `read`/`edit`/`write` inside the
      workspace (and, for `read`, the bundle), so they cannot rewrite the
      wrappers and profiles under `sandbox/`, which are also staged read-only.

    What the missing outer layer gives up: Pi's node process itself (with
    SoL-Pi, the extensions and RLM child Pi processes) is not confined on
    macOS; it can reach any host and read and write the host filesystem as
    the user, and sees the stores and the repository. That code is pinned
    (`pi/package-lock.json`); the agent reaches files only through the
    gateway-checked tools and runs commands only in the inner layer.

    What Seatbelt cannot give the inner layer that bwrap does: there is no
    PID, IPC or UTS namespace, so it sees the host's process list (`ps`),
    though signals reach only processes of the same sandbox; macOS has no
    `/proc`, so there is no `/proc` to empty, and a process's environment is
    readable only through `sysctl` (`kern.procargs2`, which shows it for
    same-uid processes), so the profiles read only an allowlist of sysctls
    without it; nothing is hidden by mounts, so a path's *existence* is
    visible on its ancestors' metadata (the profile allows metadata reads of
    each allowed path's ancestors only). `sandbox-exec` is deprecated by Apple
    but still functional; if a macOS release removes it, `detect` refuses
    rather than running unsandboxed.
    """

    kind: ClassVar[str] = SEATBELT_KIND

    sandbox_exec: str
    runtime_dirs: tuple[Path, ...]
    node_prefix: Path
    python_dirs: tuple[Path, ...]
    kernel_dir: Path
    extra_ro: tuple[Path, ...] = ()
    env: str = "/usr/bin/env"

    @classmethod
    def detect(
        cls, *, runtime_dir: Path, kernel_dir: Path, extra_ro: tuple[Path, ...] = ()
    ) -> SeatbeltSandbox:
        """The sandbox on this host, or `CapabilityError` saying why there is none."""
        if sys.platform != "darwin":
            raise CapabilityError(f"the Seatbelt sandbox needs macOS (A50); this is {sys.platform}")
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            raise CapabilityError("the Pi sandbox needs Seatbelt (`sandbox-exec`) on PATH (A50)")
        return cls(sandbox_exec=sandbox_exec, **_host_paths(runtime_dir, kernel_dir, extra_ro))

    @staticmethod
    def tmp_dir(pi_work_dir: Path) -> Path:
        """The Pi process's private TMPDIR and HOME (macOS has no tmpfs)."""
        return Path(pi_work_dir).resolve() / "tmp"

    @staticmethod
    def inner_tmp_dir(pi_work_dir: Path) -> Path:
        """The inner layer's TMPDIR and HOME, shared by its commands within the run."""
        return Path(pi_work_dir).resolve() / "inner-tmp"

    def _profile(self, *, layer: str, read: list[Path], write: list[Path], tail: list[str]) -> str:
        """A deny-default profile reading `read` (and system paths), writing `write`."""
        read = resolved([*DARWIN_SYSTEM, *_DARWIN_ETC, *read, *write])
        write = resolved(write)
        # Resolving a path (node's realpath, exec) stats each of its ancestors.
        ancestors = sb_ancestors(read)
        rules = [
            f";; onoda Pi sandbox, {layer} layer (A50); generated, do not edit",
            "(version 1)",
            "(deny default)",
            "(allow process-exec process-fork)",
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            *sb_sysctl_rules(),
            sb_rule("allow file-read-metadata", "literal", ancestors),
            SB_ROOT_LISTING,
            sb_rule("allow file-read*", "subpath", read),
            '(allow file-read* (subpath "/dev"))',
            sb_rule("allow file-write* file-ioctl", "literal", DARWIN_DEV_RW),
            '(allow file-write* (regex #"^/dev/fd/"))',
            sb_rule("allow file-write*", "subpath", write),
            *tail,
        ]
        return "\n".join(r for r in rules if r) + "\n"

    def inner_profile(
        self, *, pi_work_dir: Path, workspace: Path, bundle_dir: Path, kernel: bool
    ) -> str:
        """An inner layer's profile; `kernel` adds Pi's tmp and its unix sockets."""
        workspace, bundle_dir = Path(workspace).resolve(), Path(bundle_dir).resolve()
        write = [workspace, self.inner_tmp_dir(pi_work_dir)]
        tail = ["(deny network*)"]
        if kernel:
            outer_tmp = self.tmp_dir(pi_work_dir)
            write.append(outer_tmp)
            # Last match wins: after the deny, only unix sockets under Pi's tmp.
            tail.append(f"(allow network-outbound (subpath {sb_str(outer_tmp)}))")
        read = [*self.python_dirs, self.kernel_dir, bundle_dir]
        return self._profile(layer="inner", read=read, write=write, tail=tail)

    def outer_argv(self, *, pi_work_dir: Path, bundle_dir: Path) -> list[str]:
        """The Pi process's prefix: only its TMPDIR and HOME, no Seatbelt (A60).

        macOS 27 refuses a second sandbox inside a deny-default one, and Pi must
        start the inner wrappers, so Pi itself runs unconfined; see the class.
        """
        tmp = self.tmp_dir(pi_work_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        return [self.env, f"TMPDIR={tmp}", f"HOME={tmp}"]

    def _inner(self, *, profile_path: Path, pi_work_dir: Path) -> list[str]:
        home = self.inner_tmp_dir(pi_work_dir)
        return [
            self.env,
            "-i",
            f"PATH={SYSTEM_PATH}",
            f"HOME={home}",
            f"TMPDIR={home}",
            f"LANG={_DARWIN_LANG}",
            self.sandbox_exec,
            "-f",
            str(profile_path),
        ]

    def stage(self, *, pi_work_dir: Path, workspace: Path, bundle_dir: Path) -> tuple[Path, Path]:
        """Write the inner profiles and wrappers under `pi_work_dir/sandbox`; (shell, python)."""
        d = Path(pi_work_dir).resolve() / "sandbox"
        d.mkdir(parents=True, exist_ok=True)
        for t in (self.tmp_dir(pi_work_dir), self.inner_tmp_dir(pi_work_dir)):
            t.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, kernel, program in (
            ("shell", False, "/bin/bash"),
            ("python", True, str(Path(sys.executable))),
        ):
            profile_path = d / f"{name}.sb"
            profile_path.unlink(missing_ok=True)  # read-only from an earlier staging
            profile_path.write_text(
                self.inner_profile(
                    pi_work_dir=pi_work_dir, workspace=workspace, bundle_dir=bundle_dir,
                    kernel=kernel,
                )
            )  # fmt: skip
            profile_path.chmod(0o444)
            paths[name] = d / name
            _write_wrapper(
                paths[name],
                [*self._inner(profile_path=profile_path, pi_work_dir=pi_work_dir), program],
                passthrough=True,
            )
        return paths["shell"], paths["python"]

    @staticmethod
    def pi_settings(shell_path: Path) -> dict[str, str]:
        """The settings.json entries that route tool commands into the inner layer."""
        return Sandbox.pi_settings(shell_path)


# Either backend; `runtime.detect_sandbox` picks the one this host supports.
PiSandbox = Sandbox | SeatbeltSandbox
