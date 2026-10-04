"""A54: the promotion gate check, offline over a fake runner."""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_optimize import SPEC, vec

from onoda.adapters.pi.contract import INITIAL_SEED_CANDIDATE, SEED_CANDIDATE
from onoda.cli import main
from onoda.errors import OnodaError
from onoda.optimize.adapter import Trial
from onoda.optimize.compare import compare_candidates


class Runner:
    """The promoted seed removes the initial seed's false-success claims."""

    def __init__(self, fail: str | None = None, unpriced: str | None = None):
        self.calls: list[str] = []
        self.fail = fail
        self.unpriced = unpriced

    def run(self, candidate, case_id, *, trial_id):
        self.calls.append(trial_id)
        if trial_id == self.fail:
            raise RuntimeError("provider went away")
        better = candidate == SEED_CANDIDATE
        outcome = vec(
            supported=3 if better else 2,
            fs=0 if better else 1,
            case=case_id,
            run=trial_id,
            cost=None if trial_id == self.unpriced else "0.01",
        )
        return Trial(case_id=case_id, run_id=trial_id, outcome=outcome)


def check(runner, **kw):
    return compare_candidates(
        runner,
        check_id="p",
        baseline=INITIAL_SEED_CANDIDATE,
        candidate=SEED_CANDIDATE,
        cases=("c1", "c2"),
        gates=SPEC.model_copy(update={"capability_gain": True}),
        **kw,
    )


def test_a_capability_gain_passes_the_gates_in_whole_rounds():
    runner = Runner()
    result = check(runner, repeats=2)
    assert result["accepted"] and result["evidence"] == "synthetic"
    assert "capability gain: false_success_claims 4 -> 0" in result["reasons"]
    assert runner.calls[:2] == ["p-baseline-c1-r1", "p-candidate-c1-r1"]
    assert result["rounds"] == {"planned": 4, "completed": 4} and result["spent_usd"] == "0.08"


def test_the_budget_stops_before_a_round_it_cannot_cover():
    result = check(
        Runner(), repeats=2, max_cost_usd=Decimal("0.05"), run_reserve_usd=Decimal("0.01")
    )
    assert result["rounds"]["completed"] == 2 and result["stopped"].startswith("budget")


def test_a_failed_run_never_promotes():
    result = check(Runner(fail="p-baseline-c2-r1"))
    assert not result["accepted"]
    assert result["failed"] == [{"run": "p-baseline-c2-r1", "error": "RuntimeError"}]


CAP = {"max_cost_usd": Decimal("1"), "run_reserve_usd": Decimal("0.10")}


def test_a_failed_run_is_charged_its_reserve_under_a_cap():
    result = check(Runner(fail="p-baseline-c1-r1"), **CAP)
    assert result["spent_usd"] == "0.13"  # the reserve for the failed run, 0.01 for the rest


def test_an_unknown_cost_stops_the_check_and_never_promotes():
    result = check(Runner(unpriced="p-candidate-c1-r1"), repeats=2, **CAP)
    assert result["stopped"] == "run p-candidate-c1-r1 reported no cost; the check stops"
    assert result["rounds"]["completed"] == 1 and not result["accepted"]


def test_an_unknown_cost_without_a_cap_is_not_a_stop():
    assert check(Runner(unpriced="p-candidate-c1-r1"))["stopped"] is None


def test_a_candidate_is_not_checked_against_itself():
    with pytest.raises(OnodaError, match="same candidate"):
        compare_candidates(
            Runner(),
            check_id="p",
            baseline=SEED_CANDIDATE,
            candidate=SEED_CANDIDATE,
            cases=("c1",),
            gates=SPEC,
        )


def test_the_cli_compare_is_live_only(tmp_path, capsys):
    code = main(
        [
            "optimize",
            "compare",
            "--candidate",
            "c.json",
            "--split",
            "s.json",
            "--gates",
            "g.yaml",
            "--check-id",
            "p",
            "--store",
            str(tmp_path / "a"),
            "--evaluator-store",
            str(tmp_path / "e"),
            "--work-dir",
            str(tmp_path / "w"),
        ]
    )
    assert code == 2 and "live only" in capsys.readouterr().err
