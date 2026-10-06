"""VTE-003: the VTE task tools (`structure`, `orca_submit`, `report_vtes`) in the real Pi.

Pi runs offline under the scripted provider, as in `test_pi_extensions`. The
broker here is a fake: a small Unix-socket server in this file that speaks
the `vte_contract` broker protocol and answers with synthetic text. It runs
no ORCA, so nothing it returns is a solver or chemistry result, although its
replies say `executed_here` to exercise that path. The structure, scripts and
claims are synthetic test fixtures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import requires_pi
from test_pi_extensions import PI_BIN, Run, assert_well_formed, call, text, turn

from onoda.adapters.pi import contract as C
from onoda.adapters.pi import vte_contract as V
from onoda.science.vte import VteTarget

pytestmark = requires_pi

CASE_ID = "synthetic-vte-formaldehyde"
STRUCTURE = (
    "4\nsynthetic test geometry (not a QUESTDB structure)\n"
    "C 0.000000 0.000000 -0.528500\nO 0.000000 0.000000 0.674500\n"
    "H 0.000000 0.934500 -1.111500\nH 0.000000 -0.934500 -1.111500\n"
)
TARGETS = (VteTarget(spin=1, irrep="A2", count=1), VteTarget(spin=3, irrep="A1", count=2))
INPUT = "! synthetic test input\n* xyzfile 0 1 structure.xyz\n"
SYNTHETIC_LADDER = "# Synthetic advisory ladder (test fixture)\n\nonoda-vte-ladder-marker\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_output(job_id: str, pad: int = 0) -> bytes:
    """Synthetic log text in the shape of an NEVPT2 summary; no ORCA ran."""
    body = (
        f"SYNTHETIC fake-broker output for {job_id}; no ORCA ran, not a chemistry result\n"
        "STATE 1 MULT 1 IRREP A2  DE = 3.912 eV\n"
    )
    return (body + "synthetic padding line\n" * pad).encode()


class FakeBroker(socketserver.UnixStreamServer):
    """One reply per connection. Inputs containing REFUSE are refused, TAMPER
    gets an output that does not match its pin, PAD pads the output past
    ObservationPack's threshold, CHECKS gets one synthetic check line (A66).
    Every completed job gets a one-line synthetic summary (A74).
    A repeated (input, attach) is a cache hit."""

    def __init__(self, path: str):
        self.submits: list[V.BrokerSubmit] = []
        self.jobs: dict[str, bytes] = {}  # job id -> output bytes
        self.cache: dict[str, bytes] = {}
        super().__init__(path, _Handler)

    def answer(self, req: V.BrokerSubmit) -> V.BrokerReply:
        self.submits.append(req)
        if "REFUSE" in req.input:
            return V.BrokerReply(status="refused", reason="synthetic refusal")
        if any(a.job_id not in self.jobs for a in req.attach):
            return V.BrokerReply(status="refused", reason="unknown attachment")
        key = sha(req.model_dump_json(include={"input", "attach"}).encode())
        job_id = f"j{len(self.jobs) + 1:04d}"
        hit = key in self.cache
        output = self.cache[key] if hit else fake_output(job_id, 1000 if "PAD" in req.input else 0)
        self.cache[key] = self.jobs[job_id] = output
        pin = sha(b"tampered" if "TAMPER" in req.input else output)
        return V.BrokerReply(
            status="completed",
            job_id=job_id,
            cache_hit=hit,
            executed_here=not hit,
            input_sha256="sha256:" + sha(req.input.encode()),
            output_sha256="sha256:" + pin,
            output_b64=base64.b64encode(output).decode(),
            files=("job.gbw",),
            checks=("SYNTHETIC check line, not a chemistry result",)
            if "CHECKS" in req.input
            else (),
            summary=(f"termination: SYNTHETIC summary for {job_id}",),
        )


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        req = V.BrokerSubmit.model_validate_json(self.rfile.readline())
        self.wfile.write(self.server.answer(req).model_dump_json().encode() + b"\n")


@pytest.fixture
def broker():
    # AF_UNIX paths are short (108 bytes); pytest's tmp_path can be too long.
    sock_dir = tempfile.mkdtemp(prefix="onoda-vte-")
    server = FakeBroker(os.path.join(sock_dir, "broker.sock"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    shutil.rmtree(sock_dir)


def stage_vte_bundle(dest: Path, *, ladder: bool, max_executions: int) -> V.VteTaskBundle:
    (dest / "structure").mkdir(parents=True)
    (dest / "structure" / "structure.xyz").write_text(STRUCTURE)
    if ladder:
        (dest / "ladder.md").write_text(SYNTHETIC_LADDER)
    bundle = V.VteTaskBundle(
        case_id=CASE_ID,
        instruction="Synthetic scripted test run: compute the target states.",
        molecule="formaldehyde (synthetic test geometry)",
        point_group="C2v",
        structure_name="structure.xyz",
        structure_sha256="sha256:" + sha(STRUCTURE.encode()),
        targets=TARGETS,
        review_status="synthetic",
        ladder_sha256=sha(SYNTHETIC_LADDER.encode()) if ladder else None,
        synthetic_ladder=ladder,
        max_executions=max_executions,
    )
    (dest / "task.json").write_text(bundle.model_dump_json(by_alias=True))
    return bundle


def run_vte(
    root: Path,
    turns: list[dict[str, Any]],
    broker: FakeBroker,
    *,
    variant: str = "S0",
    max_executions: int | None = 3,
) -> Run:
    """`run_pi` for a VTE bundle; `max_executions=None` leaves the policy default."""
    v = C.VARIANTS[variant]
    for d in ("ws", "agent", "sessions", "spool", "home", "bundle"):
        (root / d).mkdir(parents=True)
    bundle = stage_vte_bundle(root / "bundle", ladder=v.ladder, max_executions=max_executions or 1)
    sol_pi = v.sol_pi_config(
        reducer_provider=C.SCRIPTED_PROVIDER, reducer_model=C.SCRIPTED_REDUCER_MODEL
    )
    if sol_pi:
        (root / "agent" / "sol-pi.json").write_text(json.dumps(sol_pi))
    tools = v.tools("vte")
    limits = {} if max_executions is None else {"max_executions": max_executions}
    gateway = C.GatewayPolicy(allowed_tools=tools, max_requests=20, max_acquisitions=0, **limits)
    script = C.Script(source="tests/test_pi_vte_tools.py", turns=turns)
    run_info = {
        "pi_version": C.PI_VERSION,
        "sol_pi_commit": C.SOL_PI_COMMIT if sol_pi else None,
        "variant": v.id,
        "mechanisms": list(v.mechanisms),
        "provider": C.SCRIPTED_PROVIDER,
        "model": C.SCRIPTED_MODEL,
        "scripted": True,
    }
    (root / "policy.json").write_text(gateway.model_dump_json(by_alias=True))
    (root / "script.json").write_text(script.model_dump_json(by_alias=True))
    (root / "run-info.json").write_text(json.dumps(run_info))

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
        V.ENV_BROKER: broker.server_address,
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
    run = Run(root, proc, lines, b"")
    assert_well_formed(run)
    return run


def shown(run: Run, call_id: str) -> str:
    (visible,) = run.results(call_id, "visible")
    return "\n".join(b["text"] for b in run.json(visible.content))


def vte_state(job_id: str, energy: str = "3.912", unit: str = "eV") -> dict[str, Any]:
    return {
        "spin": 1,
        "irrep": "A2",
        "character": "n,pi*",
        "energy": {"text": energy, "unit": unit},
        "job_id": job_id,
    }


def test_vte_run_submits_spools_execute_and_reports_a_claim(tmp_path, broker):
    turns = [
        turn(call("s1", "structure")),
        turn(
            call("x1", "orca_submit", input=INPUT),
            call("x2", "orca_submit", input=INPUT),  # identical: the broker's cache
            call(
                "x3",
                "orca_submit",
                input=INPUT + "%moinp\n",
                attach=[{"job_id": "j0001", "name": "job.gbw"}],
            ),
        ),
        turn(
            call(
                "r1",
                "report_vtes",
                states=[vte_state("j0001")],
                notes="synthetic scripted claim; not a result",
            )
        ),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, variant="S1")
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()

    structure = shown(run, "s1")
    assert STRUCTURE.strip() in structure and "C2v" in structure
    assert "1 singlet A2 state" in structure and "2 triplet A1 states" in structure
    assert "At most 3 orca_submit calls" in structure

    assert [(s.tool_call_id, s.input) for s in broker.submits] == [
        ("x1", INPUT),
        ("x2", INPUT),
        ("x3", INPUT + "%moinp\n"),
    ]
    assert broker.submits[2].attach == (V.BrokerAttach(job_id="j0001", name="job.gbw"),)

    execs = {e.tool_call_id: e for e in run.of("task.execute")}
    assert {k: (e.job_id, e.status, e.cache_hit, e.executed_here) for k, e in execs.items()} == {
        "x1": ("j0001", "completed", False, True),
        "x2": ("j0002", "completed", True, False),
        "x3": ("j0003", "completed", False, True),
    }
    for cid, e in execs.items():
        (raw,) = run.results(cid, "raw")
        assert e.seq < raw.seq  # durable before the agent sees it
        assert (
            run.obj(e.input)
            == next(s.input for s in broker.submits if s.tool_call_id == cid).encode()
        )
        assert run.obj(e.output) == broker.jobs[e.job_id]
        visible = shown(run, cid)
        assert visible.startswith(f"ORCA job {e.job_id}: completed")
        assert visible.endswith(broker.jobs[e.job_id].decode())
        assert "job.gbw" in visible
        assert "orca_submit calls left in this run: " in visible
    assert "cache hit" in shown(run, "x2")
    assert broker.jobs["j0002"] == broker.jobs["j0001"]

    (claim_line,) = run.of("task.claim")
    claim = V.VteClaim.model_validate(run.json(claim_line.claim))
    assert claim.case_id == CASE_ID
    assert claim.states == (V.ClaimedVte.model_validate(vte_state("j0001")),)

    first = json.loads(run.request_text(0))
    assert {t["name"] for t in first["tools"]} >= set(V.VTE_TASK_TOOLS)
    assert not {t["name"] for t in first["tools"]} & set(C.TASK_TOOLS)
    assert "onoda-vte-ladder-marker" in first["systemPrompt"]  # S1 gets the ladder


def test_broker_checks_are_shown_before_the_output_tail(tmp_path, broker):
    turns = [
        turn(
            call("x1", "orca_submit", input=INPUT + "CHECKS\n"),
            call("x2", "orca_submit", input=INPUT),
        ),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, variant="S0")
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    checked, plain = shown(run, "x1"), shown(run, "x2")
    assert "Host checks on this output (advisory):\n- SYNTHETIC check line" in checked
    assert checked.index("Host checks") < checked.index("Full output:")
    assert "Host checks" not in plain


def test_orca_submit_past_max_executions_is_blocked(tmp_path, broker):
    turns = [
        turn(*(call(f"x{i}", "orca_submit", input=f"{INPUT}# {i}\n") for i in (1, 2, 3))),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, max_executions=2)
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    calls = {c.tool_call_id: c for c in run.of("tool.call")}
    assert [calls[c].decision for c in ("x1", "x2", "x3")] == ["allowed", "allowed", "blocked"]
    assert "execution budget exhausted (2)" in calls["x3"].reason
    assert run.results("x3", "raw") == []
    assert "onoda gateway" in shown(run, "x3")
    assert [s.tool_call_id for s in broker.submits] == ["x1", "x2"]
    assert [e.tool_call_id for e in run.of("task.execute")] == ["x1", "x2"]


def test_refused_submissions_do_not_spend_the_execution_budget(tmp_path, broker):
    """The cap counts replies that name a job, as the broker does (A59)."""
    turns = [
        turn(
            call("x1", "orca_submit", input=INPUT + "# REFUSE\n"),
            call("x2", "orca_submit", input=INPUT + "# 2\n"),
            call("x3", "orca_submit", input=INPUT + "# 3\n"),
            call("x4", "orca_submit", input=INPUT + "# 4\n"),
        ),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, max_executions=2)
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    calls = {c.tool_call_id: c for c in run.of("tool.call")}
    assert [calls[c].decision for c in ("x1", "x2", "x3", "x4")] == [
        "allowed",
        "allowed",
        "allowed",
        "blocked",
    ]
    assert "execution budget exhausted (2)" in calls["x4"].reason
    assert [s.tool_call_id for s in broker.submits] == ["x1", "x2", "x3"]
    assert [e.tool_call_id for e in run.of("task.execute")] == ["x2", "x3"]


def test_policy_without_max_executions_blocks_every_submit(tmp_path, broker):
    turns = [turn(call("x1", "orca_submit", input=INPUT)), turn(text("done"))]
    run = run_vte(tmp_path, turns, broker, max_executions=None)
    (c,) = run.of("tool.call")
    assert c.decision == "blocked" and "execution budget exhausted (0)" in c.reason
    assert broker.submits == [] and not run.of("task.execute")


def test_refused_or_unverifiable_replies_and_bad_claims_spool_nothing(tmp_path, broker):
    turns = [
        turn(
            call("x1", "orca_submit", input=INPUT + "# REFUSE\n"),
            call("x2", "orca_submit", input=INPUT + "# TAMPER\n"),
            call("x3", "orca_submit", input=INPUT, attach=[{"job_id": "j0009", "name": "job.gbw"}]),
            call("x4", "orca_submit", input=INPUT, attach=[{"job_id": "j0001", "name": "../x"}]),
        ),
        turn(
            call("r1", "report_vtes", states=[vte_state("j0002", unit="Eh")]),
            call("r2", "report_vtes", states=[vte_state("j0002", energy="about 4")]),
            call("r3", "report_vtes", states=[vte_state("job-2")]),
        ),
        turn(text("done")),
    ]
    run = run_vte(tmp_path, turns, broker, max_executions=4)
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    assert not run.of("task.execute") and not run.of("task.claim")
    expected = {
        "x1": "orca_submit refused: synthetic refusal",
        "x2": "do not match its output_sha256",
        "x3": "orca_submit refused: unknown attachment",
        "x4": "not a plain file name",
        "r1": "reported in eV",
        "r2": "not a decimal",
        "r3": "not a job id",
    }
    for cid, message in expected.items():
        (visible,) = run.results(cid, "visible")
        assert visible.is_error and message in shown(run, cid), cid
    assert [s.tool_call_id for s in broker.submits] == ["x1", "x2", "x3"]  # x4 never sent


def test_a_long_output_is_summarized_not_pasted(tmp_path, broker):
    """A74: the reply carries the parser summary and the output's tail, not the whole output."""
    turns = [
        turn(call("x1", "orca_submit", input="! CASSCF UseSym\n" + INPUT + "# PAD\n")),
        turn(call("x2", "orca_submit", input=INPUT)),
        turn(call("l1", "orca_output")),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker)
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    output = broker.jobs["j0001"].decode()
    assert len(output) > 10 * 1024
    visible = shown(run, "x1")
    assert len(visible) < 4 * 1024
    assert (
        "What the parser read from this output:\ntermination: SYNTHETIC summary for j0001"
        in visible
    )
    assert f"Full output: {len(output.split(chr(10)))} lines, read with orca_output" in visible
    assert visible.endswith("\n".join(output.split("\n")[-25:]))
    (e,) = [e for e in run.of("task.execute") if e.job_id == "j0001"]
    assert run.obj(e.output) == broker.jobs["j0001"]  # the record keeps every byte
    assert shown(run, "l1") == (
        "ORCA job j0001: completed (executed)\n"
        "input: ! CASSCF UseSym\ninput: ! synthetic test input\n"
        "termination: SYNTHETIC summary for j0001\n\n"
        "ORCA job j0002: completed (executed)\ninput: ! synthetic test input\n"
        "termination: SYNTHETIC summary for j0002"
    )


def test_a_whole_output_reread_passes_through_for_observation_pack(tmp_path, broker):
    turns = [
        turn(call("x1", "orca_submit", input=INPUT + "# PAD\n")),
        turn(call("o1", "orca_output", job_id="j0001")),
        turn(call("s1", "structure")),
        turn(call("s2", "structure")),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, variant="S2a")
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    output = broker.jobs["j0001"]
    assert len(output) > 10 * 1024
    assert shown(run, "o1").endswith(output.decode())  # the tool result is whole
    assert run.shown_result(2, "o1").endswith(output.decode())
    later = run.shown_result(4, "o1")  # ObservationPack's handle once it ages
    assert "obs_" in later and len(later) < len(output) // 4


def test_orca_output_rereads_an_earlier_job_without_spending_a_job(tmp_path, broker):
    """A72: job files are host-side, so the agent re-reads them through this tool."""
    turns = [
        turn(call("x1", "orca_submit", input=INPUT)),
        turn(
            call("r1", "orca_output", job_id="j0001"),
            call("r2", "orca_output", job_id="j0001", find="fake-broker", context=0),
            call("r3", "orca_output", job_id="j0001", find="no such text"),
            call("r4", "orca_output", job_id="j0009"),
        ),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, max_executions=1)
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    output = broker.jobs["j0001"].decode()
    assert shown(run, "r1") == "ORCA job j0001: whole output\n" + output
    found = shown(run, "r2")
    assert found.startswith('ORCA job j0001: 1 line(s) containing "fake-broker"\n')
    assert "fake-broker output for j0001" in found and found.count("\n") == 1
    assert shown(run, "r3") == 'ORCA job j0001: 0 line(s) containing "no such text"'
    (missing,) = run.results("r4", "visible")
    assert missing.is_error and "no job" in shown(run, "r4") and "j0001" in shown(run, "r4")
    assert len(broker.submits) == 1 and len(run.of("task.execute")) == 1
    copy = tmp_path / "ws" / "jobs" / "j0001"  # A72: a read-only copy in the workspace
    assert (copy / "output.out").read_bytes() == broker.jobs["j0001"]
    assert (copy / "input.inp").read_text() == INPUT
    assert (copy / "output.out").stat().st_mode & 0o222 == 0
    assert "jobs/j0001/output.out" in shown(run, "x1")


ATOM_LINES = "\n".join(line for line in STRUCTURE.splitlines()[2:])


def test_orca_input_writes_inputs_that_orca_submit_takes_without_spending_a_job(tmp_path, broker):
    """`orca_input` writes the syntax from chosen parameters; it runs nothing, so only
    `orca_submit` spends the job budget, and it refuses parameters that would not run."""
    target = {
        "method": "nevpt2",
        "basis": "aug-cc-pVTZ",
        "read_orbitals": "j0001",
        "rotate": [{"i": 7, "j": 9}],
        "nel": 6,
        "norb": 5,
        "roots": [{"mult": 1, "irrep": 0, "nroots": 1}, {"mult": 1, "irrep": 1, "nroots": 2}],
    }
    turns = [
        turn(call("b1", "orca_input", method="rhf", basis="cc-pVDZ")),
        turn(call("x1", "orca_submit", input="! RHF cc-pVDZ synthetic\n")),
        turn(
            call("b2", "orca_input", **target),
            call("e1", "orca_input", **{**target, "read_orbitals": "j0009"}),
            call("e2", "orca_input", **{**target, "roots": [{"mult": 1, "irrep": 1, "nroots": 1}]}),
            call("e3", "orca_input", method="casscf", basis="cc-pVDZ"),
            call("e4", "orca_input", method="rhf", basis="cc-pVDZ\n%base x"),
        ),
        turn(text("Synthetic scripted run complete.")),
    ]
    run = run_vte(tmp_path, turns, broker, max_executions=1)
    assert run.lines[-1].status == "settled", run.proc.stderr.decode()
    rhf = shown(run, "b1")
    assert rhf.endswith(
        "! RHF cc-pVDZ TightSCF UseSym\n%maxcore 2000\n%pal nprocs 4 end\n"
        f"* xyz 0 1\n{ATOM_LINES}\n*\n"
    )
    assert "attach: none" in rhf
    built = shown(run, "b2")
    assert '"job_id":"j0001","name":"job.gbw"' in built
    assert (
        "! aug-cc-pVTZ RIJCOSX AutoAux TightSCF UseSym MORead\n"
        '%moinp "j0001.gbw"\n%maxcore 2000\n%pal nprocs 4 end\n'
        "%scf\n  rotate\n    {7, 9, 90}\n  end\nend\n"
        "%casscf\n  nel 6\n  norb 5\n  mult 1,1\n  irrep 0,1\n  nroots 1,2\n  bweight = 1,2\n"
        "  trafostep ri\n  orbstep TRAH\n  maxiter 150\n  PTMethod SC_NEVPT2\nend\n"
        f"* xyz 0 1\n{ATOM_LINES}\n*\n"
    ) in built
    for call_id, why in [
        ("e1", "no job"),
        ("e2", "ground state first"),
        ("e3", "needs nel and norb"),
        ("e4", "not a basis set name"),
    ]:
        (result,) = run.results(call_id, "visible")
        assert result.is_error and why in shown(run, call_id), call_id
    assert len(broker.submits) == 1 and len(run.of("task.execute")) == 1
