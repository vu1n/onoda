"""M4 end to end on the real pinned Pi: an S1r run with one rlm() child and one
commit_state, staged -> launched -> ingested -> evaluated -> replayed.

Offline under the scripted provider (A32): parent and child scripts, the
answer and the claim are synthetic fixtures; the ORCA output the agent reads
is a vendored upstream file recorded as acquired, never executed here.
"""

from __future__ import annotations

import pytest
from conftest import requires_pi
from test_pi_end_to_end import LADDER, case, claim_args, stores

from onoda.ablation.metrics import outcome_vector
from onoda.adapters.pi import run as pi_run
from onoda.adapters.pi.contract import CTX_RLM_STATE, SEED_CANDIDATE, HarnessCandidate, Script
from onoda.adapters.pi.ingest import run_claim
from onoda.adapters.pi.task import evaluate_claims
from onoda.budgets import cost_views
from onoda.errors import ReplayMismatch
from onoda.replay import replay_run

pytestmark = requires_pi

CELL = (
    "unit = rlm('Which unit goes with FINAL SINGLE POINT ENERGY?', context='synthetic test')\n"
    "print(commit_state('unit', {'answer': unit}))\n"
)


def call(i: int, name: str, **args):
    return {"type": "toolCall", "id": f"c{i}", "name": name, "arguments": args}


def rlm_script() -> Script:
    return Script.model_validate(
        {
            "schema": "onoda.pi-script/1",
            "source": "tests/test_rlm_end_to_end.py",
            "turns": [
                {"content": [call(1, "case_input")]},
                {"content": [call(2, "orca_run")]},
                {"content": [call(3, "ipython", code=CELL)]},
                {"content": [call(4, "report_states", **claim_args())]},
                {"content": [{"type": "text", "text": "reported"}]},
            ],
            "children": [
                {
                    "schema": "onoda.pi-script/1",
                    "source": "tests/test_rlm_end_to_end.py:child",
                    "turns": [{"content": [{"type": "text", "text": "Eh"}]}],
                }
            ],
        }
    )


def run_s1r(tmp_path, store):
    return pi_run.run_case(
        store,
        case(),
        run_id="r1",
        variant_id="S1r",
        work_dir=tmp_path / "w",
        script=rlm_script(),
        ladder=LADDER,
        synthetic_ladder=True,
    )


def test_s1r_runs_ingests_evaluates_and_replays(tmp_path):
    store, evaluator = stores(tmp_path)
    pi = run_s1r(tmp_path, store)
    assert (pi.ingest.status, pi.ingest.complete) == ("settled", True), pi.stderr_tail

    run = store.run_record("r1")
    assert run.config["candidate"] == SEED_CANDIDATE.digest()
    (child_event,) = [e for e in store.events("r1") if e.type == "rlm.child"]
    assert child_event.payload["complete"] and child_event.payload["child_run"] == "r1.rlm-0000"
    assert store.get_bytes(child_event.payload_refs["answer"]) == b"Eh"
    child = store.run_record("r1.rlm-0000")
    assert child.config["parent_run"] == "r1"

    final = store.checkpoints("r1")[-1][2]
    (state_digest,) = final.context[CTX_RLM_STATE].values()
    assert store.get_json(state_digest) == {"answer": "Eh"}

    views = cost_views(store, "r1")
    assert views["child_runs"] == ["r1.rlm-0000"]

    claims = [e for e in store.events("r1") if e.type == "pi.claim"]
    evaluation = evaluate_claims(
        evaluator,
        run_id="r1",
        case=case(),
        claim=run_claim(store, "r1"),
        claim_ref=claims[-1].payload_refs["claim"],
    )
    assert evaluation.metrics["false_success_claims"] == "0"
    vector = outcome_vector(store, evaluation, arm="S1r")
    assert vector.status == "settled"
    assert vector.efficiency["requests.agent"] == "6"  # 5 parent requests + 1 child request

    for run_id in ("r1", "r1.rlm-0000"):
        assert replay_run(store, run_id)["external_calls"] == 0
    report = pi_run.replay_run_pi(store, "r1", case(), work_dir=tmp_path / "replay")
    assert report["status"] == "match"


def test_a_changed_child_prompt_fails_the_parents_replay_closed(tmp_path, monkeypatch):
    store, _ = stores(tmp_path)
    run_s1r(tmp_path, store)
    real = pi_run.build_launch

    def drifted(**kw):
        c = kw["candidate"].components
        kw["candidate"] = HarnessCandidate(
            components={**c, "rlm.child_prompt": "Drifted: {prompt}\n{context}"}
        )
        return real(**kw)

    monkeypatch.setattr(pi_run, "build_launch", drifted)
    with pytest.raises(ReplayMismatch):
        pi_run.replay_run_pi(store, "r1", case(), work_dir=tmp_path / "replay")
