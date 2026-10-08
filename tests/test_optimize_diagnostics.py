"""Hill-climb diagnostics: noise (A/A), headroom, case intervals, leaked text. Offline."""

from __future__ import annotations

from test_optimize import SPEC, SPLIT, ScriptedReflection, SpyRunner, journal, optimize, vec
from test_optimize_compare import Runner, check

from onoda.adapters.pi.contract import SEED_CANDIDATE
from onoda.optimize.adapter import Trial
from onoda.optimize.compare import noise_check
from onoda.optimize.contract import CandidateRecord
from onoda.optimize.diagnostics import headroom, intervals, leaked_text


def test_an_interval_that_spans_zero_is_noise():
    base = [vec(supported=3, case="c1"), vec(supported=2, case="c2"), vec(supported=3, case="c3")]
    cand = [vec(supported=2, case="c1"), vec(supported=3, case="c2"), vec(supported=3, case="c3")]
    iv = intervals(base, cand, ["count.supported"])["count.supported"]
    assert iv is not None and iv["delta"] == "0" and iv["excludes_zero"] == "false"


def test_a_gain_on_every_case_excludes_zero():
    base = [vec(supported=2, case=c) for c in ("c1", "c2", "c3")]
    cand = [vec(supported=3, case=c) for c in ("c1", "c2", "c3")]
    iv = intervals(base, cand, ["count.supported"])["count.supported"]
    assert iv == {"delta": "3", "low": "3", "high": "3", "excludes_zero": "true"}


def test_repeats_pool_per_case_and_different_cases_have_no_interval():
    base = [vec(case="c1", run="a"), vec(case="c1", run="b")]
    assert intervals(base, [vec(case="c2")], ["count.supported"])["count.supported"] is None


def test_headroom_names_saturated_and_open_cases():
    room = headroom([vec(case="c1"), vec(case="c2", missing=1), vec(case="c1", run="r2")])
    assert room == {"saturated_cases": ["c1"], "open_cases": ["c2"], "saturated": False}
    assert headroom([vec(case="c1")])["saturated"]


def test_leaked_text_flags_only_what_the_proposal_adds():
    parent = {"rlm.tool_doc": 'e.g. "orca5-dvb-tddft", SCF energy -382.05510710682142 Eh'}
    same = leaked_text(parent, dict(parent), ["orca5-dvb-tddft"])
    assert same == ()
    child = {"rlm.tool_doc": parent["rlm.tool_doc"] + " c2 printed -76.3266523 and 0.196688"}
    assert leaked_text(parent, child, ["c2"]) == (
        "rlm.tool_doc: case id 'c2'",
        "rlm.tool_doc: value -76.3266523",
        "rlm.tool_doc: value 0.196688",
    )


def test_a_proposal_that_copies_a_value_is_invalid_and_never_run(tmp_path):
    runner = SpyRunner()
    optimize(tmp_path, runner, ScriptedReflection(["use -76.3266523 for c1"]), max_reflections=1)
    store, events = journal(tmp_path, "optimizer.candidate")
    records = [
        CandidateRecord.model_validate(store.get_json(e.payload_refs["record"])) for e in events
    ]
    leaked = [r for r in records if r.decision == "invalid"]
    assert len(leaked) == 1 and leaked[0].candidate is None
    assert any("case id 'c1'" in x for x in leaked[0].reasons)
    assert any("value -76.3266523" in x for x in leaked[0].reasons)
    assert len({d for d, _, _ in runner.calls}) == 1  # only the seed ran


class Flaky:
    """Same candidate, different outcomes: side b loses a claim on c1."""

    def run(self, candidate, case_id, *, trial_id):
        lost = trial_id.startswith("n-b-c1")
        outcome = vec(supported=2 if lost else 3, missing=int(lost), case=case_id, run=trial_id)
        return Trial(case_id=case_id, run_id=trial_id, outcome=outcome)


def test_noise_check_reports_what_moved_with_nothing_changed():
    result = noise_check(
        Flaky(), check_id="n", candidate=SEED_CANDIDATE, cases=SPLIT.acceptance, gates=SPEC
    )
    assert result["schema"] == "onoda.noise-check/1" and result["evidence"] == "synthetic"
    assert result["noisy_capability"] == ["count.missing", "count.supported"]
    assert not result["gate_on_self"]["passes"]  # zero tolerance rejects pure noise
    assert result["headroom"]["open_cases"] == ["c1"]


def test_the_promotion_check_carries_intervals_and_headroom_beside_the_verdict():
    result = check(Runner(), repeats=2)
    assert result["accepted"]
    assert result["intervals"]["false_success_claims"]["delta"] == "-4"
    assert result["headroom"]["saturated"] is False  # the baseline still has false successes


def test_unequal_run_counts_per_case_give_no_interval():
    base = [vec(case="c1", run="a"), vec(case="c1", run="b")]
    cand = [vec(case="c1", run="c")]  # one run failed
    assert intervals(base, cand, ["count.supported"]) == {"count.supported": None}


def test_the_cli_module_runs_as_a_script():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "onoda.cli", "optimize", "noise", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--compare-max-cost-usd" in out.stdout
