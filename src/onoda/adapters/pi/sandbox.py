"""A bubblewrap sandbox for Pi runs (A38, A50).

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

Linux with `bwrap` on PATH only. Anything else is refused, never degraded.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from onoda.errors import CapabilityError

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


def _existing(paths: list[Path]) -> list[Path]:
    """Existing paths, deduplicated, dropping any already covered by another."""
    out: list[Path] = []
    for p in sorted({Path(x) for x in paths if Path(x).exists()}, key=lambda q: len(q.parts)):
        if not any(p == q or q in p.parents for q in out):
            out.append(p)
    return out


def _ro(paths: list[Path]) -> list[str]:
    return [a for p in paths for a in ("--ro-bind", str(p), str(p))]


@dataclass(frozen=True)
class Sandbox:
    """Where `bwrap` is and which extra host paths the layers mount read-only."""

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
        node = shutil.which("node")
        if node is None:
            raise CapabilityError("no `node` on PATH")
        node_prefix = Path(node).resolve().parent.parent
        runtime = Path(runtime_dir)
        runtime_dirs = (runtime.resolve(), (runtime / "node_modules").resolve())
        python_dirs = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
        return cls(
            bwrap=bwrap,
            runtime_dirs=runtime_dirs,
            node_prefix=node_prefix,
            python_dirs=python_dirs,
            kernel_dir=Path(kernel_dir).resolve(),
            extra_ro=tuple(Path(p).resolve() for p in extra_ro),
        )

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
