"""Protected evaluation derivations (T16; T13 evaluator side)."""

from __future__ import annotations

import pytest

from onoda.clock import LogicalClock
from onoda.errors import CapabilityError, VisibilityError
from onoda.evaluation import compare_evaluations, evaluate_run, publish_reference
from onoda.fixture import DEFAULT_PARAMS
from onoda.store import Store


def evaluator_store(env, name: str = "evaluator") -> Store:
    return Store(env.root / name, clock=LogicalClock(), allowed_visibility=("evaluator",))


# --------------------------------------------------------------------- T16


def test_two_graders_over_same_run_coexist_with_distinct_versions(make_env):
    env = make_env()
    env.controller.run_fixture("demo")
    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)

    ev1 = evaluate_run(env.store, estore, "demo", grader="fixture-grader@1", reference=ref)
    ev2 = evaluate_run(env.store, estore, "demo", grader="fixture-grader@2", reference=ref)

    assert ev1.evaluator_version != ev2.evaluator_version
    assert ev1.evaluation_id != ev2.evaluation_id
    assert estore.has_record("evaluation", ev1.evaluation_id)
    assert estore.has_record("evaluation", ev2.evaluation_id)
    # grader@2 adds the warning-free-round check that grader@1 does not have.
    assert "warning_free_round" not in ev1.assignments
    assert "warning_free_round" in ev2.assignments


def test_rerunning_a_grader_creates_new_record_and_keeps_the_first_byte_identical(make_env):
    env = make_env()
    env.controller.run_fixture("demo")
    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)

    first = evaluate_run(env.store, estore, "demo", grader="fixture-grader@1", reference=ref)
    path = estore.root / "records" / "evaluation" / f"{first.evaluation_id}.json"
    before = path.read_bytes()

    second = evaluate_run(env.store, estore, "demo", grader="fixture-grader@1", reference=ref)

    assert second.evaluation_id != first.evaluation_id
    assert path.read_bytes() == before
    assert estore.get_record("evaluation", first.evaluation_id) == first


def test_compare_evaluations_refuses_mixed_evaluator_versions(make_env):
    env = make_env()
    env.controller.run_fixture("demo")
    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)

    ev1 = evaluate_run(env.store, estore, "demo", grader="fixture-grader@1", reference=ref)
    ev2 = evaluate_run(env.store, estore, "demo", grader="fixture-grader@2", reference=ref)

    with pytest.raises(CapabilityError):
        compare_evaluations([ev1, ev2])


def test_compare_evaluations_over_same_version_covers_two_runs(make_env):
    env = make_env()
    st1 = env.controller.run_fixture("v1-run", policy="mock-policy-v1")
    st2 = env.controller.run_fixture("v2-run", policy="mock-policy-v2")
    assert st1.done and st2.done
    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)

    ev1 = evaluate_run(env.store, estore, "v1-run", grader="fixture-grader@1", reference=ref)
    ev2 = evaluate_run(env.store, estore, "v2-run", grader="fixture-grader@1", reference=ref)

    result = compare_evaluations([ev1, ev2])
    assert set(result["runs"]) == {"v1-run", "v2-run"}
    assert result["evaluator_version"] == "1"
    for row in result["runs"].values():
        assert row["assignments"]["state_identification"] == "pass"
        assert row["assignments"]["execution_status"] == "pass"


def test_compare_evaluations_refuses_empty_list():
    with pytest.raises(CapabilityError):
        compare_evaluations([])


# ------------------------------------------------------------- unresolved


def test_missing_min_point_is_unresolved_never_pass(make_env):
    env = make_env()
    env.controller.run_fixture(
        "sparse", params={"system": "fixture-A", "points": 0, "seed": 7}, max_rounds=1
    )
    final = env.store.get_json(env.controller.state("sparse").visible["final"])
    assert "min_point" not in final["final_observation"]

    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)
    ev = evaluate_run(env.store, estore, "sparse", grader="fixture-grader@1", reference=ref)

    assert ev.assignments["state_identification"] == "unresolved"
    assert "state_identification" in ev.unresolved
    assert ev.assignments["state_identification"] != "pass"


def test_unknown_grader_is_rejected(make_env):
    env = make_env()
    env.controller.run_fixture("demo")
    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)
    with pytest.raises(CapabilityError):
        evaluate_run(env.store, estore, "demo", grader="no-such-grader@9", reference=ref)


# ------------------------------------------------------------------- T13


def test_agent_store_refuses_evaluator_visibility_artifacts(make_env):
    env = make_env()
    with pytest.raises(VisibilityError):
        env.store.put_json({"secret": "reference"}, visibility="evaluator")


def test_reference_never_appears_in_agent_store(make_env):
    from onoda.canonical import canonical_bytes

    env = make_env()
    env.controller.run_fixture("demo")
    estore = evaluator_store(env)
    ref_digest = publish_reference(estore, DEFAULT_PARAMS)
    reference_value = estore.get_json(ref_digest)
    reference_bytes = canonical_bytes(reference_value)
    digest_bytes = ref_digest.encode()

    for path in env.store.root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        assert digest_bytes not in data, f"reference digest leaked into agent store file {path}"
        assert reference_bytes not in data, f"reference bytes leaked into agent store file {path}"


def test_evaluation_records_live_only_under_evaluator_store(make_env):
    env = make_env()
    env.controller.run_fixture("demo")
    estore = evaluator_store(env)
    ref = publish_reference(estore, DEFAULT_PARAMS)
    ev = evaluate_run(env.store, estore, "demo", grader="fixture-grader@1", reference=ref)

    assert estore.has_record("evaluation", ev.evaluation_id)
    assert not env.store.has_record("evaluation", ev.evaluation_id)
    assert env.store.list_record_ids("evaluation") == []


def test_evaluator_store_holds_the_reference_with_evaluator_visibility(make_env):
    env = make_env()
    estore = evaluator_store(env)
    digest = publish_reference(estore, DEFAULT_PARAMS)
    desc = estore.descriptor(digest)
    assert desc.visibility == "evaluator"
