"""A50: the bubblewrap sandbox around Pi runs.

Unit tests build launches without starting them. The integration tests run the
real pinned Pi offline under the scripted provider inside `bwrap`, so they
need both the Pi runtime and bubblewrap; every probe below is a synthetic
fixture, not a solver run.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import requires_pi, stub_sandbox
from test_pi_end_to_end import LADDER, case, claim_args, stores
from test_pi_runtime import SCRIPT, make_bundle

from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi.contract import GatewayPolicy, Script
from onoda.adapters.pi.runtime import LiveSettings, build_launch, detect_sandbox
from onoda.adapters.pi.sandbox import Sandbox
from onoda.errors import CapabilityError


def _bwrap_works() -> bool:
    if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
        return False
    import subprocess

    probe = ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--", "/bin/true"]
    return subprocess.run(probe, capture_output=True).returncode == 0


requires_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap unavailable")


# ------------------------------------------------------------------ units


def test_a_live_run_is_refused_without_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "x")
    live = LiveSettings(
        provider="openrouter",
        model="vendor/open-model",
        credential_env="MY_KEY",
        max_requests=5,
        max_cost_usd="0.50",
    )
    with pytest.raises(CapabilityError, match="sandbox"):
        build_launch(
            variant_id="S0",
            bundle_dir=make_bundle(tmp_path),
            work_dir=tmp_path / "work",
            live=live,
            max_requests=5,
            max_acquisitions=0,
        )


def test_detect_refuses_a_host_without_bwrap(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(CapabilityError, match="bubblewrap"):
        Sandbox.detect(runtime_dir=tmp_path, kernel_dir=tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(CapabilityError, match="Linux"):
        Sandbox.detect(runtime_dir=tmp_path, kernel_dir=tmp_path)


def test_a_sandboxed_launch_routes_tools_into_the_inner_layer(tmp_path):
    bundle = make_bundle(tmp_path)
    work = tmp_path / "work"
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=bundle,
        work_dir=work,
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
        sandbox=stub_sandbox(tmp_path),
    )
    assert launch_obj.argv[0] == "/usr/bin/bwrap"
    work = work.resolve()
    settings = json.loads((launch_obj.agent_dir / "settings.json").read_text())
    assert settings == {
        "shellPath": str(work / "sandbox" / "shell"),
        "defaultProjectTrust": "never",
    }
    shell = (work / "sandbox" / "shell").read_text()
    assert "--unshare-all" in shell and "--clearenv" in shell
    workspace = str(work / "workspace")
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert policy.write_roots == (workspace,)
    assert policy.read_roots == (workspace, str(bundle.resolve()))
    assert json.loads(launch_obj.run_info_path.read_text())["sandbox"] == "bwrap"


def test_an_unsandboxed_launch_leaves_the_file_tools_unrestricted(tmp_path):
    launch_obj = build_launch(
        variant_id="S0",
        bundle_dir=make_bundle(tmp_path),
        work_dir=tmp_path / "work",
        script=SCRIPT,
        max_requests=5,
        max_acquisitions=1,
    )
    policy = GatewayPolicy.model_validate_json(launch_obj.policy_path.read_text())
    assert policy.read_roots is None and policy.write_roots is None
    assert not (launch_obj.agent_dir / "settings.json").exists()


# ------------------------------------------------------------ integration

SECRET = "sandbox-probe-secret"

PROBE = (
    "{ echo ENV=$(env | wc -l); echo SECRET=${ONODA_PROBE_SECRET:-none};"
    " ls ../spool >/dev/null 2>&1 && echo SPOOL=seen || echo SPOOL=hidden;"
    " ls {repo}/cases >/dev/null 2>&1 && echo REPO=seen || echo REPO=hidden;"
    " ls /proc/1 >/dev/null 2>&1 && echo PROC=seen || echo PROC=hidden;"
    " python3 -c 'import socket; socket.getaddrinfo(\"pypi.org\", 443)'"
    " >/dev/null 2>&1 && echo NET=open || echo NET=closed; } > probe.txt"
)


def call(i: str, name: str, **args) -> dict:
    return {"type": "toolCall", "id": i, "name": name, "arguments": args}


def probe_script(*calls: dict, children: list | None = None) -> Script:
    turns = [{"content": [c]} for c in calls]
    turns += [
        {"content": [call("claim", "report_states", **claim_args())]},
        {"content": [{"type": "text", "text": "reported"}]},
    ]
    body: dict = {"source": "tests/test_pi_sandbox.py (synthetic probe)", "turns": turns}
    if children is not None:
        body["children"] = children
    return Script.model_validate(body)


def sandboxed_run(tmp_path, script: Script, variant: str = "S0", **kw):
    store, _ = stores(tmp_path)
    pi = pi_run.run_case(
        store, case(), run_id="sb", variant_id=variant, work_dir=tmp_path / "w",
        script=script, sandbox=detect_sandbox(), **kw,
    )  # fmt: skip
    assert (pi.ingest.status, pi.ingest.complete) == ("settled", True), pi.stderr_tail
    return store, tmp_path / "w" / "pi" / "workspace"


def lines(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().split())


@requires_pi
@requires_bwrap
def test_bash_runs_without_network_environment_or_host_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ONODA_PROBE_SECRET", SECRET)
    repo = Path(__file__).resolve().parents[1]
    store, workspace = sandboxed_run(
        tmp_path, probe_script(call("b1", "bash", command=PROBE.replace("{repo}", str(repo))))
    )
    seen = lines(workspace / "probe.txt")
    assert seen["SECRET"] == "none"
    assert int(seen["ENV"]) < 10  # only the handful the inner layer sets
    assert seen["SPOOL"] == seen["REPO"] == seen["PROC"] == "hidden"
    assert seen["NET"] == "closed"
    assert store.run_record("sb").config["sandbox"] == "bwrap"


@requires_pi
@requires_bwrap
def test_file_tools_are_confined_to_the_workspace_and_bundle(tmp_path):
    script = probe_script(
        call("b1", "bash", command="ln -s /proc/1/environ env.lnk; echo hi > ok.txt"),
        call("r1", "read", path="env.lnk"),
        call("r2", "read", path="/proc/1/environ"),
        call("r3", "read", path="../spool/events.jsonl"),
        call("r4", "read", path="ok.txt"),
        call("w1", "write", path="../agent/settings.json", content="{}"),
        call("e1", "edit", path="~/x", oldText="a", newText="b"),
    )
    store, workspace = sandboxed_run(tmp_path, script)
    blocked = {e.payload["tool_call_id"] for e in store.events("sb") if e.type == "pi.tool-blocked"}
    assert blocked == {"r1", "r2", "r3", "w1", "e1"}
    assert (workspace / "ok.txt").read_text() == "hi\n"  # b1 and r4 ran


CELL = (
    "import socket, os\n"
    "try:\n"
    "    socket.getaddrinfo('pypi.org', 443); net = 'open'\n"
    "except OSError:\n"
    "    net = 'closed'\n"
    "open('kernel.txt', 'w').write(f'NET={net} ENV={len(os.environ)}')\n"
    "print(rlm('say ok', context='synthetic'))\n"
)


@requires_pi
@requires_bwrap
def test_the_rlm_kernel_and_its_child_run_inside_the_sandbox(tmp_path):
    child = {
        "schema": "onoda.pi-script/1",
        "source": "tests/test_pi_sandbox.py:child",
        "turns": [{"content": [{"type": "text", "text": "ok"}]}],
    }
    store, workspace = sandboxed_run(
        tmp_path,
        probe_script(call("k1", "ipython", code=CELL), children=[child]),
        variant="S1r",
        ladder=LADDER,
        synthetic_ladder=True,
    )
    seen = lines(workspace / "kernel.txt")
    assert seen["NET"] == "closed" and int(seen["ENV"]) < 10
    (child_event,) = [e for e in store.events("sb") if e.type == "rlm.child"]
    assert child_event.payload["complete"]
