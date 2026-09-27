"""ABL-002: `outcome_vector` and the A15 `reducer_probe`.

The four scenario tests run the real pinned Pi (`requires_pi`) under the
scripted provider (A32): S1 (baseline, no mechanisms), S2a (a large output
ObservationPack archives and the agent recalls), S2b (a reducer receipt), and
S2d (a fused write + then_run). The probe itself is also covered standalone,
on a hand-built raw/visible pair where a reducer dropped an energy line.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import requires_pi

from onoda.ablation.contract import (
    ACTIVATION_METRICS,
    CAPABILITY_METRICS,
    EFFICIENCY_METRICS,
    REDUCER_PROBE_METRICS,
)
from onoda.ablation.metrics import outcome_vector, reducer_probe
from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi.contract import Script
from onoda.adapters.pi.ingest import run_claim
from onoda.adapters.pi.task import CLAIM_EVALUATOR, evaluate_claims
from onoda.clock import LogicalClock
from onoda.errors import OnodaError
from onoda.records import Evaluation, Run, Usage
from onoda.science.contract import case_manifests, load_case
from onoda.science.orca_parser import parse_orca_output
from onoda.store import Store

CASE_ID = "orca6-water-mp2"  # a 43 KB output: large enough for ObservationPack
LADDER = b"# Synthetic advisory ladder (test fixture, no scientific content)\n"


def case():
    return load_case(case_manifests()[CASE_ID])


def claim_args() -> dict:
    """A claim copied from the deterministic parse: what a careful agent would report."""
    ((_, out),) = case().output_bytes.items()
    energy = parse_orca_output(out).final_energy
    return {
        "final_energy": {"text": energy.value, "unit": energy.units},
        "states": [],
        "notes": "synthetic scripted claim",
    }


def call(i: int, name: str, **args) -> dict:
    return {"type": "toolCall", "id": f"c{i}", "name": name, "arguments": args}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def stores(tmp_path: Path) -> tuple[Store, Store]:
    return (
        Store(tmp_path / "agent", clock=LogicalClock()),
        Store(tmp_path / "evaluator", clock=LogicalClock()),
    )


def evaluate(store: Store, evaluator: Store, run_id: str) -> Evaluation:
    claims = [e for e in store.events(run_id) if e.type == "pi.claim"]
    return evaluate_claims(
        evaluator,
        run_id=run_id,
        case=case(),
        claim=run_claim(store, run_id),
        claim_ref=claims[-1].payload_refs["claim"] if claims else None,
    )


def assert_vector_shape(vector, evaluation: Evaluation, *, arm: str, case_id: str) -> None:
    assert vector.run_id == evaluation.run_id
    assert vector.arm == arm
    assert vector.case_id == case_id
    assert vector.evaluation_id == evaluation.evaluation_id
    assert set(vector.capability) == set(CAPABILITY_METRICS)
    assert vector.capability == {k: evaluation.metrics[k] for k in CAPABILITY_METRICS}
    assert set(vector.efficiency) == set(EFFICIENCY_METRICS)
    assert set(vector.activation) == set(ACTIVATION_METRICS)
    assert set(vector.reducer_probe) == set(REDUCER_PROBE_METRICS)


# ------------------------------------------------------------------- S1: baseline


@requires_pi
def test_outcome_vector_s1_baseline_has_no_mechanism_activation(tmp_path):
    store, evaluator = stores(tmp_path)
    script = Script.model_validate(
        {
            "source": "test_s1",
            "turns": [
                {"content": [call(1, "case_input"), call(2, "orca_run")]},
                {"content": [call(3, "report_states", **claim_args())]},
                {"content": [text("done")]},
            ],
        }
    )
    pi = pi_run.run_case(
        store,
        case(),
        run_id="s1",
        variant_id="S1",
        work_dir=tmp_path / "w",
        script=script,
        ladder=LADDER,
        synthetic_ladder=True,
    )
    assert pi.ingest.status == "settled"
    evaluation = evaluate(store, evaluator, "s1")
    assert evaluation.metrics["false_success_claims"] == "0"

    vector = outcome_vector(store, evaluation, arm="S1")
    assert_vector_shape(vector, evaluation, arm="S1", case_id=CASE_ID)
    assert vector.model == "scripted"
    assert vector.synthetic is True
    assert vector.status == "settled"
    assert vector.mechanism_config is None  # S1 has no mechanisms (S0/S1)
    assert vector.activation == {k: "0" for k in ACTIVATION_METRICS}
    assert vector.reducer_probe == {k: "0" for k in REDUCER_PROBE_METRICS}
    # One acquisition (orca_run), three agent requests, no reducer/summary traffic.
    assert vector.efficiency["acquisitions"] == "1"
    assert vector.efficiency["requests.agent"] == "3"
    assert vector.efficiency["requests.reducer"] == "0"
    assert vector.efficiency["requests.summary"] == "0"
    assert vector.efficiency["context_bytes.reducer"] == "0"
    assert vector.efficiency["context_bytes.summary"] == "0"
    assert int(vector.efficiency["context_bytes.agent"]) > 0
    assert int(vector.efficiency["context_bytes.total"]) == int(
        vector.efficiency["context_bytes.agent"]
    )
    # The scripted provider reports a (zero) figure on every response: not None.
    assert vector.efficiency["tokens.total"] is not None
    assert vector.efficiency["cost_usd"] is not None


# ------------------------------------------------------------------- S2a: ObservationPack


def observation_id(tool_name: str, call_id: str, text_: str) -> str:
    """SoL-Pi ObservationPack's id for a pure-text result (mirrors test_pi_extensions)."""
    content = hashlib.sha256(text_.encode()).hexdigest()
    return "obs_" + hashlib.sha256(f"{tool_name}\0{call_id}\0{content}".encode()).hexdigest()[:24]


@requires_pi
def test_outcome_vector_s2a_counts_archive_and_recall(tmp_path):
    store, evaluator = stores(tmp_path)
    ((_, out),) = case().output_bytes.items()
    obs = observation_id("orca_run", "c2", out.decode())
    script = Script.model_validate(
        {
            "source": "test_s2a",
            "turns": [
                {"content": [call(1, "case_input"), call(2, "orca_run")]},
                {"content": [call(3, "obs_recall", id=obs, offset=0)]},
                {"content": [call(4, "report_states", **claim_args())]},
                {"content": [text("done")]},
            ],
        }
    )
    pi = pi_run.run_case(
        store,
        case(),
        run_id="s2a",
        variant_id="S2a",
        work_dir=tmp_path / "w",
        script=script,
        ladder=LADDER,
        synthetic_ladder=True,
    )
    assert pi.ingest.status == "settled"
    assert len(pi.ingest.observation_pack) >= 1  # the 43 KB output crossed the archive threshold
    evaluation = evaluate(store, evaluator, "s2a")

    vector = outcome_vector(store, evaluation, arm="S2a")
    assert_vector_shape(vector, evaluation, arm="S2a", case_id=CASE_ID)
    assert vector.activation["observationPack"] == str(len(pi.ingest.observation_pack))
    assert vector.activation["observationPack.recall"] == "1"
    assert vector.activation["evidencePreservingReducer"] == "0"
    assert vector.activation["onlineContextCompact"] == "0"
    assert vector.activation["actionFusion"] == "0"
    assert vector.mechanism_config is not None  # S2a loads a sol-pi.json


# ------------------------------------------------------------------- S2b: reducer receipt


@requires_pi
def test_outcome_vector_s2b_counts_the_reduced_result(tmp_path):
    from test_pi_extensions import REDUCER_COMMAND, REDUCER_LOG, receipt, sha

    store, evaluator = stores(tmp_path)
    script = Script.model_validate(
        {
            "source": "test_s2b",
            "turns": [
                {"content": [call(1, "bash", command=REDUCER_COMMAND)]},
                {"content": [call(2, "report_states", **claim_args())]},
                {"content": [text("done")]},
            ],
            "reducer_turns": [{"content": [text(receipt(sha(REDUCER_LOG.encode())))]}],
        }
    )
    pi = pi_run.run_case(
        store,
        case(),
        run_id="s2b",
        variant_id="S2b",
        work_dir=tmp_path / "w",
        script=script,
        ladder=LADDER,
        synthetic_ladder=True,
    )
    assert pi.ingest.status == "settled"
    assert "context.reduced" in [e.type for e in store.events("s2b")]
    evaluation = evaluate(store, evaluator, "s2b")

    vector = outcome_vector(store, evaluation, arm="S2b")
    assert_vector_shape(vector, evaluation, arm="S2b", case_id=CASE_ID)
    assert vector.activation["evidencePreservingReducer"] == "1"
    assert vector.efficiency["requests.reducer"] == "1"
    assert int(vector.efficiency["context_bytes.reducer"]) > 0
    # The synthetic bash log is not ORCA output: the probe finds no evidence lines in it.
    assert vector.reducer_probe == {"reducer.evidence_lines": "0", "reducer.evidence_lost": "0"}


# ------------------------------------------------------------------- S2d: action fusion


@requires_pi
def test_outcome_vector_s2d_counts_one_fused_call(tmp_path):
    store, evaluator = stores(tmp_path)
    script = Script.model_validate(
        {
            "source": "test_s2d",
            "turns": [
                {
                    "content": [
                        call(
                            1,
                            "write",
                            path="a.txt",
                            content="hi\n",
                            then_run={"command": "cat a.txt"},
                        )
                    ]
                },
                {"content": [call(2, "case_input"), call(3, "orca_run")]},
                {"content": [call(4, "report_states", **claim_args())]},
                {"content": [text("done")]},
            ],
        }
    )
    pi = pi_run.run_case(
        store,
        case(),
        run_id="s2d",
        variant_id="S2d",
        work_dir=tmp_path / "w",
        script=script,
        ladder=LADDER,
        synthetic_ladder=True,
    )
    assert pi.ingest.status == "settled"
    events = store.events("s2d")
    assert [e.type for e in events].count("context.fused-step") == 2
    evaluation = evaluate(store, evaluator, "s2d")

    vector = outcome_vector(store, evaluation, arm="S2d")
    assert_vector_shape(vector, evaluation, arm="S2d", case_id=CASE_ID)
    assert vector.activation["actionFusion"] == "1"  # one fusion id over two fused-step events
    assert vector.activation["observationPack"] == "0"
    assert vector.activation["evidencePreservingReducer"] == "0"
    assert vector.activation["onlineContextCompact"] == "0"


# ------------------------------------------------------------------- refusals (no Pi needed)


def _fake_run(store: Store, run_id: str, *, variant: str) -> None:
    store.put_record(
        Run.new(
            run_id=run_id,
            experiment_id="pi-dev",
            task="t",
            config={"mode": "pi-scripted", "variant": variant, "case_id": CASE_ID},
            quota_id="q",
            created_at=store.clock.now(),
            labels=["pi-scripted", variant, "synthetic"],
        )
    )


def _fake_evaluation(run_id: str, *, evaluator: str) -> Evaluation:
    return Evaluation.new(
        evaluation_id=f"{run_id}.e001",
        run_id=run_id,
        evaluator=evaluator,
        evaluator_version="1",
        output_refs={},
        reference_version="test",
        assignments={},
        unresolved=[],
        metrics={k: "0" for k in CAPABILITY_METRICS},
        usage=Usage(units={}, measured=False, simulated=False, cost_usd=None),
        created_at="1970-01-01T00:00:00Z",
    )


def test_outcome_vector_refuses_a_mismatched_arm(tmp_path):
    store = Store(tmp_path / "agent", clock=LogicalClock())
    _fake_run(store, "r", variant="S1")
    evaluation = _fake_evaluation("r", evaluator=CLAIM_EVALUATOR)
    with pytest.raises(OnodaError, match="S2a"):
        outcome_vector(store, evaluation, arm="S2a")


def test_outcome_vector_refuses_a_non_claim_evaluation(tmp_path):
    store = Store(tmp_path / "agent", clock=LogicalClock())
    _fake_run(store, "r", variant="S1")
    evaluation = _fake_evaluation("r", evaluator="some-other-evaluator")
    with pytest.raises(OnodaError, match="claim evaluator"):
        outcome_vector(store, evaluation, arm="S1")


# ------------------------------------------------------------------- reducer_probe (pure)


def _content(*texts: str) -> bytes:
    return json.dumps([{"type": "text", "text": t} for t in texts]).encode()


def test_reducer_probe_counts_a_dropped_energy_line():
    raw_text = (
        "Program Version 6.0.1\n"
        "SCF CONVERGED AFTER 9 CYCLES\n"
        "FINAL SINGLE POINT ENERGY  -76.123456789012\n"
        "****ORCA TERMINATED NORMALLY****\n"
    )
    # The reducer's receipt keeps the SCF line but drops the final-energy line.
    visible_text = "sol_pi_evidence_receipt_v1: SCF CONVERGED AFTER 9 CYCLES; normal termination.\n"
    probe = reducer_probe(_content(raw_text), _content(visible_text))
    assert probe["reducer.evidence_lines"] == "1"  # only the final-energy line has evidence
    assert probe["reducer.evidence_lost"] == "1"


def test_reducer_probe_finds_nothing_lost_when_the_evidence_line_survives():
    raw_text = (
        "Program Version 6.0.1\n"
        "SCF CONVERGED AFTER 9 CYCLES\n"
        "FINAL SINGLE POINT ENERGY  -76.123456789012\n"
        "****ORCA TERMINATED NORMALLY****\n"
    )
    visible_text = "The output ends with: FINAL SINGLE POINT ENERGY  -76.123456789012\n"
    probe = reducer_probe(_content(raw_text), _content(visible_text))
    assert probe["reducer.evidence_lines"] == "1"
    assert probe["reducer.evidence_lost"] == "0"


def test_reducer_probe_over_non_orca_text_has_no_evidence():
    probe = reducer_probe(_content("synthetic reducer log line\n" * 5), _content("summary"))
    assert probe == {"reducer.evidence_lines": "0", "reducer.evidence_lost": "0"}
