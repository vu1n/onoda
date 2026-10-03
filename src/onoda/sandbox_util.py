"""Path and Seatbelt-profile helpers shared by the Pi sandbox and the ORCA job sandbox.

Neutral ground so `onoda.science` never imports `onoda.adapters` (and the
reverse): `adapters.pi.sandbox` (A50, A60) and `science.orca_sandbox` (A65)
both build their bind lists and SBPL profiles from these.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from onoda.errors import CapabilityError

# Read-only Linux system paths a sandboxed program needs; each is mounted only if it exists.
LINUX_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Read-only macOS system paths (symlinks resolved when a profile is written:
# /etc, /tmp and /var are links into /private on macOS). /System also holds
# the dyld shared cache (under /System/Volumes/Preboot).
DARWIN_SYSTEM = ("/usr", "/bin", "/sbin", "/System", "/private/var/select")
DARWIN_DEV_RW = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper")

# Listing "/" itself (its entry names, not anything under it). macOS 27's
# /bin/bash aborts at start-up (SIGABRT, no output) without it; seen on Vu's
# Mac 2026-10-03, where metadata-only access to "/" was not enough.
SB_ROOT_LISTING = '(allow file-read-data (literal "/"))'

# The sysctls a Seatbelt-sandboxed process may read: what python, bash and node
# ask at start-up (CPU, memory, OS release, host name, boot time, load). A
# blanket `sysctl-read` would include KERN_PROCARGS2 (`kern.procargs2`, what
# `ps -E` reads), which shows any same-uid process's argv and environment,
# credentials included; nothing under `kern.proc` is listed.
DARWIN_SYSCTL_PREFIXES = ("hw.", "machdep.cpu.", "sysctl.proc_")
DARWIN_SYSCTL_NAMES = (
    "kern.argmax",
    "kern.boottime",
    "kern.hostname",
    "kern.maxfiles",
    "kern.maxfilesperproc",
    "kern.maxproc",
    "kern.ngroups",
    "kern.osproductversion",
    "kern.osproductversioncompat",
    "kern.osrelease",
    "kern.ostype",
    "kern.osvariant_status",
    "kern.osversion",
    "kern.secure_kernel",
    "kern.usrstack64",
    "kern.version",
    "vm.loadavg",
    "vm.swapusage",
)


def covering(paths: Iterable[Path]) -> list[Path]:
    """Paths deduplicated, dropping any already covered by another."""
    out: list[Path] = []
    for p in sorted({Path(x) for x in paths}, key=lambda q: (len(q.parts), str(q))):
        if not any(p == q or q in p.parents for q in out):
            out.append(p)
    return out


def existing(paths: Iterable[str | Path]) -> list[Path]:
    """Existing paths, deduplicated, dropping any already covered by another."""
    return covering(Path(x) for x in paths if Path(x).exists())


def bwrap_ro(paths: Iterable[Path]) -> list[str]:
    """`--ro-bind p p` for each path."""
    return [a for p in paths for a in ("--ro-bind", str(p), str(p))]


def sb_str(value: str | Path) -> str:
    """`value` as an SBPL string literal; a control character is refused, not escaped."""
    text = str(value)
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise CapabilityError(f"a Seatbelt profile path holds a control character: {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _symlink_hops(path: Path) -> list[Path]:
    """`path` rewritten once per symlink met while resolving it, the last fully resolved.

    Seatbelt checks every link it follows: uv's `.venv/bin/python` goes through
    `cpython-3.11-macos-aarch64-none` (a link) to `cpython-3.11.15`, and exec
    fails unless the intermediate link is named too.
    """
    hops, cur = [path], path
    for _ in range(40):  # the kernel's own symlink-loop limit
        for i in range(1, len(cur.parts) + 1):
            prefix = Path(*cur.parts[:i])
            if prefix.is_symlink():
                target = prefix.parent / os.readlink(prefix)
                cur = Path(os.path.normpath(target.joinpath(*cur.parts[i:])))
                hops.append(cur)
                break
        else:
            break
    return [*hops, path.resolve()]


def resolved(paths: Iterable[str | Path]) -> list[Path]:
    """Each path and every symlink hop on the way to its target (all are named)."""
    out: list[Path] = []
    for p in paths:
        out += _symlink_hops(Path(p).absolute())
    return covering(out)


def sb_rule(action: str, filt: str, values: Iterable[str | Path]) -> str:
    """`(action (filt "v1") ...)`, or nothing when there are no values."""
    items = [f"({filt} {sb_str(v)})" for v in values]
    return f"({action}\n  " + "\n  ".join(items) + ")" if items else ""


def sb_sysctl_rules() -> list[str]:
    """The sysctl-read allowlist (`DARWIN_SYSCTL_*`) as SBPL rules."""
    return [
        sb_rule("allow sysctl-read", "sysctl-name-prefix", DARWIN_SYSCTL_PREFIXES),
        sb_rule("allow sysctl-read", "sysctl-name", DARWIN_SYSCTL_NAMES),
    ]


def sb_ancestors(paths: Iterable[Path]) -> list[Path]:
    """Ancestors of `paths` that are not themselves listed: resolving a path stats each."""
    listed = set(paths)
    return sorted({a for p in listed for a in p.parents} - listed)
