"""M3-002: onoda's Pi extensions, exercised in the real pinned Pi process.

Every run here launches `pi` 0.85.1 (and SoL-Pi at its pinned commit when the
variant has mechanisms) offline with the scripted provider. Scripts, claims,
and ladders are synthetic test fixtures; the ORCA output handed to the agent
is a vendored upstream file recorded as acquired, never executed here.
The argv is written by hand: the runtime module is a separate lane.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import requires_pi

from onoda.adapters.pi import contract as C
from onoda.science.contract import case_manifests, load_case

pytestmark = requires_pi

CASE_ID = "orca6-water-mp2"  # smallest vendored output that is still > 10 KiB
PI_BIN = C.RUNTIME_DIR / "node_modules/.bin/pi"
SYNTHETIC_DOC = (
    "# Synthetic test document\n\nonoda-doc-marker: allowed documentation reaches the prompt.\n"
)
SYNTHETIC_LADDER = (
    "# Synthetic advisory ladder (test fixture, no scientific content)\n\nonoda-ladder-marker\n"
)


def case():
    return load_case(case_manifests()[CASE_ID])


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage_bundle(dest: Path, *, ladder: bool) -> tuple[C.TaskBundle, bytes]:
    """A TaskBundle per contract layout, pinned with the manifest's own digests."""
    c = case()
    m = c.manifest
    out_name = Path(m.outputs["log"].path).name
    (dest / "input").mkdir(parents=True)
    (dest / "output").mkdir()
    (dest / "docs").mkdir()
    (dest / "input" / Path(m.input.path).name).write_bytes(c.input_bytes)
    (dest / "output" / out_name).write_bytes(c.output_bytes["log"])
    (dest / "docs" / "notes.md").write_text(SYNTHETIC_DOC)
    if ladder:
        (dest / "ladder.md").write_text(SYNTHETIC_LADDER)
    bundle = C.TaskBundle(
        case_id=m.case_id,
        instruction="Synthetic scripted test run: report the states of the staged case.",
        input_name=Path(m.input.path).name,
        input_sha256=m.input.sha256,
        output_name=out_name,
        output_sha256=m.outputs["log"].sha256,
        review_status=m.review.status,
        docs=("notes.md",),
        ladder_sha256=sha(SYNTHETIC_LADDER.encode()) if ladder else None,
        synthetic_ladder=ladder,
    )
    (dest / "task.json").write_text(bundle.model_dump_json(by_alias=True))
    return bundle, c.output_bytes["log"]


def turn(*blocks: dict[str, Any], expect: str | None = None) -> dict[str, Any]:
    return {"content": list(blocks), "expect_replay_key": expect}


def call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


@dataclass
class Run:
    root: Path
    proc: subprocess.CompletedProcess
    lines: list[Any]
    output: bytes
    spool: Path = field(init=False)

    def __post_init__(self):
        self.spool = self.root / "spool"

    def of(self, kind: str) -> list[Any]:
        return [line for line in self.lines if line.type == kind]

    def obj(self, digest: str) -> bytes:
        return (self.spool / C.SPOOL_OBJECTS / digest).read_bytes()

    def json(self, digest: str) -> Any:
        return json.loads(self.obj(digest))

    def results(self, call_id: str, stage: str) -> list[Any]:
        return [r for r in self.of("tool.result") if r.tool_call_id == call_id and r.stage == stage]

    def request_text(self, index: int) -> str:
        (req,) = [r for r in self.of("provider.request") if r.request_index == index]
        return self.obj(req.context).decode()

    def shown_result(self, index: int, call_id: str) -> str:
        """The text of `call_id`'s tool result as request `index` showed it to the model."""
        (msg,) = [
            m
            for m in json.loads(self.request_text(index))["messages"]
            if m["role"] == "toolResult" and m["toolCallId"] == call_id
        ]
        return "\n".join(b["text"] for b in msg["content"])


def run_pi(
    root: Path,
    turns: list[dict[str, Any]],
    *,
    variant: str = "S0",
    tools: tuple[str, ...] | None = None,
    **policy: Any,
) -> Run:
    v = C.VARIANTS[variant]
    for d in ("ws", "agent", "sessions", "spool", "home", "bundle"):
        (root / d).mkdir(parents=True)
    bundle, output = stage_bundle(root / "bundle", ladder=v.ladder)
    sol_pi = v.sol_pi_config(
        reducer_provider=C.SCRIPTED_PROVIDER, reducer_model=C.SCRIPTED_REDUCER_MODEL
    )
    if sol_pi:
        (root / "agent" / "sol-pi.json").write_text(json.dumps(sol_pi))
    tools = tools or v.tools()
    aliases = {
        str(root / "ws"): "<workspace>",
        str(root / "bundle"): "<bundle>",
        str(root / "spool"): "<spool>",
        str(root / "sessions"): "<sessions>",
        str(C.RUNTIME_DIR): "<runtime>",
        str(C.RUNTIME_DIR.resolve()): "<runtime>",
        str((C.RUNTIME_DIR / "node_modules").resolve()): "<runtime>/node_modules",
    }
    gateway = C.GatewayPolicy(
        allowed_tools=policy.pop("allowed_tools", tools),
        max_requests=policy.pop("max_requests", 20),
        max_acquisitions=policy.pop("max_acquisitions", 2),
        path_aliases=aliases,
    )
    script = C.Script(
        source="tests/test_pi_extensions.py",
        turns=turns,
        reducer_turns=policy.pop("reducer_turns", []),
    )
    assert not policy
    run_info = {
        "pi_version": C.PI_VERSION,
        "sol_pi_commit": C.SOL_PI_COMMIT if sol_pi else None,
        "variant": v.id,
        "mechanisms": list(v.mechanisms),
        "provider": C.SCRIPTED_PROVIDER,
        "model": C.SCRIPTED_MODEL,
        "scripted": True,
    }
    files = {
        "policy.json": gateway.model_dump_json(by_alias=True),
        "script.json": script.model_dump_json(by_alias=True),
        "run-info.json": json.dumps(run_info),
    }
    for name, body in files.items():
        (root / name).write_text(body)

    extensions = [C.EXTENSIONS_DIR / C.TASK_EXTENSION]
    if sol_pi:
        extensions.append(C.RUNTIME_DIR / C.SOL_PI_ENTRY)
    extensions += [C.EXTENSIONS_DIR / C.SCRIPTED_EXTENSION, C.EXTENSIONS_DIR / C.RECORDER_EXTENSION]
    argv = [str(PI_BIN), "--no-extensions"]
    for ext in extensions:
        argv += ["-e", str(ext)]
    argv += [
        "--session-dir", str(root / "sessions"),
        "--no-context-files", "--no-skills", "--no-prompt-templates",
        "--provider", C.SCRIPTED_PROVIDER, "--model", C.SCRIPTED_MODEL,
        "--tools", ",".join(tools),
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
        C.ENV_SCRIPT: str(root / "script.json"),
        C.ENV_TASK: str(root / "bundle"),
        C.ENV_POLICY: str(root / "policy.json"),
        C.ENV_RUN_INFO: str(root / "run-info.json"),
    }
    proc = subprocess.run(
        argv,
        cwd=root / "ws",
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )
    events = root / "spool" / C.SPOOL_EVENTS
    assert events.exists(), proc.stderr.decode()
    lines = [C.parse_spool_line(json.loads(raw)) for raw in events.read_text().splitlines()]
    run = Run(root, proc, lines, output)
    assert_well_formed(run)
    return run


def assert_well_formed(run: Run) -> None:
    """Invariants every spool must satisfy, whatever the scenario."""
    lines = run.lines
    assert [line.seq for line in lines] == list(range(len(lines))), "seq must be gap-free"
    assert lines[0].type == "spool.start"
    assert [line.type for line in lines].count("spool.end") == 1
    assert lines[-1].type == "spool.end"
    for line in lines:
        for name, value in line:
            if name in {"context", "message", "input", "content", "details", "output", "claim"}:
                if isinstance(value, str) and len(value) == 64:
                    assert (run.spool / C.SPOOL_OBJECTS / value).exists(), (line.type, name)
    requests = run.of("provider.request")
    assert [r.request_index for r in requests] == list(range(len(requests)))
    assert lines[-1].requests == len(requests)
    for r in requests:
        assert sha(run.obj(r.context)) == r.context


def s0_script() -> list[dict[str, Any]]:
    return [
        turn(call("c1", "case_input"), call("c2", "orca_run")),
        turn(
            call(
                "c3",
                "report_states",
                final_energy={"text": "-76.0", "unit": "Eh"},
                states=[{"root": 1, "energy": {"text": "7.5", "unit": "eV"}}],
                notes="synthetic scripted claim; not a result",
            )
        ),
        turn(text("Synthetic scripted run complete.")),
    ]


def test_s0_run_spools_the_whole_run(tmp_path):
    run = run_pi(tmp_path, s0_script())
    assert run.proc.returncode == 0, run.proc.stderr.decode()

    start = run.lines[0]
    assert (start.variant, start.scripted, start.sol_pi_commit) == ("S0", True, None)
    assert start.session_dir == str(tmp_path / "sessions") and start.session_id
    types = {line.type for line in run.lines}
    assert {
        "spool.start",
        "provider.request",
        "provider.response",
        "tool.call",
        "tool.result",
        "task.acquire",
        "task.claim",
        "spool.end",
    } <= types
    end = run.lines[-1]
    assert (end.status, end.requests) == ("settled", 3)

    for r in run.of("provider.request"):
        assert (r.source, r.model) == ("scripted-provider", C.SCRIPTED_MODEL)
    responses = run.of("provider.response")
    assert [r.request_index for r in responses] == [0, 1, 2]
    assert [r.stop_reason for r in responses] == ["toolUse", "toolUse", "stop"]
    assert responses[0].cost_usd == "0" and responses[0].usage_tokens["totalTokens"] == 0

    calls = {c.tool_call_id: c for c in run.of("tool.call")}
    assert {k: (c.tool_name, c.decision, c.request_index) for k, c in calls.items()} == {
        "c1": ("case_input", "allowed", 0),
        "c2": ("orca_run", "allowed", 0),
        "c3": ("report_states", "allowed", 1),
    }
    for cid in calls:
        (raw,) = run.results(cid, "raw")
        (visible,) = run.results(cid, "visible")
        assert calls[cid].seq < raw.seq < visible.seq  # recorded before it ran
        assert raw.content == visible.content and not raw.is_error

    (acq,) = run.of("task.acquire")
    assert (acq.tool_call_id, acq.case_id, acq.executed_here) == ("c2", CASE_ID, False)
    assert run.obj(acq.output) == run.output
    assert acq.seq < run.results("c2", "raw")[0].seq
    assert run.json(run.results("c2", "visible")[0].content)[0]["text"] == run.output.decode()

    (claim_line,) = run.of("task.claim")
    claim = C.Claim.model_validate(run.json(claim_line.claim))
    assert claim.case_id == CASE_ID and claim.states[0].multiplicity is None

    first = json.loads(run.request_text(0))
    assert "onoda-doc-marker" in first["systemPrompt"]
    assert "onoda-ladder-marker" not in first["systemPrompt"]  # S0 has no ladder
    assert {t["name"] for t in first["tools"]} >= set(C.TASK_TOOLS)


def test_replay_keys_are_location_free(tmp_path):
    first = run_pi(tmp_path / "a", s0_script())
    keys = [r.replay_key for r in first.of("provider.request")]
    replay = [{**t, "expect_replay_key": k} for t, k in zip(s0_script(), keys, strict=True)]
    second = run_pi(tmp_path / "elsewhere", replay)
    assert second.lines[-1].status == "settled"
    assert [r.replay_key for r in second.of("provider.request")] == keys
    assert not second.of("replay.mismatch")


def test_gateway_blocks_disallowed_tool_and_unfusable_then_run(tmp_path):
    turns = [
        turn(
            call("b1", "bash", command="touch ran-bash"),
            call(
                "w1",
                "write",
                path="a.inp",
                content="! synthetic\n",
                then_run={"command": "orca a.inp"},
            ),
            call("o1", "orca_run"),
            call("o2", "orca_run"),
        ),
        turn(text("Synthetic scripted run complete.")),
    ]
    allowed = tuple(t for t in C.VARIANTS["S2d"].tools() if t != "bash")
    run = run_pi(tmp_path, turns, variant="S2d", allowed_tools=allowed, max_acquisitions=1)

    calls = {c.tool_call_id: c for c in run.of("tool.call")}
    assert calls["b1"].decision == "blocked" and "not allowed" in calls["b1"].reason
    assert calls["w1"].decision == "blocked" and "orca" in calls["w1"].reason
    assert run.json(calls["w1"].input)["then_run"]["command"] == "orca a.inp"
    assert calls["o1"].decision == "allowed"
    assert calls["o2"].decision == "blocked" and "acquisition" in calls["o2"].reason
    for cid in ("b1", "w1", "o2"):
        assert run.results(cid, "raw") == []  # never ran
        (visible,) = run.results(cid, "visible")
        assert visible.is_error
        assert "onoda gateway" in run.json(visible.content)[0]["text"]
    assert not (tmp_path / "ws" / "ran-bash").exists()
    assert not (tmp_path / "ws" / "a.inp").exists()
    assert len(run.of("task.acquire")) == 1
    assert run.lines[-1].status == "settled"


def observation_id(tool_name: str, call_id: str, text_: str) -> str:
    """SoL-Pi ObservationPack's id for a pure-text result (observation.ts at the pinned commit)."""
    content = sha(text_.encode())
    return "obs_" + sha(f"{tool_name}\0{call_id}\0{content}".encode())[:24]


def test_observation_pack_archives_large_output_and_shows_handle(tmp_path):
    output_text = case().output_bytes["log"].decode()
    assert len(output_text.encode()) > 10 * 1024
    obs = observation_id("orca_run", "o1", output_text)
    turns = [
        turn(call("o1", "orca_run")),
        turn(call("i1", "case_input")),
        turn(call("i2", "case_input")),
        turn(call("r1", "obs_recall", id=obs, offset=0)),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_pi(tmp_path, turns, variant="S2a")
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()

    start = run.lines[0]
    assert start.sol_pi_commit == C.SOL_PI_COMMIT and start.mechanisms == ("observationPack",)
    pack = Path(start.session_dir) / "sol-pi" / start.session_id / "observation-pack"
    assert (pack / "objects" / f"{obs}.txt").read_bytes() == run.output
    assert obs in (pack / "ledger.jsonl").read_text()

    # Full output in the two requests after the call, then only the handle.
    for index in (1, 2):
        assert run.shown_result(index, "o1") == output_text
    for index in (3, 4):
        handle = run.shown_result(index, "o1")
        assert f"id: {obs}" in handle and len(handle) < len(output_text) // 4

    (visible,) = run.results("o1", "visible")  # the tool result itself is not rewritten
    assert run.json(visible.content)[0]["text"] == output_text
    (recall,) = run.results("r1", "visible")
    assert f"[obs_recall id={obs} offset=0" in run.json(recall.content)[0]["text"]


def test_exhausted_script_ends_script_exhausted(tmp_path):
    run = run_pi(tmp_path, [turn(call("c1", "case_input"))])
    end = run.lines[-1]
    assert (end.status, end.requests) == ("script-exhausted", 2)
    assert "script exhausted" in end.reason
    assert run.of("provider.response")[-1].stop_reason == "error"


def test_wrong_replay_key_ends_replay_mismatch(tmp_path):
    wrong = "0" * 64
    run = run_pi(tmp_path, [turn(text("unreachable"), expect=wrong)])
    (mismatch,) = run.of("replay.mismatch")
    (request,) = run.of("provider.request")
    assert (mismatch.request_index, mismatch.expected, mismatch.actual) == (
        0,
        wrong,
        request.replay_key,
    )
    assert run.lines[-1].status == "replay-mismatch"
    assert run.of("provider.response")[-1].stop_reason == "error"


def test_request_budget_aborts_the_run(tmp_path):
    turns = [turn(call("c1", "case_input")), turn(text("never served"))]
    run = run_pi(tmp_path, turns, max_requests=1)
    end = run.lines[-1]
    assert (end.status, end.requests) == ("budget-exhausted", 1)
    assert [r.request_index for r in run.of("provider.response")] == [0]


REDUCER_LOG = "synthetic reducer log line\n" * 200  # what the bash call below prints
# `make` makes it a diagnostic command SoL-Pi's reducer applies to; its output is discarded.
REDUCER_COMMAND = (
    "make --version >/dev/null 2>&1; "
    "python3 -c \"print('synthetic reducer log line\\n' * 200, end='')\""
)


def receipt(source_sha256: str) -> str:
    return json.dumps(
        {
            "schema": "sol-pi-evidence-receipt/1",
            "source_sha256": source_sha256,
            "status": "success",
            "uncertain": False,
            "evidence": [{"kind": "summary", "quote": "synthetic reducer log line"}],
        }
    )


def test_reducer_rewrite_keeps_raw_and_visible_apart(tmp_path):
    turns = [turn(call("b1", "bash", command=REDUCER_COMMAND)), turn(text("done"))]
    reducer = [turn(text(receipt(sha(REDUCER_LOG.encode()))))]
    run = run_pi(tmp_path, turns, variant="S2b", reducer_turns=reducer)
    end = run.lines[-1]
    assert (end.status, end.requests) == ("settled", 3)

    requests = run.of("provider.request")
    assert [r.model for r in requests] == ["scripted", "scripted-reducer", "scripted"]
    responses = run.of("provider.response")
    assert sorted(r.request_index for r in responses) == [0, 1, 2]
    (raw,) = run.results("b1", "raw")
    (visible,) = run.results("b1", "visible")
    assert run.json(raw.content)[0]["text"] == REDUCER_LOG
    assert run.json(visible.content)[0]["text"].startswith("sol_pi_evidence_receipt_v1")
    assert raw.seq < requests[1].seq < visible.seq


def test_missing_reducer_turn_stops_the_run(tmp_path):
    turns = [turn(call("b1", "bash", command=REDUCER_COMMAND)), turn(text("never served"))]
    run = run_pi(tmp_path, turns, variant="S2b")
    end = run.lines[-1]
    assert (end.status, end.requests) == ("script-exhausted", 2)
    assert [r.model for r in run.of("provider.request")] == ["scripted", "scripted-reducer"]
