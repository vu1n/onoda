"""Protected evaluation derivations (BUILD_SPEC §5, ACCEPTANCE_TESTS T13/T16).

A grader here is a synthetic-fixture derivation over immutable run outputs.
It is NOT a chemistry verdict: pass/fail/unresolved reflect agreement with a
synthetic reference computed independently from the fixture's task params,
never a claim about scientific correctness.

`publish_reference` derives the protected reference by re-deriving the
fixture's energy ordering directly from `params` (system/points/seed), not by
importing `onoda.fixture`'s agent-visible calculation or parser, and stores
it with `visibility="evaluator"` in the evaluator's own store. An agent store
(default `allowed_visibility=("agent",)`) refuses such artifacts outright.

`evaluate_run` reads a run's finalized output from the agent store (read
only), reads the reference from the evaluator store, applies a registered
grader, and writes a brand new `Evaluation` record to the evaluator store.
It never mutates or overwrites an earlier evaluation, and never writes to the
agent store. Two runs are only ever compared through `compare_evaluations`,
which refuses to mix evaluator, evaluator_version, or reference_version.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from onoda.canonical import digest_of
from onoda.errors import CapabilityError
from onoda.records import Evaluation, Usage
from onoda.store import Store

GraderResult = tuple[dict[str, str], list[str], dict[str, str]]
GraderFn = Callable[[dict[str, Any], dict[str, Any]], GraderResult]


def _reference_min_point(params: dict[str, Any]) -> str:
    """Independent re-derivation of the fixture's minimum-energy point.

    Synthetic only: mirrors the deterministic integer arithmetic the fixture
    program uses (`seed*31 + i*17 mod 97`, larger residual -> lower synthetic
    energy), computed here from `params` alone, never by importing or
    inspecting the agent-visible calculation/parser output.
    """
    points = params["points"]
    seed = params["seed"]
    if points <= 0:
        raise CapabilityError("cannot derive a reference minimum for zero scan points")
    best_i, best_k = 0, -1
    for i in range(points):
        k = (seed * 31 + i * 17) % 97
        if k > best_k:
            best_i, best_k = i, k
    return str(best_i)


def publish_reference(
    evaluator_store: Store,
    params: dict[str, Any],
    *,
    reference_version: str = "fixture-reference@1",
) -> str:
    """Compute and store the protected synthetic reference. Returns its digest.

    Written with `visibility="evaluator"`; an agent-visibility store refuses
    this call (`VisibilityError`), so the reference never lands where an
    agent could read it.
    """
    payload = {
        "synthetic": True,
        "reference_version": reference_version,
        "params_digest": digest_of(params),
        "min_point": _reference_min_point(params),
    }
    desc = evaluator_store.put_json(
        payload, visibility="evaluator", labels=["synthetic", "evaluator-reference"]
    )
    return desc.digest


def _final_output_digest(agent_store: Store, run_id: str) -> str:
    """The `final` artifact digest from the run's latest finalized checkpoint."""
    for event in reversed(agent_store.events(run_id)):
        if event.type != "checkpoint":
            continue
        cp = agent_store.checkpoint(event.payload_refs["checkpoint"].split("/", 1)[1])
        if cp.boundary == "finalized":
            return cp.visible["final"]
    raise CapabilityError(f"run {run_id} has no finalized checkpoint to evaluate")


def _grade_state_and_execution(obs: dict[str, Any], reference: dict[str, Any]) -> GraderResult:
    assignments: dict[str, str] = {}
    unresolved: list[str] = []
    metrics: dict[str, str] = {}

    if "min_point" not in obs or not obs.get("energies"):
        assignments["state_identification"] = "unresolved"
        unresolved.append("state_identification")
    else:
        match = obs["min_point"] == reference["min_point"]
        assignments["state_identification"] = "pass" if match else "fail"
        metrics["state_identification_correct"] = "1" if match else "0"

    converged = obs.get("converged")
    if converged is None:
        assignments["execution_status"] = "unresolved"
        unresolved.append("execution_status")
    else:
        assignments["execution_status"] = "pass" if converged else "fail"

    return assignments, unresolved, metrics


def _grade_v1(obs: dict[str, Any], reference: dict[str, Any]) -> GraderResult:
    """fixture-grader@1: state identification (min point vs. reference) + execution status."""
    return _grade_state_and_execution(obs, reference)


def _grade_v2(obs: dict[str, Any], reference: dict[str, Any]) -> GraderResult:
    """fixture-grader@2: v1's checks, plus a warning-free final round check."""
    assignments, unresolved, metrics = _grade_state_and_execution(obs, reference)
    warnings = obs.get("warnings")
    if warnings is None:
        assignments["warning_free_round"] = "unresolved"
        unresolved.append("warning_free_round")
    else:
        metrics["warning_count"] = str(len(warnings))
        assignments["warning_free_round"] = "pass" if len(warnings) == 0 else "fail"
    return assignments, unresolved, metrics


GRADERS: dict[str, GraderFn] = {"fixture-grader@1": _grade_v1, "fixture-grader@2": _grade_v2}


def _next_evaluation_id(evaluator_store: Store, run_id: str, evaluator: str, version: str) -> str:
    prefix = f"{run_id}.{evaluator}-v{version}."
    used = {
        int(rid[len(prefix) :])
        for rid in evaluator_store.list_record_ids("evaluation")
        if rid.startswith(prefix) and rid[len(prefix) :].isdigit()
    }
    n = (max(used) if used else 0) + 1
    return f"{prefix}{n:03d}"


def evaluate_run(
    agent_store: Store,
    evaluator_store: Store,
    run_id: str,
    *,
    grader: str = "fixture-grader@1",
    reference: str,
) -> Evaluation:
    """Grade one run's immutable output and record a new, distinct Evaluation.

    Reads the run's finalized output from `agent_store` (read only) and the
    reference from `evaluator_store` by its digest; writes only to
    `evaluator_store`. Never overwrites or mutates an earlier Evaluation
    record: re-running the same grader over the same run creates another one.
    """
    if grader not in GRADERS:
        raise CapabilityError(f"unknown grader {grader!r}")
    evaluator_name, _, evaluator_version = grader.partition("@")

    final_digest = _final_output_digest(agent_store, run_id)
    final = agent_store.get_json(final_digest)
    obs = final["final_observation"]
    ref = evaluator_store.get_json(reference)

    assignments, unresolved, metrics = GRADERS[grader](obs, ref)

    evaluation = Evaluation.new(
        evaluation_id=_next_evaluation_id(
            evaluator_store, run_id, evaluator_name, evaluator_version
        ),
        run_id=run_id,
        evaluator=evaluator_name,
        evaluator_version=evaluator_version,
        output_refs={"final": final_digest},
        reference_version=ref["reference_version"],
        assignments=assignments,
        unresolved=unresolved,
        metrics=metrics,
        usage=Usage(units={}, measured=False, simulated=True, cost_usd="0.00"),
        created_at=evaluator_store.clock.now(),
    )
    evaluator_store.put_record(evaluation)
    return evaluation


def compare_evaluations(evals: list[Evaluation]) -> dict[str, Any]:
    """Per-run metrics side by side. Refuses to mix evaluator/version/reference.

    Every evaluation compared must share the same `evaluator`,
    `evaluator_version`, and `reference_version`; otherwise a `CapabilityError`
    is raised rather than silently blending incompatible scores.
    """
    if not evals:
        raise CapabilityError("compare_evaluations requires at least one evaluation")
    evaluators = {e.evaluator for e in evals}
    versions = {e.evaluator_version for e in evals}
    references = {e.reference_version for e in evals}
    if len(evaluators) > 1 or len(versions) > 1 or len(references) > 1:
        raise CapabilityError(
            "cannot compare evaluations across differing evaluator/evaluator_version/"
            f"reference_version: evaluators={sorted(evaluators)} "
            f"versions={sorted(versions)} references={sorted(references)}"
        )
    return {
        "evaluator": next(iter(evaluators)),
        "evaluator_version": next(iter(versions)),
        "reference_version": next(iter(references)),
        "runs": {
            e.run_id: {
                "evaluation_id": e.evaluation_id,
                "assignments": dict(e.assignments),
                "unresolved": list(e.unresolved),
                "metrics": dict(e.metrics),
            }
            for e in evals
        },
    }
