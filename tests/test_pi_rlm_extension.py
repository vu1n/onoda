"""M4-003: the `onoda-rlm.ts` extension in the real pinned Pi process.

Pi runs offline under the scripted provider, parent and child alike; every
script, answer and value here is a synthetic test fixture. The argv is written
by hand (the runtime is a separate lane), with the RLM extension loaded after
the task extension and before the scripted provider and the recorder.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conftest import requires_pi
from test_pi_extensions import call, stage_bundle, text, turn

from onoda.adapters.pi import contract as C

pytestmark = requires_pi

PI_BIN = C.RUNTIME_DIR / "node_modules/.bin/pi"
KEY = bytes(range(32))


@dataclass
class Spool:
    dir: Path
    lines: list[Any]

    def of(self, kind: str) -> list[Any]:
        return [ln for ln in self.lines if ln.type == kind]

    def obj(self, digest: str) -> bytes:
        return (self.dir / C.SPOOL_OBJECTS / digest).read_bytes()

    def first(self, kind: str, **match: Any) -> Any:
        return next(
            ln for ln in self.of(kind) if all(getattr(ln, k) == v for k, v in match.items())
        )


def read_spool(spool_dir: Path, key: bytes) -> Spool:
    """Parse every line, check seq is gap-free and the mac chain holds, and that
    every cited object exists."""
    raw = (spool_dir / C.SPOOL_EVENTS).read_bytes()
    lines, prev = [], ""
    for n, text_ in enumerate(raw.split(b"\n")[:-1]):
        m = C.MAC_RE.search(text_)
        assert m, f"line {n} has no mac"
        body = text_[: m.start()] + b"}"
        mac = hmac.new(key, prev.encode() + body, hashlib.sha256).hexdigest()
        assert mac == m.group(1).decode(), f"line {n} fails its mac"
        prev = mac
        line = C.parse_spool_line(json.loads(text_))
        assert line.seq == n
        lines.append(line)
    assert lines[0].type == "spool.start" and lines[-1].type == "spool.end"
    for line in lines:
        for field in ("code", "output", "prompt", "answer", "value", "content", "input"):
            digest = getattr(line, field, None)
            if isinstance(digest, str) and len(digest) == 64:
                assert (spool_dir / C.SPOOL_OBJECTS / digest).is_file(), (line.type, field)
    return Spool(spool_dir, lines)


def child_script(*turns: dict[str, Any], children: tuple[C.Script, ...] = ()) -> C.Script:
    return C.Script(source="tests/test_pi_rlm_extension.py:child", turns=turns, children=children)


def run_rlm(
    root: Path,
    turns: list[dict[str, Any]],
    *,
    children: tuple[C.Script, ...] = (),
    limits: C.RlmLimits | None = None,
    max_requests: int = 20,
) -> tuple[subprocess.CompletedProcess, Spool]:
    v = C.VARIANTS["S1r"]
    for d in ("ws", "agent", "sessions", "spool", "home", "bundle", "rlm"):
        (root / d).mkdir(parents=True)
    bundle, _ = stage_bundle(root / "bundle", ladder=True)
    candidate = C.SEED_CANDIDATE
    if limits is not None:
        candidate = C.HarnessCandidate(
            components={**candidate.components, "rlm.limits": limits.model_dump_json()}
        )
    policy = C.GatewayPolicy(
        allowed_tools=v.tools(),
        max_requests=max_requests,
        max_acquisitions=2,
        path_aliases={
            str(root / "ws"): "$WORKSPACE",
            str(root / "bundle"): "$BUNDLE",
            str(root / "spool"): "$SPOOL",
            str(root / "sessions"): "$SESSIONS",
            str(root / "rlm"): "$RLM_WORK",
            str(C.RUNTIME_DIR): "$PI_RUNTIME",
            str(C.RUNTIME_DIR.resolve()): "$PI_RUNTIME",
        },
    )
    script = C.Script(source="tests/test_pi_rlm_extension.py", turns=turns, children=children)
    run_info = {
        "schema": C.SPOOL_SCHEMA,
        "pi_version": C.PI_VERSION,
        "sol_pi_commit": None,
        "variant": v.id,
        "mechanisms": [],
        "provider": C.SCRIPTED_PROVIDER,
        "model": C.SCRIPTED_MODEL,
        "scripted": True,
    }
    (root / "policy.json").write_text(policy.model_dump_json(by_alias=True))
    (root / "script.json").write_text(script.model_dump_json(by_alias=True))
    (root / "run-info.json").write_text(json.dumps(run_info))
    (root / "components.json").write_text(candidate.rlm_components().model_dump_json(by_alias=True))
    (root / "spool.key").write_bytes(KEY)
    extensions = [
        C.EXTENSIONS_DIR / C.TASK_EXTENSION,
        C.EXTENSIONS_DIR / C.RLM_EXTENSION,
        C.EXTENSIONS_DIR / C.SCRIPTED_EXTENSION,
        C.EXTENSIONS_DIR / C.RECORDER_EXTENSION,
    ]
    argv = [str(PI_BIN), "--no-extensions"]
    for ext in extensions:
        argv += ["-e", str(ext)]
    argv += [
        "--session-dir", str(root / "sessions"),
        "--no-context-files", "--no-skills", "--no-prompt-templates",
        "--provider", C.SCRIPTED_PROVIDER, "--model", C.SCRIPTED_MODEL,
        "--tools", ",".join(v.tools()),
        "--mode", "json", "-p", bundle.instruction,
    ]  # fmt: skip
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root / "home"),
        "PI_CODING_AGENT_DIR": str(root / "agent"),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "PI_SKIP_VERSION_CHECK": "1",
        C.ENV_SPOOL: str(root / "spool"),
        C.ENV_SPOOL_KEY: str(root / "spool.key"),
        C.ENV_SCRIPT: str(root / "script.json"),
        C.ENV_TASK: str(root / "bundle"),
        C.ENV_POLICY: str(root / "policy.json"),
        C.ENV_RUN_INFO: str(root / "run-info.json"),
        C.ENV_RLM_DEPTH: "0",
        C.ENV_RLM_COMPONENTS: str(root / "components.json"),
        C.ENV_RLM_WORK: str(root / "rlm"),
        C.ENV_RLM_PYTHON: sys.executable,
    }
    proc = subprocess.run(
        argv, cwd=root / "ws", env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=180
    )
    assert (root / "spool" / C.SPOOL_EVENTS).exists(), proc.stderr.decode()
    return proc, read_spool(root / "spool", KEY)


def ipython(call_id: str, code: str) -> dict[str, Any]:
    return call(call_id, "ipython", code=code)


def visible_index(spool: Spool, call_id: str) -> int:
    return spool.first("tool.result", tool_call_id=call_id, stage="visible").seq


def test_cells_share_state_and_rlm_spawns_a_recorded_child(tmp_path):
    cell2 = (
        "ans = rlm('what is x doubled?', context=str(x))\n"
        "print('child said', ans)\n"
        "print(commit_state('answer', {'a': ans, 'x': x}))\n"
        "try:\n"
        "    rlm('again')\n"
        "except RlmError as e:\n"
        "    print('refused:', e)\n"
    )
    proc, parent = run_rlm(
        tmp_path,
        [
            turn(ipython("c1", "x = 21\nx")),
            turn(ipython("c2", cell2)),
            turn(text("done")),
        ],
        children=(child_script(turn(text("42"))),),
        limits=C.RlmLimits(max_depth=1, max_children=1, max_output_chars=10000),
    )
    assert parent.lines[-1].status == "settled", proc.stderr.decode()

    cells = parent.of("rlm.cell")
    assert [c.cell_index for c in cells] == [0, 1]
    assert not any(c.is_error for c in cells)
    out1 = json.loads(parent.obj(cells[0].output))
    assert out1["stdout"] == "21\n"
    out2 = json.loads(parent.obj(cells[1].output))
    assert "child said 42" in out2["stdout"]
    assert "refused: rlm() refused (max-children)" in out2["stdout"]

    (start,) = parent.of("rlm.child.start")
    (end,) = parent.of("rlm.child.end")
    (refused,) = parent.of("rlm.child.refused")
    (state,) = parent.of("rlm.state")
    assert start.spool_subdir == "children/0000" and start.depth == 1
    assert b"what is x doubled?" in parent.obj(start.prompt) and b"21" in parent.obj(start.prompt)
    assert end.end_status == "settled" and end.requests == 1 and end.exit_code == 0
    assert parent.obj(end.answer) == b"42"
    assert refused.reason == "max-children" and refused.tool_call_id == "c2"
    assert json.loads(parent.obj(state.value)) == {"a": "42", "x": 21}
    assert f"sha256:{state.value}" in out2["stdout"]
    # Every rlm line is durable before the result that reports it is visible.
    shown = visible_index(parent, "c2")
    for line in (start, end, refused, state, cells[1]):
        assert line.seq < shown
    assert parent.first("tool.result", tool_call_id="c2", stage="raw").seq > cells[1].seq

    child = read_spool(tmp_path / "spool" / "children" / "0000", C.derive_child_key(KEY, 0))
    assert child.lines[-1].status == "settled"
    assert len(child.of("provider.request")) == 1
    assert child.lines[0].variant == "S1r"


def test_child_request_cap_is_the_parents_remaining_budget(tmp_path):
    busy = tuple(turn(ipython(f"k{i}", f"{i}")) for i in range(6))
    proc, parent = run_rlm(
        tmp_path,
        [turn(ipython("c1", "print(rlm('work'))")), turn(text("unreachable"))],
        children=(child_script(*busy),),
        max_requests=3,
    )
    (start,) = parent.of("rlm.child.start")
    (end,) = parent.of("rlm.child.end")
    assert start.max_requests == 2  # 3 minus the parent's own first request
    assert end.end_status == "budget-exhausted" and end.requests == 2
    child = read_spool(tmp_path / "spool" / "children" / "0000", C.derive_child_key(KEY, 0))
    assert len(child.of("provider.request")) == 2
    # The parent is charged: 1 own + 2 child = 3, so it cannot make another request.
    assert parent.lines[-1].status == "budget-exhausted", proc.stderr.decode()
    assert len(parent.of("provider.request")) == 1


def test_max_depth_refuses_before_spawning(tmp_path):
    proc, parent = run_rlm(
        tmp_path,
        [turn(ipython("c1", "rlm('deep')")), turn(text("done"))],
        limits=C.RlmLimits(max_depth=0, max_children=4, max_output_chars=100),
    )
    (refused,) = parent.of("rlm.child.refused")
    assert refused.reason == "max-depth"
    assert not parent.of("rlm.child.start")
    assert not (tmp_path / "spool" / "children").exists()
    (cell,) = parent.of("rlm.cell")
    assert cell.is_error


def test_commit_state_of_a_non_json_value_is_a_cell_error_not_a_spool_line(tmp_path):
    proc, parent = run_rlm(
        tmp_path,
        [turn(ipython("c1", "commit_state('bad', {1, 2})")), turn(text("done"))],
    )
    assert parent.lines[-1].status == "settled", proc.stderr.decode()
    (cell,) = parent.of("rlm.cell")
    assert cell.is_error
    assert "TypeError" in json.loads(parent.obj(cell.output))["error"]
    assert not parent.of("rlm.state")


def test_output_is_capped_and_components_reach_the_prompt(tmp_path):
    proc, parent = run_rlm(
        tmp_path,
        [turn(ipython("c1", "print('y' * 500)")), turn(text("done"))],
        limits=C.RlmLimits(max_depth=1, max_children=1, max_output_chars=50),
    )
    (cell,) = parent.of("rlm.cell")
    assert cell.truncated
    first = parent.first("provider.request", request_index=0)
    context = parent.obj(first.context).decode()
    assert "Python REPL (ipython tool)" in context  # the seed system prompt component
