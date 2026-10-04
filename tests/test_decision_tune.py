"""A70: the decision-wording hill climber, offline (scripted model and reflection)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from onoda.decision_tune import (
    Example,
    Spend,
    apply_candidate,
    check_labels,
    dump_dataset,
    example_score,
    load_dataset,
    seed_candidate,
    split,
    tune,
)
from onoda.errors import OnodaError
from onoda.systemone import ScriptedSystemOne

QUESTIONS = {
    "ready": {"type": "noul", "instructions": "Is this job ready?"},
    "rule": {
        "type": "choice",
        "instructions": "Which rule applies?",
        "criteria": {"A": "failed", "H": "success"},
    },
}
HINT = "look at the converged flag"


def examples(n: int = 12) -> list[Example]:
    return [
        Example(f"j{i:02d}", {"converged": i % 3 != 0}, {"ready": "yes" if i % 3 else "no"})
        for i in range(n)
    ]


def model() -> ScriptedSystemOne:
    """Reads the state only when the wording tells it to; otherwise answers 0.5."""

    def answer(state, questions):
        attends = HINT in questions["ready"]["instructions"]
        p = (0.9 if state["converged"] else 0.1) if attends else 0.5
        return {"ready": {"type": "noul", "noul": p}, "rule": {"probabilities": {"H": 1}}}

    return ScriptedSystemOne(answer)


def test_scores_are_the_probability_of_the_expected_answer():
    assert example_score({"ready": {"noul": 0.8}}, {"ready": "yes"}) == Decimal("0.8")
    assert example_score({"ready": {"noul": 0.8}}, {"ready": "no"}) == Decimal("0.2")
    rule = {"rule": {"choice": "B", "probabilities": {"A": 0.25, "B": 0.75}}}
    assert example_score(rule, {"rule": "A"}) == Decimal("0.25")
    assert example_score({}, {"ready": "yes"}) == 0  # unanswered scores nothing
    both = {"ready": {"noul": 1}, **rule}
    assert example_score(both, {"ready": "yes", "rule": "A"}) == Decimal("0.625")


def test_only_instructions_are_tunable_and_labels_must_fit_the_template():
    assert seed_candidate(QUESTIONS) == {
        "ready.instructions": "Is this job ready?",
        "rule.instructions": "Which rule applies?",
    }
    tuned = apply_candidate(QUESTIONS, {"rule.instructions": "new"})
    assert tuned["rule"]["instructions"] == "new" and tuned["rule"]["criteria"] == {
        "A": "failed",
        "H": "success",
    }
    assert QUESTIONS["rule"]["instructions"] == "Which rule applies?"  # template untouched
    with pytest.raises(OnodaError, match="not a tunable component"):
        apply_candidate(QUESTIONS, {"rule.criteria": "x"})
    with pytest.raises(OnodaError, match="is not one of"):
        check_labels(QUESTIONS, [Example("x", {}, {"rule": "Z"})])
    with pytest.raises(OnodaError, match="no question"):
        check_labels(QUESTIONS, [Example("x", {}, {"other": "yes"})])


def test_dataset_round_trips_and_the_split_is_deterministic(tmp_path):
    path = tmp_path / "ds.jsonl"
    path.write_text(dump_dataset(examples()))
    loaded = load_dataset(path)
    assert loaded == examples()
    train, val = split(loaded, Decimal("0.25"), seed=3)
    assert len(val) == 3 and len(train) == 9
    assert split(loaded, Decimal("0.25"), seed=3) == (train, val)
    path.write_text(dump_dataset(examples()[:1] * 2))
    with pytest.raises(OnodaError, match="ids repeat"):
        load_dataset(path)


def test_the_search_finds_wording_that_reads_the_state(tmp_path):
    prompts = []

    def reflect(prompt):
        prompts.append(prompt)
        return f"```\nIs this job ready? {HINT}.\n```"

    spend = Spend(max_decision_calls=200, max_cost_usd=Decimal("1"))
    result = tune(
        questions=QUESTIONS,
        examples=examples(),
        model=model(),
        reflect=reflect,
        spend=spend,
        cache_dir=tmp_path / "cache",
        max_reflections=2,
    )
    assert HINT in result.best["ready.instructions"]
    assert result.best["rule.instructions"] == "Which rule applies?"  # unlabelled: never touched
    assert Decimal(result.scores["best"]) > Decimal(result.scores["seed"]) == Decimal("0.5")
    assert result.synthetic and result.spend["reflection_calls"] <= 2
    # Reflection is shown the expected answer and the model's answer, worst misses first.
    assert "Expected answer to 'ready'" in prompts[0]
    json.dumps(result.as_json())  # writable as the result file

    # A rerun is answered from the cache: no new decision calls.
    calls = spend.decision_calls
    again = Spend(max_decision_calls=0, max_cost_usd=Decimal("1"))
    rerun = tune(
        questions=QUESTIONS,
        examples=examples(),
        model=model(),
        reflect=reflect,
        spend=again,
        cache_dir=tmp_path / "cache",
        max_reflections=0,
    )
    assert calls > 0 and again.decision_calls == 0 and rerun.scores["seed"] == "0.500000"


def test_the_call_cap_stops_the_search_and_says_why(tmp_path):
    spend = Spend(max_decision_calls=5, max_cost_usd=Decimal("1"))
    result = tune(
        questions=QUESTIONS,
        examples=examples(),
        model=model(),
        reflect=lambda p: f"```\n{HINT}\n```",
        spend=spend,
        cache_dir=tmp_path / "cache",
        max_reflections=5,
    )
    assert spend.decision_calls <= 5
    assert "cap" in result.stop_reason
    assert result.best == result.seed
