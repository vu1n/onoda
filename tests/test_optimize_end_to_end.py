"""M4 optimizer end to end: a scripted two-proposal GEPA search over real Pi runs.

Every trial is a real pinned Pi process under the scripted provider (A32), so
every claim, answer and reflection here is a synthetic fixture and nothing is
a harness improvement. The scripted "model" copies values exactly unless the
candidate's system prompt tells it to guess, which is how the test makes the
capability gate reject one proposal while the other is accepted on measured
payload bytes (A40: context bytes are measured, a proxy for tokens).
"""

from __future__ import annotations

import json

import pytest
from conftest import requires_pi
from test_pi_end_to_end import LADDER, stores

from onoda.ablation.contract import CAPABILITY_METRICS, Tolerances
from onoda.adapters.pi.contract import SEED_CANDIDATE, HarnessCandidate, Script
from onoda.cli import main
from onoda.clock import LogicalClock
from onoda.optimize.contract import CandidateRecord, GateSpec, SplitAssignment
from onoda.optimize.loop import run_optimization
from onoda.optimize.pi_runner import PiRunner
from onoda.science.contract import case_manifests, load_case
from onoda.science.orca_parser import parse_orca_output
from onoda.store import Store

pytestmark = requires_pi

CASES = ("orca6-water-mp2", "orca6-dvb-tddft")
SPLIT = SplitAssignment(seed=1, acceptance=CASES, final=("orca5-dvb-tddft",), exploratory=True)
SPEC = GateSpec(
    tolerances=Tolerances(
        capability={m: "0" for m in CAPABILITY_METRICS},
        justification="test fixture tolerances (A47), not operator-chosen",
    ),
    efficiency_gate=("context_bytes.total",),
)
SLOPPY = "SLOPPY: estimate values instead of copying them."
TERSE = "Use the ipython REPL; copy every value exactly as printed."


def script(case_id: str, *, guess: bool) -> Script:
    ((_, out),) = load_case(case_manifests()[case_id]).output_bytes.items()
    e = parse_orca_output(out).final_energy
    value = e.value + "1" if guess else e.value  # a guess contradicts the parser

    def call(i: int, name: str, **args):
        return {"type": "toolCall", "id": f"c{i}", "name": name, "arguments": args}

    return Script.model_validate(
        {
            "schema": "onoda.pi-script/1",
            "source": f"tests/test_optimize_end_to_end.py:{'guess' if guess else 'copy'}",
            "turns": [
                {"content": [call(1, "case_input")]},
                {"content": [call(2, "orca_run")]},
                {
                    "content": [
                        call(3, "report_states", final_energy={"text": value, "unit": e.units})
                    ]
                },
                {"content": [{"type": "text", "text": "reported"}]},
            ],
        }
    )


class PromptSensitiveRunner(PiRunner):
    """The scripted stand-in for a model: it guesses when the prompt says to."""

    def script_for(self, candidate: HarnessCandidate, case_id: str) -> Script:
        return script(case_id, guess="SLOPPY" in candidate.components["rlm.system_prompt"])


class Reflection:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"```\n{self.replies.pop(0)}\n```"


def test_two_proposals_one_rejected_one_accepted_on_real_pi(tmp_path):
    store, evaluator = stores(tmp_path)
    runner = PromptSensitiveRunner(
        store=store,
        evaluator_store=evaluator,
        work_dir=tmp_path / "trials",
        variant_id="S1r",
        scripts={},
        ladder=LADDER,
        synthetic_ladder=True,
    )
    reflect = Reflection([SLOPPY, TERSE])
    opt = Store(tmp_path / "opt", clock=LogicalClock())
    result = run_optimization(
        runner=runner,
        seed=SEED_CANDIDATE,
        split=SPLIT,
        gates=SPEC,
        reflect=reflect,
        store=opt,
        search_id="e2e",
        run_dir=tmp_path / "gepa",
        max_trials=12,
        max_reflections=2,
        minibatch_size=2,
    )
    records = [
        CandidateRecord.model_validate(opt.get_json(e.payload_refs["record"]))
        for e in opt.events("e2e")
        if e.type == "optimizer.candidate"
    ]
    decisions = [(r.origin, r.decision) for r in records]
    assert decisions == [("seed", "seed"), ("reflection", "rejected"), ("reflection", "accepted")]
    rejected, accepted = records[1], records[2]
    assert rejected.components["rlm.system_prompt"] == SLOPPY
    assert any("false-success" in r or "capability" in r for r in rejected.reasons)
    # GEPA's round-robin selector moves on to the next component (the tool doc).
    changed = {k for k, v in accepted.components.items() if SEED_CANDIDATE.components[k] != v}
    assert changed == {"rlm.tool_doc"} and accepted.components["rlm.tool_doc"] == TERSE
    assert any("context_bytes.total" in r for r in accepted.reasons)
    assert [r.candidate for r in result.accepted] == [accepted.candidate]

    # Every trial is a real recorded run of an acceptance case, never a final one.
    trial_runs = [e.payload["trial_id"] for e in opt.events("e2e") if e.type == "optimizer.trial"]
    assert trial_runs and all(store.has_record("run", r) for r in trial_runs)
    assert {store.run_record(r).config["case_id"] for r in trial_runs} == set(CASES)
    # The reflection saw the agent's trace, never the ORCA reference itself as a grader input.
    assert reflect.prompts and "report_states" in reflect.prompts[0]


def test_cli_refuses_unset_tolerances_and_live_search(tmp_path, capsys):
    gates = tmp_path / "gates.yaml"
    gates.write_text(
        "tolerances:\n  justification: fixture\n  capability:\n    count.supported: '0'\n"
        "efficiency_gate: [context_bytes.total]\n"
    )
    split = tmp_path / "split.json"
    split.write_text(SPLIT.model_dump_json(by_alias=True))
    common = [
        "optimize", "run", "--split", str(split), "--gates", str(gates),
        "--search-id", "s", "--store", str(tmp_path / "a"),
        "--evaluator-store", str(tmp_path / "e"), "--optimizer-store", str(tmp_path / "o"),
        "--work-dir", str(tmp_path / "w"), "--max-trials", "4", "--max-reflections", "1",
    ]  # fmt: skip
    assert main([*common, "--scripts", str(tmp_path), "--reflection-script", str(split)]) != 0
    err = capsys.readouterr().err
    assert "unresolved" in err or "tolerance" in err
    assert main([*common, "--live"]) != 0
    assert "authorized" in capsys.readouterr().err
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize("final_count", [1, 2])
def test_cli_split_is_exploratory_over_pending_cases(capsys, final_count):
    assert main(["optimize", "split", "--seed", "3", "--final-count", str(final_count)]) == 0
    split = json.loads(capsys.readouterr().out)
    assert split["exploratory"] is True
    assert len(split["final"]) == final_count
