"""A50 on macOS: the Seatbelt backend of the Pi sandbox.

The unit tests generate profiles and wrappers and pick a backend; they run on
any host. The darwin tests run the generated wrappers under the real
`sandbox-exec` and skip elsewhere. Every probe is a synthetic fixture.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_pi_runtime import SCRIPT, make_bundle

from onoda.adapters.pi import runtime
from onoda.adapters.pi.contract import GatewayPolicy
from onoda.adapters.pi.runtime import build_launch, detect_sandbox
from onoda.adapters.pi.sandbox import (
    SANDBOX_KIND,
    Sandbox,
    SeatbeltSandbox,
)
from onoda.errors import CapabilityError
from onoda.sandbox_util import resolved
from onoda.sandbox_util import sb_str as _sb_str

on_darwin = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")


def stub(tmp_path: Path) -> SeatbeltSandbox:
    return SeatbeltSandbox(
        sandbox_exec="/usr/bin/sandbox-exec",
        runtime_dirs=(tmp_path / "runtime",),
        node_prefix=tmp_path / "node",
        python_dirs=(tmp_path / "python",),
        kernel_dir=tmp_path / "kernel",
    )


def staged(tmp_path: Path, sb: SeatbeltSandbox | None = None):
    sb = sb or stub(tmp_path)
    work = tmp_path / "work"
    workspace = work / "workspace"
    for d in (workspace, work / "spool", work / "sessions", work / "agent", work / "rlm"):
        d.mkdir(parents=True)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shell, python = sb.stage(pi_work_dir=work, workspace=workspace, bundle_dir=bundle)
    return sb, work.resolve(), workspace.resolve(), bundle.resolve(), shell, python


def balanced(profile: str) -> bool:
    depth = 0
    for tok in re.sub(r'"(\\.|[^"\\])*"|#"[^"]*"', '""', profile):
        depth += {"(": 1, ")": -1}.get(tok, 0)
        if depth < 0:
            return False
    return depth == 0


# ------------------------------------------------------------------ units


def test_each_backend_names_its_kind():
    assert Sandbox.kind == SANDBOX_KIND == "bwrap"
    assert SeatbeltSandbox.kind == "seatbelt"


def test_detect_refuses_a_host_without_seatbelt(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(CapabilityError, match="macOS"):
        SeatbeltSandbox.detect(runtime_dir=tmp_path, kernel_dir=tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(CapabilityError, match="sandbox-exec"):
        SeatbeltSandbox.detect(runtime_dir=tmp_path, kernel_dir=tmp_path)


def test_detect_sandbox_picks_the_backend_by_platform(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sys, "platform", "linux")
    linux = detect_sandbox()
    assert isinstance(linux, Sandbox) and linux.bwrap == "/usr/bin/bwrap"
    monkeypatch.setattr(sys, "platform", "darwin")
    mac = detect_sandbox()
    assert isinstance(mac, SeatbeltSandbox) and mac.sandbox_exec == "/usr/bin/sandbox-exec"
    assert mac.kernel_dir == runtime.RLM_KERNEL.parent.resolve()
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(CapabilityError, match="Linux .* or macOS"):
        detect_sandbox()


def test_the_outer_argv_sets_tmp_and_home_and_applies_no_seatbelt(tmp_path):
    """macOS 27 cannot nest Seatbelt (A60): Pi runs with only its TMPDIR and HOME set."""
    sb, work, _, bundle, _, _ = staged(tmp_path)
    outer = sb.outer_argv(pi_work_dir=work, bundle_dir=bundle)
    tmp = work / "tmp"
    assert outer == ["/usr/bin/env", f"TMPDIR={tmp}", f"HOME={tmp}"]
    assert not any("sandbox-exec" in w for w in outer) and "-p" not in outer
    assert tmp.is_dir()
    assert not hasattr(sb, "outer_profile")


def test_the_wrappers_and_profiles_are_staged_read_only(tmp_path):
    """With no outer layer, nothing but the gateway and these modes guards `sandbox/`."""
    sb, work, workspace, bundle, _, _ = staged(tmp_path)
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in (work / "sandbox").iterdir()}
    assert modes == {"shell": 0o555, "python": 0o555, "shell.sb": 0o444, "python.sb": 0o444}
    # Restaging over the read-only files still works and leaves them read-only.
    sb.stage(pi_work_dir=work, workspace=workspace, bundle_dir=bundle)
    again = {p.name: stat.S_IMODE(p.stat().st_mode) for p in (work / "sandbox").iterdir()}
    assert again == modes


def test_the_inner_profiles_have_no_network_and_only_the_workspace(tmp_path):
    sb, work, workspace, bundle, shell, python = staged(tmp_path)
    shell_sb = (work / "sandbox" / "shell.sb").read_text()
    kernel_sb = (work / "sandbox" / "python.sb").read_text()
    for profile in (shell_sb, kernel_sb):
        assert balanced(profile)
        assert "(deny default)" in profile and "(deny network*)" in profile
        assert "(allow network*)" not in profile and "mach-lookup" not in profile
        assert f'(subpath "{workspace}")' in profile and f'(subpath "{bundle}")' in profile
        for hidden in ("spool", "sessions", "agent", "rlm", "sandbox"):
            assert str(work / hidden) not in profile
        # Read-write only the workspace and the private tmp dirs.
        (rw,) = re.findall(r'\(allow file-write\*\n((?:  \(subpath "[^"]*"\)\n?)+)\)', profile)
        assert set(re.findall(r'"([^"]*)"', rw)) <= {
            str(workspace),
            str(work / "inner-tmp"),
            str(work / "tmp"),
        }
    # Only the work dir itself is named, as metadata of an ancestor, never its contents.
    assert f'(literal "{work}")' in shell_sb and f'(subpath "{work}")' not in shell_sb
    assert "network-outbound" not in shell_sb and str(work / "tmp") not in shell_sb
    # The kernel reaches unix sockets under the outer tmp, after the deny.
    allow = f'(allow network-outbound (subpath "{work / "tmp"}"))'
    assert kernel_sb.index(allow) > kernel_sb.index("(deny network*)")


def test_the_wrappers_clear_the_environment_before_sandbox_exec(tmp_path):
    _, work, _, _, shell, python = staged(tmp_path)
    assert shell == work / "sandbox" / "shell" and os.access(shell, os.X_OK)
    text = shell.read_text()
    assert text.startswith("#!/bin/sh\nexec /usr/bin/env -i PATH=")
    assert f"HOME={work / 'inner-tmp'}" in text and "LANG=en_US.UTF-8" in text
    assert f'/usr/bin/sandbox-exec -f {work / "sandbox" / "shell.sb"} /bin/bash "$@"' in text
    assert f"-f {work / 'sandbox' / 'python.sb'} {sys.executable}" in python.read_text()
    assert stat.S_IMODE((work / "sandbox" / "shell.sb").stat().st_mode) == 0o444


def test_profile_strings_are_escaped_and_control_characters_refused(tmp_path):
    assert _sb_str('/a "b"\\c') == '"/a \\"b\\"\\\\c"'
    with pytest.raises(CapabilityError, match="control character"):
        _sb_str("/a\nb")
    odd = tmp_path / 'we"ird\\dir'
    sb, work, workspace, bundle, _, _ = staged(odd)
    profile = sb.inner_profile(pi_work_dir=work, workspace=workspace, bundle_dir=bundle,
                               kernel=True)  # fmt: skip
    assert balanced(profile)
    assert _sb_str(work) in profile and str(work) not in profile.replace(_sb_str(work), "")


def test_a_seatbelt_launch_routes_tools_into_the_inner_layer(tmp_path):
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=make_bundle(tmp_path),
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
        sandbox=stub(tmp_path),
    )
    work = (tmp_path / "work").resolve()
    tmp = work / "tmp"
    assert list(launch_obj.argv[:3]) == ["/usr/bin/env", f"TMPDIR={tmp}", f"HOME={tmp}"]
    assert launch_obj.argv[3].endswith("/node_modules/.bin/pi")
    assert "/usr/bin/sandbox-exec" not in launch_obj.argv
    settings = json.loads((launch_obj.agent_dir / "settings.json").read_text())
    assert settings == {
        "shellPath": str(work / "sandbox" / "shell"),
        "defaultProjectTrust": "never",
    }
    # Pi is unconfined on macOS, so its file tools must not reach the wrappers:
    # the gateway writes only under the workspace, a sibling of `sandbox/`.
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert policy.write_roots == (str(work / "workspace"),)
    assert policy.read_roots == (str(work / "workspace"), str((tmp_path / "bundle").resolve()))
    for root in (*policy.write_roots, *policy.read_roots):
        assert not (work / "sandbox").is_relative_to(root)
        assert not Path(root).is_relative_to(work / "sandbox")


SECRET_SYSCTLS = ("kern.procargs2", "kern.procargs", "kern.proc.pid.1", "kern.proc.all")


def sysctl_filters(profile: str) -> list[tuple[str, str]]:
    """Every (filter, value) under an `(allow sysctl-read ...)` rule."""
    out = []
    for body in re.findall(r"\(allow sysctl-read((?:\n?\s*\(sysctl-name[^)]*\))*)\)", profile):
        out += re.findall(r'\((sysctl-name(?:-prefix)?) "([^"]*)"\)', body)
    return out


def test_profiles_read_only_allowlisted_sysctls(tmp_path):
    """No blanket sysctl-read: KERN_PROCARGS2 shows another process's environment."""
    _, work, _, _, _, _ = staged(tmp_path)
    profiles = [
        (work / "sandbox" / "shell.sb").read_text(),
        (work / "sandbox" / "python.sb").read_text(),
    ]
    for profile in profiles:
        assert balanced(profile)
        assert "(allow sysctl-read)" not in profile
        assert "sysctl*" not in profile and "sysctl-write" not in profile
        filters = sysctl_filters(profile)
        # Every line that mentions a sysctl is one of the allowlist rules.
        for line in profile.splitlines():
            if "sysctl" in line:
                assert re.fullmatch(
                    r'\(allow sysctl-read|\s+\(sysctl-name(-prefix)? "[^"]*"\)\)?', line
                ), line
        names = {v for f, v in filters if f == "sysctl-name"}
        prefixes = {v for f, v in filters if f == "sysctl-name-prefix"}
        assert {"hw.", "machdep.cpu."} <= prefixes
        assert {"kern.ostype", "kern.osrelease", "kern.osversion", "kern.version"} <= names
        assert "kern.hostname" in names
        for secret in SECRET_SYSCTLS:
            assert secret not in names
            assert not any(secret.startswith(p) for p in prefixes), secret


def root_reads(profile: str) -> list[str]:
    """Every rule that names "/" itself or everything under it."""
    return [ln for ln in profile.splitlines() if '"/"' in ln or '(subpath "/")' in ln]


def test_profiles_list_the_root_directory_and_read_nothing_else_there(tmp_path):
    """macOS 27's bash aborts without listing "/"; that is all "/" itself grants."""
    _, work, _, _, _, _ = staged(tmp_path)
    profiles = [
        (work / "sandbox" / "shell.sb").read_text(),
        (work / "sandbox" / "python.sb").read_text(),
    ]
    for profile in profiles:
        assert '(allow file-read-data (literal "/"))' in profile.splitlines()
        assert '(subpath "/")' not in profile
        for line in root_reads(profile):
            assert line.strip() in ('(allow file-read-data (literal "/"))', '(literal "/")'), line


# ------------------------------------------------------------ darwin only


@pytest.fixture
def mac(tmp_path):
    sb = runtime.detect_sandbox()
    assert isinstance(sb, SeatbeltSandbox)
    return staged(tmp_path, sb)


def inner(shell: Path, workspace: Path, command: str, **env: str):
    return subprocess.run(
        [str(shell), "-c", command], cwd=workspace, capture_output=True, text=True,
        env={**os.environ, **env}, timeout=60,
    )  # fmt: skip


@on_darwin
def test_darwin_inner_shell_has_no_network(mac):
    _, _, workspace, _, shell, _ = mac
    curl = inner(shell, workspace, "curl -sS --max-time 5 https://pypi.org/simple/ >/dev/null")
    assert curl.returncode != 0
    probe = (
        f"{sys.executable} -c 'import socket; s = socket.socket();"
        ' s.settimeout(5); s.connect(("1.1.1.1", 443))\''
    )
    py = inner(shell, workspace, probe)
    assert py.returncode != 0 and ("PermissionError" in py.stderr or "not permitted" in py.stderr)


@on_darwin
def test_darwin_inner_shell_reads_and_writes_only_where_allowed(mac, tmp_path):
    _, work, workspace, bundle, shell, _ = mac
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret\n")
    (work / "spool" / "events.jsonl").write_text("{}\n")
    (bundle / "task.txt").write_text("task\n")
    assert inner(shell, workspace, f"cat {outside}").returncode != 0
    assert inner(shell, workspace, f"cat {work / 'spool' / 'events.jsonl'}").returncode != 0
    assert inner(shell, workspace, f"ls {Path.home()}").returncode != 0
    assert inner(shell, workspace, f"touch {bundle / 'x'}").returncode != 0
    assert inner(shell, workspace, f"touch {work / 'sandbox' / 'shell'}").returncode != 0
    assert inner(shell, workspace, f"cat {bundle / 'task.txt'}").stdout == "task\n"
    ok = inner(shell, workspace, "echo hi > ok.txt && cat ok.txt")
    assert ok.returncode == 0, ok.stderr
    assert (workspace / "ok.txt").read_text() == "hi\n"


@on_darwin
def test_darwin_inner_shell_environment_is_cleared(mac):
    _, _, workspace, _, shell, _ = mac
    seen = inner(shell, workspace, "env", ONODA_PROBE_SECRET="sandbox-probe-secret",
                 OPENROUTER_API_KEY="sk-probe", HTTPS_PROXY="http://proxy.invalid")  # fmt: skip
    assert seen.returncode == 0, seen.stderr
    names = {line.split("=", 1)[0] for line in seen.stdout.splitlines() if "=" in line}
    assert not names & {"ONODA_PROBE_SECRET", "OPENROUTER_API_KEY", "HTTPS_PROXY"}
    assert "sandbox-probe-secret" not in seen.stdout
    assert len(names) < 10


@on_darwin
def test_darwin_kernel_reaches_only_its_unix_socket(mac):
    sb, work, workspace, _, _, python = mac
    path = str(work / "tmp" / "k.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.sendall(b"ready\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    cell = (
        "import socket\n"
        f"u = socket.socket(socket.AF_UNIX); u.connect({path!r}); print(u.recv(16).decode())\n"
        "t = socket.socket(); t.settimeout(5)\n"
        "try:\n    t.connect(('1.1.1.1', 443)); print('NET=open')\n"
        "except OSError:\n    print('NET=closed')\n"
    )
    out = subprocess.run([str(python), "-c", cell], cwd=workspace, capture_output=True,
                         text=True, timeout=60)  # fmt: skip
    server.close()
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["ready", "NET=closed"]


@on_darwin
def test_darwin_inner_wrapper_runs_under_outer_argv_and_hides_the_host(mac, tmp_path):
    """Pi starts the inner wrapper with `outer_argv`'s prefix: Seatbelt applies once, there."""
    sb, work, workspace, bundle, shell, _ = mac
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret\n")
    outer = sb.outer_argv(pi_work_dir=work, bundle_dir=bundle)
    ran = subprocess.run(
        [*outer, str(shell), "-c", "echo inner > n.txt"], cwd=workspace, capture_output=True,
        text=True, timeout=60,
    )  # fmt: skip
    assert ran.returncode == 0, ran.stderr
    assert (workspace / "n.txt").read_text() == "inner\n"
    hidden = subprocess.run(
        [*outer, str(shell), "-c", f"cat {outside}"], cwd=workspace, capture_output=True,
        text=True, timeout=60,
    )  # fmt: skip
    assert hidden.returncode != 0 and "host secret" not in hidden.stdout


@on_darwin
def test_darwin_inner_shell_cannot_read_another_process_environment(mac):
    """KERN_PROCARGS2 (what `ps -E` reads) is not on the sysctl allowlist."""
    _, _, workspace, _, shell, _ = mac
    marker = "sandbox-procargs-probe-secret"
    host = subprocess.Popen(["/bin/sleep", "60"], env={**os.environ, "ONODA_PROBE_SECRET": marker})
    try:
        seen = inner(shell, workspace, f"ps -Eww -p {host.pid}; ps -Eww -ax")
        assert marker not in seen.stdout and marker not in seen.stderr
    finally:
        host.kill()
        host.wait()


def test_every_symlink_hop_is_named(tmp_path):
    """uv's python: .venv/bin/python -> cpython-3.11-...-none (a link) -> cpython-3.11.15."""
    real = tmp_path / "cpython-3.11.15"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.11").write_text("")
    (tmp_path / "cpython-3.11-none").symlink_to(real)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(tmp_path / "cpython-3.11-none" / "bin" / "python3.11")
    named = set(resolved([venv / "python"]))
    assert tmp_path / "cpython-3.11-none" / "bin" / "python3.11" in named
    assert real.resolve() / "bin" / "python3.11" in named
