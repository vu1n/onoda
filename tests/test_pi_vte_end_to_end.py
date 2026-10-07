"""VTE-009 end to end on the real pinned Pi: stage -> broker -> launch -> ingest -> grade -> replay.

Runs are offline under the scripted provider (A32): the scripts, claims and
ladder are synthetic fixtures. The "ORCA" the broker runs is a small Python
script labelled as a stand-in, never presented as ORCA: it prints a marker
line and then the bytes of a real ORCA 6.1.1 SC-NEVPT2 output for ketene
(`tests/fixtures/orca611-nevpt2/ketene_f_target.out`), whatever the input.
Its jobs are recorded as stand-in executions and every observation of them is
synthetic, so nothing here is a solver or chemistry result. The case is the
dev-only QUESTDB ketene case; its references are read by the grader only.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import requires_pi
from test_pi_sandbox import requires_bwrap

from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi import vte as pi_vte
from onoda.adapters.pi.contract import Script
from onoda.adapters.pi.runtime import detect_sandbox
from onoda.cli import main
from onoda.clock import LogicalClock
from onoda.errors import CapabilityError, OnodaError, ReplayMismatch
from onoda.science.orca_broker import OrcaBroker, read_journal
from onoda.science.orca_local import OrcaSettings
from onoda.science.vte import REFERENCES_SCHEMA, load_vte_case, vte_case_manifests
from onoda.store import Store

pytestmark = requires_pi

CASE_ID = "questdb-ketene"
FIXTURE = Path(__file__).parent / "fixtures" / "orca611-nevpt2" / "ketene_f_target.out"
STAND_IN_MARKER = "STAND-IN EXECUTABLE - NOT ORCA"
LADDER = b"# Synthetic advisory ladder (test fixture, no scientific content)\n"
INPUT = "! CASSCF NEVPT2 aug-cc-pVTZ\n* xyz 0 1\nC 0 0 -1.295\nC 0 0 0.019\nO 0 0 1.184\n*\n"

# Under the ORCA job sandbox (A65) a job reads only ORCA_HOME (the stand-in's own
# directory, which holds a copy of the fixture) and writes only its job dir, where
# it leaves the marker `runs` counts.
STAND_IN = f"""#!/usr/bin/env {sys.executable}
import os, sys
open("ran.stand-in", "w").write("")
sys.stdout.write("{STAND_IN_MARKER}\\n")
sys.stdout.write(open(os.path.join(os.path.dirname(__file__), "fixture.out")).read())
open("job.gbw", "w").write("stand-in orbitals\\n")
"""


def case():
    return load_vte_case(vte_case_manifests(pi_run.VTE_CASES_ROOT)[CASE_ID])


@pytest.fixture
def short(tmp_path):
    """A short work root: the broker socket path must fit AF_UNIX (104 bytes on macOS)."""
    d = Path(tempfile.mkdtemp(prefix="vte-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def stand_in(root: Path, name: str = "orca_stand_in.py", body: str = STAND_IN) -> Path:
    home = root / "orca-home"
    home.mkdir(exist_ok=True)
    shutil.copyfile(FIXTURE, home / "fixture.out")
    path = home / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def runs(binary: Path) -> int:
    return sum(1 for _ in binary.parent.parent.rglob("ran.stand-in"))


def orca(root: Path, binary: Path) -> OrcaSettings:
    return OrcaSettings(
        enabled=True,
        binary=str(binary),
        work_root=str(root / "orca-jobs"),
        max_wall_seconds=60,
        stand_in=True,
    )


def tool(call_id: str, name: str, **args) -> dict:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": args}


def state(job_id: str, energy: str = "6.069") -> dict:
    return {
        "spin": 1,
        "irrep": "B1",
        "character": "pi,3s",
        "energy": {"text": energy, "unit": "eV"},
        "job_id": job_id,
    }


def script(source: str, *submits: str, claim_job: str = "j0001", energy: str = "6.069") -> Script:
    turns = [
        [tool("s1", "structure")],
        [tool(f"x{i}", "orca_submit", input=text) for i, text in enumerate(submits, start=1)],
        [tool("o1", "orca_output", job_id="j0001", find="NEVPT2")],  # a read: ingested, no job
        [
            tool(
                "r1",
                "report_vtes",
                states=[state(claim_job, energy)],
                notes="synthetic scripted claim",
            )
        ],
        [{"type": "text", "text": "reported"}],
    ]
    return Script.model_validate({"source": source, "turns": [{"content": t} for t in turns]})


def stores(root: Path) -> tuple[Store, Store]:
    return (
        Store(root / "agent", clock=LogicalClock()),
        Store(root / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",)),
    )


def run(store: Store, root: Path, run_id: str, binary: Path, s: Script, **kw):
    return pi_run.run_vte_case(
        store,
        case(),
        run_id=run_id,
        variant_id=kw.pop("variant_id", "S1"),
        work_dir=root / run_id,
        orca=orca(root, binary),
        cache_dir=root / "cache",
        script=s,
        ladder=LADDER,
        synthetic_ladder=True,
        max_executions=4,
        timeout_s=120,
        **kw,
    )


def jobs(store: Store, run_id: str) -> dict:
    receipts = [
        store.receipt(e.payload["invocation_id"])
        for e in store.events(run_id)
        if e.type == "invocation.committed"
    ]
    return {r.metadata["job_id"]: r for r in receipts if "job_id" in r.metadata}


def assert_no_references(store: Store) -> None:
    refs = (case().root / case().manifest.references.path).read_bytes()
    for digest in store.list_record_ids("artifact"):
        data = store.get_bytes(digest)
        assert data != refs and REFERENCES_SCHEMA.encode() not in data


@pytest.fixture
def recorded(short):
    """One scripted S1 run: an execution, a cache hit of it, and a claim citing the first."""
    store, evaluator = stores(short)
    binary = stand_in(short)
    pi = run(store, short, "vte", binary, script("test_vte", INPUT, INPUT))
    return short, store, evaluator, binary, pi


def test_vte_run_executes_hits_the_cache_claims_and_grades(recorded):
    root, store, evaluator, binary, pi = recorded
    r = pi.ingest
    assert (r.status, r.complete) == ("settled", True), pi.stderr_tail
    assert runs(binary) == 1  # the second, identical submission was a cache hit

    run_record = store.run_record("vte")
    assert {"pi-scripted", "S1", "synthetic"} <= set(run_record.labels)
    cfg = run_record.config
    assert cfg["task_kind"] == "vte" and cfg["max_executions"] == 4
    assert cfg["orca"]["stand_in"] is True and "sandbox" not in cfg

    j = jobs(store, "vte")
    assert set(j) == {"j0001", "j0002"}
    executed, hit = j["j0001"], j["j0002"]
    assert executed.invocation_kind == "execution" and executed.metadata["executed_here"] is True
    assert {"stand-in", "not-orca", "synthetic"} <= set(executed.labels)
    assert "orca-local" not in executed.labels  # a stand-in is never presented as ORCA
    assert hit.invocation_kind == "read" and hit.metadata["cache_hit"] is True
    assert hit.metadata["executed_here"] is False and "acquired" in hit.labels
    log = store.get_bytes(executed.outputs["log"])
    assert log.startswith(STAND_IN_MARKER.encode()) and log == store.get_bytes(hit.outputs["log"])

    # The broker journal is committed, and is the record ingest checked the spool against.
    journal = store.get_bytes(cfg["broker_journal"])
    assert journal == (root / "vte" / "broker" / "broker.jsonl").read_bytes()
    replies = [e for e in read_journal(root / "vte" / "broker" / "broker.jsonl") if e.job_id]
    assert [(e.job_id, e.executed_here, e.cache_hit) for e in replies] == [
        ("j0001", True, False),
        ("j0002", False, True),
    ]

    task = store.get_record("task", run_record.task)
    assert set(task.inputs) == {"structure", "targets", "case", "ladder"}
    assert_no_references(store)

    ev = pi_vte.evaluate_vte_run(store, evaluator, "vte", case())
    m = ev.metrics
    assert ev.assignments["claim.01"] == "supported"
    assert ev.assignments["claim.01.identity"] == "identified"
    assert ev.assignments["ref.1B1(pi,3s)"] == "identified"
    assert (m["coverage"], m["mae_ev"], m["false_success_claims"]) == ("1", "0.002000", "0")
    assert m["synthetic"] == "true"
    assert not store.list_record_ids("evaluation")  # grading stays in the evaluator store

    report = pi_vte.vte_report(store, evaluator, ["vte"])
    assert pi_vte.vte_report_runs([(store, evaluator, "vte")]) == report  # one store per run
    assert report["synthetic"] is True and "not a chemistry" in report["note"]
    assert report["arms"]["S1"]["coverage"] == 1 and report["arms"]["S1"]["mae_ev"] == "0.002000"
    assert report["arms"]["S1"]["accurate"] == 1 and report["arms"]["S1"]["runs_without_claim"] == 0
    s1 = report["arms"]["S1"]
    assert (s1["jobs"], s1["jobs_failed"], s1["jobs_rejected"]) == (2, 0, 0)


def test_a_run_citing_a_stand_in_job_is_graded_synthetic(recorded, monkeypatch):
    """However the run itself is labelled: e.g. a real-ORCA run served a stand-in's cache entry."""
    _, store, evaluator, _, _ = recorded
    run_record = store.run_record("vte")
    unlabelled = run_record.model_copy(
        update={
            "labels": [x for x in run_record.labels if x != "synthetic"],
            "config": {
                **run_record.config,
                "orca": {**run_record.config["orca"], "stand_in": False},
            },
        }
    )
    monkeypatch.setattr(store, "run_record", lambda run_id: unlabelled)
    assert jobs(store, "vte")["j0002"].metadata["stand_in"] is True  # the cache hit
    ev = pi_vte.evaluate_vte_run(store, evaluator, "vte", case())
    assert ev.metrics["synthetic"] == "true"


def test_replay_of_a_run_with_an_execution_matches_with_zero_executions(recorded):
    root, store, _, binary, _ = recorded
    out = pi_run.replay_run_pi(store, "vte", case(), work_dir=root / "replay")
    assert (out["status"], out["executions"], out["jobs"]) == ("match", 0, 2)
    assert runs(binary) == 1
    replayed = [e for e in read_journal(root / "replay" / "broker" / "broker.jsonl") if e.job_id]
    # The recorded execution replays with neither flag: shown as it was, never executed.
    assert [(e.replay, e.executed_here, e.cache_hit) for e in replayed] == [
        (True, False, False),
        (True, False, True),
    ]


def test_a_reminded_run_records_the_setting_and_replays_to_a_match(short):
    """`report_reminder`: an agent that stops without a claim gets one message, reports,
    and the run (reminder included) replays from its record."""
    store, _ = stores(short)
    binary = stand_in(short)
    turns = [
        [tool("x1", "orca_submit", input=INPUT)],
        [{"type": "text", "text": "stopping without a claim"}],
        [tool("r1", "report_vtes", states=[state("j0001", "6.069")])],
        [{"type": "text", "text": "reported"}],
    ]
    s = Script.model_validate({"source": "test_reminder", "turns": [{"content": t} for t in turns]})
    pi = run(store, short, "remind", binary, s, report_reminder=True)
    assert (pi.ingest.status, pi.ingest.claim is not None) == ("settled", True), pi.stderr_tail
    assert store.run_record("remind").config["report_reminder"] is True
    out = pi_run.replay_run_pi(store, "remind", case(), work_dir=short / "replay")
    assert (out["status"], out["executions"]) == ("match", 0)


def test_replay_of_cache_hits_matches_and_executes_nothing(recorded):
    root, store, _, binary, _ = recorded
    # A second run against the shared cache: every job is a hit, nothing executes.
    pi = run(store, root, "hits", binary, script("test_hits", INPUT))
    assert pi.ingest.status == "settled" and runs(binary) == 1
    assert [r.invocation_kind for r in jobs(store, "hits").values()] == ["read"]

    out = pi_run.replay_run_pi(store, "hits", case(), work_dir=root / "replay")
    assert (out["status"], out["executions"], out["jobs"]) == ("match", 0, 1)
    assert runs(binary) == 1
    replayed = read_journal(root / "replay" / "broker" / "broker.jsonl")
    assert all(e.replay and not e.executed_here for e in replayed if e.event == "reply")


def test_replay_of_a_run_with_an_unspooled_job_fails_closed(short, monkeypatch):
    """A job the broker journaled but no `task.execute` cites has no output in the store."""
    store, _ = stores(short)
    echo = STAND_IN + "sys.stdout.write(open(sys.argv[1]).read())\n"
    binary = stand_in(short, "orca_echo_stand_in.py", echo)
    real_send = OrcaBroker._send

    def drop_j0002(self, wfile, reply):  # the extension never gets this reply to spool
        if reply.job_id == "j0002":
            raise ConnectionError("reply lost (test)")
        real_send(self, wfile, reply)

    monkeypatch.setattr(OrcaBroker, "_send", drop_j0002)
    pi = run(store, short, "lost", binary, script("test_lost", INPUT, INPUT + "# second\n"))
    assert pi.ingest.complete, pi.stderr_tail
    [event] = [e for e in store.events("lost") if e.type == "pi.broker-unspooled"]
    assert event.payload["jobs"] == ["j0002"]
    monkeypatch.undo()
    with pytest.raises(ReplayMismatch):
        pi_run.replay_run_pi(store, "lost", case(), work_dir=short / "replay")
    assert runs(binary) == 2


def test_replay_fails_closed_when_the_task_drifts(recorded, monkeypatch):
    root, store, _, binary, _ = recorded
    run(store, root, "hits", binary, script("test_hits", INPUT))
    real = pi_run.stage_vte_bundle

    def drifted(case_, dest, **kw):  # one more allowed submission changes what structure shows
        return real(case_, dest, **{**kw, "max_executions": kw["max_executions"] + 1})

    monkeypatch.setattr(pi_run, "stage_vte_bundle", drifted)
    with pytest.raises(ReplayMismatch):
        pi_run.replay_run_pi(store, "hits", case(), work_dir=root / "replay")
    assert runs(binary) == 1


def test_a_bundle_holding_the_references_is_refused_before_anything_runs(short):
    store, _ = stores(short)
    binary = stand_in(short)
    refs = (case().root / case().manifest.references.path).read_bytes()
    for i, docs in enumerate(
        ({"refs.md": refs}, {"notes.md": b"see " + REFERENCES_SCHEMA.encode()})
    ):
        with pytest.raises(CapabilityError, match="evaluator-only references"):
            run(store, short, f"leak{i}", binary, script("test_leak", INPUT), docs=docs)
    assert not store.list_record_ids("run") and not store.list_record_ids("task")
    assert not store.list_record_ids("artifact")
    assert runs(binary) == 0


def test_unresolved_live_execution_settings_fail_closed(short):
    store, _ = stores(short)
    with pytest.raises(CapabilityError, match="disabled"):
        pi_run.run_vte_case(
            store,
            case(),
            run_id="off",
            variant_id="S0",
            work_dir=short / "off",
            orca=OrcaSettings(),
            script=script("test_off", INPUT),
        )
    assert not store.list_record_ids("artifact") and not (short / "off").exists()


def test_a_socket_path_too_long_for_af_unix_is_refused(short):
    store, _ = stores(short)
    binary = stand_in(short)
    deep = short / ("d" * 90)
    with pytest.raises(CapabilityError, match="AF_UNIX"):
        pi_run.run_vte_case(
            store,
            case(),
            run_id="deep",
            variant_id="S0",
            work_dir=deep,
            orca=orca(short, binary),
            script=script("test_deep", INPUT),
        )
    assert not store.list_record_ids("artifact")


def test_rlm_variants_are_refused_for_vte(short):
    store, _ = stores(short)
    binary = stand_in(short)
    with pytest.raises(CapabilityError, match="RLM"):
        run(store, short, "rlm", binary, script("test_rlm", INPUT), variant_id="S1r")
    assert not store.list_record_ids("artifact")


@requires_bwrap
def test_sandboxed_run_reaches_the_broker_and_bash_does_not(short):
    store, evaluator = stores(short)
    binary = stand_in(short)
    sock = (short / "sb" / "pi" / pi_run.BROKER_SOCKET).resolve()
    probe = f"if [ -e {sock} ]; then echo SOCK=visible; else echo SOCK=hidden; fi > probe.txt"
    turns = [
        [tool("b1", "bash", command=probe)],
        [tool("x1", "orca_submit", input=INPUT)],
        [tool("r1", "report_vtes", states=[state("j0001")])],
        [{"type": "text", "text": "reported"}],
    ]
    s = Script.model_validate({"source": "test_sb", "turns": [{"content": t} for t in turns]})
    pi = run(store, short, "sb", binary, s, sandbox=detect_sandbox(), variant_id="S0")
    assert (pi.ingest.status, pi.ingest.complete) == ("settled", True), pi.stderr_tail
    assert (short / "sb" / "pi" / "workspace" / "probe.txt").read_text() == "SOCK=hidden\n"
    config = store.run_record("sb").config
    assert config["sandbox"] == "bwrap"
    # The stand-in's job ran in the ORCA job sandbox too (A65).
    assert config["orca"]["job_sandbox"] == "bwrap"
    assert config["orca"]["limits"] == {
        "max_nprocs": 4,
        "max_maxcore_mb": 4000,
        "max_output_bytes": 256 << 20,
    }
    journal = read_journal(short / "sb" / "broker" / "broker.jsonl")
    assert [e.sandbox for e in journal if e.executed_here] == ["bwrap"]
    assert runs(binary) == 1  # Pi reached the broker from inside the outer layer
    assert pi_vte.evaluate_vte_run(store, evaluator, "sb", case()).metrics["coverage"] == "1"


def test_cli_run_evaluate_report(short, capsys):
    binary = stand_in(short)
    script_path = short / "script.json"
    script_path.write_text(script("test_cli", INPUT).model_dump_json(by_alias=True))
    ladder = short / "ladder.md"
    ladder.write_bytes(LADDER)
    agent, ev = str(short / "agent"), str(short / "evaluator")
    common = ["--store", agent, "--cases-root", str(pi_run.VTE_CASES_ROOT)]
    for variant, ladder_args in (
        ("S0", []),
        ("S1", ["--ladder", str(ladder), "--synthetic-ladder"]),
    ):
        argv = [
            "pi", "run-vte", CASE_ID, "--variant", variant, "--run-id", f"cli-{variant}",
            "--work-dir", str(short / variant), "--script", str(script_path),
            "--enable-live-execution", "--orca-binary", str(binary), "--stand-in",
            "--orca-work-root", str(short / "jobs"), "--max-wall-seconds", "60",
            "--orca-cache", str(short / "cache"), "--max-executions", "3", *ladder_args, *common,
        ]  # fmt: skip
        assert main(argv) == 0
        capsys.readouterr()
        assert main(["pi", "evaluate-vte", f"cli-{variant}", "--evaluator-store", ev, *common]) == 0
        assert '"coverage": "1"' in capsys.readouterr().out
    assert runs(binary) == 1  # S1's job was S0's, from the shared cache

    assert (
        main(["vte", "report", "cli-S0", "cli-S1", "--store", agent, "--evaluator-store", ev]) == 0
    )
    out = capsys.readouterr().out
    assert '"common_targets": [\n    "questdb-ketene:ref.1B1(pi,3s)"' in out
    assert '"synthetic": true' in out

    audit = ["vte", "audit", "cli-S0", "cli-S1", *common]
    assert main(audit) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["states"] == 2 and found["false_accepts"] == 0
    assert found["variants"]["casscf_not_converged"] == {"graded": 2, "accepted": 0}

    # Without the live-execution switch nothing runs (every setting defaults off).
    off = ["pi", "run-vte", CASE_ID, "--variant", "S0", "--run-id", "off", "--work-dir",
           str(short / "off"), "--script", str(script_path), *common]  # fmt: skip
    assert main(off) == 2
    assert "disabled" in capsys.readouterr().err


def test_evaluate_refuses_a_run_that_is_not_vte(recorded):
    _, store, evaluator, _, _ = recorded
    with pytest.raises(OnodaError, match="recorded from case"):
        other = load_vte_case(vte_case_manifests(pi_run.VTE_CASES_ROOT)["questdb-acetone"])
        pi_vte.evaluate_vte_run(store, evaluator, "vte", other)


def test_shadow_decisions_are_recorded_beside_the_grade_and_shown_in_the_report(recorded):
    from onoda.adapters.pi.vte_shadow import shadow_report, shadow_run
    from onoda.systemone import ScriptedSystemOne

    root, store, evaluator, binary, pi = recorded
    pi_vte.evaluate_vte_run(store, evaluator, "vte", case())
    states = []

    def answer(state, questions):
        states.append(state)
        assert set(questions["rule"]["criteria"]) == set("ABCDEFGH")
        return {
            "rule": {"type": "choice", "choice": "H", "probabilities": {"H": 0.9, "D": 0.1}},
            "ready": {"type": "noul", "noul": 0.8 if state["job_id"] == "j0001" else 0.3},
        }

    ladder = (Path(__file__).parents[1] / "experiments/questdb/ladder-from-paper.md").read_bytes()
    ev = shadow_run(store, evaluator, "vte", case(), ScriptedSystemOne(answer), ladder=ladder)
    assert ev.evaluator == "vte-shadow-scripted" and ev.metrics["synthetic"] == "true"
    assert ev.assignments["job.j0001.rule"] == "H"
    assert ev.assignments["job.j0001.rule.p"] == {"D": "0.100000", "H": "0.900000"}
    assert not store.list_record_ids("evaluation")  # shadow answers stay in the evaluator store

    # The model sees the targets, the job's own output and the host checks; no reference value.
    first = states[0]
    assert first["target_states"] and "SA-CASSCF TRANSITION ENERGIES" in first["output_excerpt"]
    refs = (case().root / case().manifest.references.path).read_bytes()
    for digest in (ev.output_refs["job.j0001.request"], ev.output_refs["job.j0002.request"]):
        sent = evaluator.get_bytes(digest)
        assert refs not in sent and REFERENCES_SCHEMA.encode() not in sent

    report = shadow_report(store, evaluator, ["vte"])
    s1 = report["arms"]["S1"]["scripted"]
    assert s1["jobs"] == 2 and s1["rules"] == {"H": 2}
    assert s1["ready_p"]["identified"] == "0.800000"  # j0001: the claim cites it, identified
    assert s1["ready_p"]["uncited"] == "0.300000"  # j0002: the cache hit nothing cites
    assert report["synthetic"] is True and "not a model" in report["note"]


# A stand-in for the `kypp` CLI (its remember/recall surface only): keyword OR over
# subject and content, insertion order, JSON Lines in KYPP_MEMORY_DB. Not kypp itself.
FAKE_KYPP = f"""#!/usr/bin/env {sys.executable}
import json, os, re, sys
db, args = os.environ["KYPP_MEMORY_DB"], sys.argv[1:]
if args[0] == "remember":
    with open(db, "a") as f:
        f.write(json.dumps({{"subject": args[1], "content": args[2], "type": args[4]}}) + "\\n")
elif args[0] == "recall":
    limit = int(args[args.index("--limit") + 1])
    terms = [t.lower() for t in args[1 : args.index("--limit")] if re.fullmatch(r"\\w+", t)]
    rows = [json.loads(ln) for ln in open(db)] if os.path.exists(db) else []
    hit = [r for r in rows if any(t in (r["subject"] + r["content"]).lower() for t in terms)]
    print(json.dumps(hit[:limit]))
"""


def test_memory_shadow_scores_kypp_recall_against_the_tiers(recorded):
    from onoda.adapters.pi.vte_memory_shadow import (
        Kypp,
        check_order,
        memory_shadow,
        memory_shadow_report,
        seed,
    )

    root, store, evaluator, binary, _ = recorded  # "vte": its claim is identified
    run(
        store, root, "vte2", binary, script("test_vte_wrong", INPUT, energy="9.999")
    )  # cites j0001 with a value it never printed
    for run_id in ("vte", "vte2"):
        pi_vte.evaluate_vte_run(store, evaluator, run_id, case())
    with pytest.raises(OnodaError, match="would know that run's outcome"):
        check_order(store, ["vte2"], ["vte"])
    check_order(store, ["vte"], ["vte2"])

    kypp_bin = root / "kypp"
    kypp_bin.write_text(FAKE_KYPP)
    kypp_bin.chmod(0o755)
    kypp = Kypp(str(kypp_bin), root / "kypp.jsonl")
    memory = Path(__file__).parents[1] / "experiments" / "questdb" / "memory"
    tiers = {p.name: p.read_text() for p in sorted(memory.glob("memory-*.md"))}
    lessons = seed(kypp, store, evaluator, tiers, ["vte"])
    assert len([x for x in lessons if x.type == "procedure"]) >= 10
    with pytest.raises(OnodaError, match="fresh kypp database"):
        seed(kypp, store, evaluator, tiers, ["vte"])

    ev = memory_shadow(
        store, evaluator, "vte2", kypp, tiers=tiers, lessons=lessons, seed_runs=["vte"]
    )
    a = ev.assignments
    assert a["job.j0001.failures"] == ["claim_value"]
    assert a["job.j0001.tiers.covers"] == ["claim_value"]  # the harness tier names subtraction
    assert set(a["job.j0001.kypp.covers"]) <= {"claim_value"}
    assert int(ev.metrics["tiers.chars"]) > int(a["job.j0001.kypp.chars"]) > 0
    assert ev.metrics["kypp.recall"] in ("keyword", "semantic")
    assert not store.list_record_ids("evaluation")  # the shadow stays in the evaluator store
    refs = (case().root / case().manifest.references.path).read_bytes()
    for digest in ev.output_refs.values():
        sent = evaluator.get_bytes(digest)
        assert refs not in sent and REFERENCES_SCHEMA.encode() not in sent

    report = memory_shadow_report(store, evaluator, ["vte", "vte2"])
    s1 = report["arms"]["S1"]
    assert s1["runs"] == 1 and s1["failures"] == 1 and s1["tiers_covered"] == 1


def test_graded_jobs_become_tuning_examples_and_tuned_wording_is_used(recorded):
    from onoda.adapters.pi.vte_shadow import shadow_examples, shadow_run
    from onoda.systemone import ScriptedSystemOne

    _, store, evaluator, _, _ = recorded
    pi_vte.evaluate_vte_run(store, evaluator, "vte", case())
    (ex,) = shadow_examples(store, evaluator, [("vte", case())])  # j0002: nothing cites it
    assert ex.id == "vte/j0001" and ex.labels == {"ready": "yes"}  # its claim was identified
    assert ex.state["job_id"] == "j0001" and "output_excerpt" in ex.state

    seen = []

    def answer(state, questions):
        seen.append(questions["ready"]["instructions"])
        return {"rule": {"choice": "H", "probabilities": {"H": 1}}, "ready": {"noul": 0.5}}

    ladder = (Path(__file__).parents[1] / "experiments/questdb/ladder-from-paper.md").read_bytes()
    ev = shadow_run(
        store,
        evaluator,
        "vte",
        case(),
        ScriptedSystemOne(answer),
        ladder=ladder,
        instructions={"ready.instructions": "tuned wording"},
    )
    assert set(seen) == {"tuned wording"} and ev.metrics["instructions"].startswith("sha256:")
