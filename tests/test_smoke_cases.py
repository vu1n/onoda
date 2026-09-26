"""M2 end to end: authentic ORCA cases recorded, parsed, and evaluated (T17 subset, T18).

T17 proper needs an operator-enabled ORCA; here the authentic outputs are
acquired from pinned upstream bytes and every run says it was not executed
here. The live path is exercised only with a labelled stand-in executable.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from labrun.clock import LogicalClock
from labrun.errors import CapabilityError, IntegrityError
from labrun.science.contract import case_manifests, load_case
from labrun.science.orca_local import OrcaLocalScheduler, OrcaSettings
from labrun.science.orca_parser import parse_orca_output
from labrun.science.runner import evaluate_case_run, final_observation, record_case_run
from labrun.store import Store

CASES = case_manifests()


def stores(tmp_path: Path) -> tuple[Store, Store]:
    agent = Store(tmp_path / "agent", clock=LogicalClock())
    evaluator = Store(
        tmp_path / "evaluator", clock=LogicalClock(), allowed_visibility=("evaluator",)
    )
    return agent, evaluator


def case(case_id: str):
    return load_case(CASES[case_id])


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_every_pinned_case_records_parses_and_evaluates(tmp_path, case_id):
    agent, evaluator = stores(tmp_path)
    c = case(case_id)
    cr = record_case_run(agent, c, "run")

    digest, obs = final_observation(agent, "run")
    assert digest == cr.observation
    assert obs.synthetic is False
    assert obs.termination == "normal"
    assert agent.get_bytes(cr.log) == c.output_bytes["log"]

    acquire = agent.receipt("run.i001")
    assert acquire.invocation_kind == "read"
    assert acquire.metadata["executed_here"] is False
    assert acquire.metadata["provenance"]["commit"] == c.manifest.provenance.commit
    assert acquire.usage.cost_usd is None  # unknown, never zero
    assert agent.descriptor(cr.log).license == "BSD-3-Clause"

    ev = evaluate_case_run(agent, evaluator, "run", c, policy="orca-reexecution@1")
    assert ev.assignments["execution_status"] == "pass"
    assert ev.assignments["parse_integrity"] == "pass"
    assert ev.metrics["case.review"] == "pending-human-review"


def test_cross_version_discrepancy_is_reported_and_left_unresolved(tmp_path):
    agent, evaluator = stores(tmp_path)
    record_case_run(agent, case("orca6-dvb-tddft"), "td6")
    ev = evaluate_case_run(
        agent,
        evaluator,
        "td6",
        case("orca6-dvb-tddft"),
        policy="orca-reexecution@1",
        reference=case("orca5-dvb-tddft"),
    )
    # ORCA 6.0 prints 0.196688 au, ORCA 5.0 prints 0.196682 au for the same input.
    assert ev.metrics["diff.energy_au.m1.r1"] == "0.000006"
    assert ev.metrics["diff.final_energy"] == "0.000007562582"
    assert ev.assignments["state_identification"] == "pass"
    assert ev.assignments["reference_comparison"] == "unresolved"
    assert "reference_comparison" in ev.unresolved
    assert ev.metrics["reference_comparison.reason"] == "tolerance unresolved (A6)"
    assert ev.reference_version.startswith("case:orca5-dvb-tddft@")


def test_reference_stays_out_of_the_agent_store(tmp_path):
    agent, evaluator = stores(tmp_path)
    record_case_run(agent, case("orca6-dvb-tddft"), "td6")
    before = sorted(p.relative_to(agent.root) for p in agent.root.rglob("*") if p.is_file())
    evaluate_case_run(
        agent,
        evaluator,
        "td6",
        case("orca6-dvb-tddft"),
        policy="orca-reexecution@1",
        reference=case("orca5-dvb-tddft"),
    )
    after = sorted(p.relative_to(agent.root) for p in agent.root.rglob("*") if p.is_file())
    assert before == after
    reference_log = case("orca5-dvb-tddft").manifest.outputs["log"].sha256
    assert not agent.has_object(reference_log)


def test_eom_states_without_printed_multiplicity_stay_unresolved(tmp_path):
    agent, evaluator = stores(tmp_path)
    c = case("orca6-dvb-eom-ccsd")
    record_case_run(agent, c, "eom")
    _, obs = final_observation(agent, "eom")
    assert [s.multiplicity for s in obs.excited_states] == [None, None]
    ev = evaluate_case_run(agent, evaluator, "eom", c, policy="orca-reexecution@1")
    assert ev.assignments["state_identification"] == "unresolved"


def test_tampered_case_bytes_are_refused(tmp_path):
    src = CASES["orca6-water-mp2"]
    work = tmp_path / "cases"
    work.mkdir()
    manifest = work / src.name
    manifest.write_text(src.read_text())
    m = load_case(src).manifest
    for pinned in (m.input, m.outputs["log"]):
        dest = work / pinned.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((src.parent / pinned.path).read_bytes())
    load_case(manifest)
    log = work / m.outputs["log"].path
    log.write_bytes(log.read_bytes().replace(b"-74.999374598107", b"-74.999374598108"))
    with pytest.raises(IntegrityError):
        load_case(manifest)


def test_live_execution_is_refused_before_anything_is_recorded(tmp_path):
    agent, _ = stores(tmp_path)
    with pytest.raises(CapabilityError):
        record_case_run(
            agent, case("orca6-water-mp2"), "live", scheduler=OrcaLocalScheduler(OrcaSettings())
        )
    assert not agent.has_record("run", "live")


def _stand_in(tmp_path: Path, body: str) -> str:
    path = tmp_path / "stand-in"
    path.write_text(f"#!/usr/bin/env python3\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_stand_in_execution_is_recorded_as_synthetic_and_never_passes(tmp_path):
    agent, evaluator = stores(tmp_path)
    binary = _stand_in(
        tmp_path, 'print("STAND-IN EXECUTABLE - NOT ORCA")\nprint("output ends here")'
    )
    scheduler = OrcaLocalScheduler(
        OrcaSettings(
            enabled=True,
            binary=binary,
            work_root=str(tmp_path / "jobs"),
            max_wall_seconds=30,
            stand_in=True,
        )
    )
    c = case("orca6-water-mp2")
    cr = record_case_run(agent, c, "live", scheduler=scheduler)
    assert cr.mode == "case-live"
    execute = agent.receipt("live.i001")
    assert execute.invocation_kind == "execution"
    assert "stand-in" in execute.labels
    assert execute.metadata["binary_sha256"].startswith("sha256:")

    _, obs = final_observation(agent, "live")
    assert obs.synthetic is True
    assert obs.termination == "absent"
    assert obs.final_energy is None
    assert {p.field for p in obs.problems} >= {"termination", "final_energy"}

    ev = evaluate_case_run(agent, evaluator, "live", c, policy="orca-reexecution@1")
    assert "pass" not in (ev.assignments["execution_status"], ev.assignments["parse_integrity"])
    assert ev.metrics["observation.synthetic"] == "true"


def test_undecodable_output_is_an_explicit_problem():
    data = case("orca6-water-mp2").output_bytes["log"] + b"\xff\xfe broken\n"
    obs = parse_orca_output(data, synthetic=True)
    assert any(p.field == "source" and p.state == "error" for p in obs.problems)
