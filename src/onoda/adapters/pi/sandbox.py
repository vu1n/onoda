"""The Pi sandbox (A38, A50): a bubblewrap backend and a macOS Seatbelt one.

`Sandbox` is the bubblewrap backend (Linux); `SeatbeltSandbox` (below) gives
the same two layers on macOS and says where its guarantees differ.

Two layers, both built from allowlisted bind mounts (nothing of the host is
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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from onoda.errors import CapabilityError

# The bwrap backend's kind, as runs record it; each backend also carries `kind`.
SANDBOX_KIND = "bwrap"

# Read-only system paths both layers need; each is mounted only if it exists.
# /etc is mounted file by file: the whole directory is not needed and may hold secrets.
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
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
_INNER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _covering(paths: Iterable[Path]) -> list[Path]:
    """Paths deduplicated, dropping any already covered by another."""
    out: list[Path] = []
    for p in sorted({Path(x) for x in paths}, key=lambda q: (len(q.parts), str(q))):
        if not any(p == q or q in p.parents for q in out):
            out.append(p)
    return out


def _existing(paths: list[Path]) -> list[Path]:
    """Existing paths, deduplicated, dropping any already covered by another."""
    return _covering(Path(x) for x in paths if Path(x).exists())


def _host_paths(runtime_dir: Path, kernel_dir: Path, extra_ro: tuple[Path, ...]) -> dict[str, Any]:
    """The host paths both backends expose read-only (node, runtime, Python, kernel)."""
    node = shutil.which("node")
    if node is None:
        raise CapabilityError("no `node` on PATH")
    runtime = Path(runtime_dir)
    return {
        "runtime_dirs": (runtime.resolve(), (runtime / "node_modules").resolve()),
        "node_prefix": Path(node).resolve().parent.parent,
        "python_dirs": (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()),
        "kernel_dir": Path(kernel_dir).resolve(),
        "extra_ro": tuple(Path(p).resolve() for p in extra_ro),
    }


def _ro(paths: list[Path]) -> list[str]:
    return [a for p in paths for a in ("--ro-bind", str(p), str(p))]


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
        return [Path(p) for p in (*_SYSTEM_DIRS, *_ETC)]

    def _common(self) -> list[str]:
        return ["--die-with-parent", "--dev", "/dev", "--tmpfs", "/tmp"]

    def outer_argv(self, *, pi_work_dir: Path, bundle_dir: Path) -> list[str]:
        """The `bwrap` prefix for the Pi process; the Pi argv follows it."""
        ro = _existing(
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
            *_ro(ro),
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
        ro = _existing([*self._system(), *self.python_dirs, self.kernel_dir])
        return [
            self.bwrap,
            "--unshare-all",
            "--clearenv",
            "--setenv",
            "PATH",
            _INNER_PATH,
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
            *_ro(ro),
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
    path.write_text(f"#!/bin/sh\nexec {' '.join(words)}{tail}\n")
    path.chmod(0o555)


def write_settings(agent_dir: Path, settings: dict[str, str]) -> None:
    (Path(agent_dir) / "settings.json").write_text(json.dumps(settings, sort_keys=True))


# ------------------------------------------------------------------ Seatbelt

SEATBELT_KIND = "seatbelt"

# Read-only system paths both Seatbelt layers need (symlinks resolved when the
# profile is written: /etc, /tmp and /var are links into /private on macOS).
# /System also holds the dyld shared cache (under /System/Volumes/Preboot).
_DARWIN_SYSTEM = ("/usr", "/bin", "/sbin", "/System", "/private/var/select")
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
_DARWIN_DEV_RW = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper")
# Only the outer layer looks anything up over Mach: user and DNS lookups,
# notifications and logging that node needs to start and reach its provider.
# The inner layer gets none, so no system service can carry its traffic out.
_OUTER_MACH = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.notification_center",
    "com.apple.system.logger",
    "com.apple.logd",
    "com.apple.diagnosticd",
    "com.apple.dnssd.service",
    "com.apple.SystemConfiguration.configd",
)
_DARWIN_LANG = "en_US.UTF-8"  # macOS has no C.UTF-8 locale on every release


def _sb_str(value: str | Path) -> str:
    """`value` as an SBPL string literal; a control character is refused, not escaped."""
    text = str(value)
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise CapabilityError(f"a Seatbelt profile path holds a control character: {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _resolved(paths: Iterable[str | Path]) -> list[Path]:
    """Each path and, if it is a symlink somewhere, its target too (both are named)."""
    out: list[Path] = []
    for p in paths:
        out += [Path(p), Path(p).resolve()]
    return _covering(out)


def _rule(action: str, filt: str, values: Iterable[str | Path]) -> str:
    """`(action (filt "v1") ...)`, or nothing when there are no values."""
    items = [f"({filt} {_sb_str(v)})" for v in values]
    return f"({action}\n  " + "\n  ".join(items) + ")" if items else ""


@dataclass(frozen=True)
class SeatbeltSandbox:
    """The macOS backend: the same two layers as `Sandbox`, as Seatbelt profiles.

    Each layer runs under `sandbox-exec` with a profile generated for the run
    that starts from `(deny default)` and allows only what is named here:

    - **Outer** (the whole Pi process tree): reads of the system directories
      (`/usr`, `/bin`, `/sbin`, `/System`, selected `/etc` files), the pinned
      runtime, node, the Python environment, the RLM kernel dir, CA files and
      the task bundle; writes only to the run's Pi work dir, whose `tmp` is the
      private TMPDIR and HOME; the wrappers and profiles under `sandbox/` stay
      read-only. The network is open, because Pi has to reach its provider.
      The agent store, the evaluator store, the optimizer's run dir, other
      runs' work dirs and the repository are not readable.
    - **Inner** (Pi's `bash` tool, so `then_run`, and the RLM kernel): every
      network operation denied, an environment cleared by `env -i` before
      `sandbox-exec` (no credential, no proxy), the workspace and a private
      `inner-tmp` read-write, the bundle read-only, and nothing of Pi's spool,
      session, agent, sandbox or RLM directories, nor any broker socket in the
      work dir (none is named in the profile, and the default is deny). The
      kernel additionally reaches the outer `tmp`, where the extension's
      socket lives, as with bwrap's shared `/tmp`; it may connect to unix
      sockets there and nowhere else. No Mach service is reachable from it.
    - **File tools** and the `settings.json` wiring are exactly `Sandbox`'s.

    What Seatbelt cannot give that bwrap does: there is no PID, IPC or UTS
    namespace, so both layers see the host's process list (`ps`), though
    signals reach only processes of the same sandbox; macOS has no `/proc`, so
    there is no `/proc` to empty, and a process's environment is readable only
    through `sysctl` (which shows it for same-uid processes); nothing is hidden
    by mounts, so a path's *existence* is visible on its ancestors' metadata
    (the profile allows metadata reads of each allowed path's ancestors only).
    `sandbox-exec` is deprecated by Apple but still functional; if a macOS
    release removes it, `detect` refuses rather than running unsandboxed.
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
        """The outer layer's private TMPDIR and HOME (macOS has no tmpfs)."""
        return Path(pi_work_dir).resolve() / "tmp"

    @staticmethod
    def inner_tmp_dir(pi_work_dir: Path) -> Path:
        """The inner layer's TMPDIR and HOME, shared by its commands within the run."""
        return Path(pi_work_dir).resolve() / "inner-tmp"

    def _profile(self, *, layer: str, read: list[Path], write: list[Path], tail: list[str]) -> str:
        """A deny-default profile reading `read` (and system paths), writing `write`."""
        read = _resolved([*_DARWIN_SYSTEM, *_DARWIN_ETC, *read, *write])
        write = _resolved(write)
        # Resolving a path (node's realpath, exec) stats each of its ancestors.
        ancestors = sorted({a for p in read for a in p.parents} - set(read))
        rules = [
            f";; onoda Pi sandbox, {layer} layer (A50); generated, do not edit",
            "(version 1)",
            "(deny default)",
            "(allow process-exec process-fork)",
            "(allow signal (target same-sandbox))",
            "(allow process-info* (target same-sandbox))",
            "(allow sysctl-read)",
            _rule("allow file-read-metadata", "literal", ancestors),
            _rule("allow file-read*", "subpath", read),
            '(allow file-read* (subpath "/dev"))',
            _rule("allow file-write* file-ioctl", "literal", _DARWIN_DEV_RW),
            '(allow file-write* (regex #"^/dev/fd/"))',
            _rule("allow file-write*", "subpath", write),
            *tail,
        ]
        return "\n".join(r for r in rules if r) + "\n"

    def outer_profile(self, *, pi_work_dir: Path, bundle_dir: Path) -> str:
        """The outer layer's profile: system, runtime and bundle read, work dir written."""
        pi_work_dir, bundle_dir = Path(pi_work_dir).resolve(), Path(bundle_dir).resolve()
        read = [
            *self.runtime_dirs,
            self.node_prefix,
            *self.python_dirs,
            self.kernel_dir,
            *self.extra_ro,
            bundle_dir,
        ]
        tail = [
            # The wrappers and profiles are the inner boundary: Pi must not rewrite them.
            f"(deny file-write* (subpath {_sb_str(pi_work_dir / 'sandbox')}))",
            "(allow network*)",
            "(allow system-socket)",
            _rule("allow mach-lookup", "global-name", _OUTER_MACH),
        ]
        return self._profile(layer="outer", read=read, write=[pi_work_dir], tail=tail)

    def inner_profile(
        self, *, pi_work_dir: Path, workspace: Path, bundle_dir: Path, kernel: bool
    ) -> str:
        """An inner layer's profile; `kernel` adds the outer tmp and its unix sockets."""
        workspace, bundle_dir = Path(workspace).resolve(), Path(bundle_dir).resolve()
        write = [workspace, self.inner_tmp_dir(pi_work_dir)]
        tail = ["(deny network*)"]
        if kernel:
            outer_tmp = self.tmp_dir(pi_work_dir)
            write.append(outer_tmp)
            # Last match wins: after the deny, only unix sockets under the outer tmp.
            tail.append(f"(allow network-outbound (subpath {_sb_str(outer_tmp)}))")
        read = [*self.python_dirs, self.kernel_dir, bundle_dir]
        return self._profile(layer="inner", read=read, write=write, tail=tail)

    def outer_argv(self, *, pi_work_dir: Path, bundle_dir: Path) -> list[str]:
        """The `sandbox-exec` prefix for the Pi process; the Pi argv follows it."""
        tmp = self.tmp_dir(pi_work_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        profile = self.outer_profile(pi_work_dir=pi_work_dir, bundle_dir=bundle_dir)
        return [self.env, f"TMPDIR={tmp}", f"HOME={tmp}", self.sandbox_exec, "-p", profile]

    def _inner(self, *, profile_path: Path, pi_work_dir: Path) -> list[str]:
        home = self.inner_tmp_dir(pi_work_dir)
        return [
            self.env,
            "-i",
            f"PATH={_INNER_PATH}",
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
