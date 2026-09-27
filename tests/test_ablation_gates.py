"""ABL-003: two-gate comparison, non-dominated set, S3 interaction."""

from __future__ import annotations

from onoda.ablation.contract import CAPABILITY_METRICS, FALSE_SUCCESS, Tolerances
from onoda.ablation.gates import PooledArm, compare_arm, interaction, non_dominated, pool

EFFICIENCY_GATE = ("context_bytes.total", "acquisitions")


def make_vector(
    *,
    arm: str,
    case_id: str,
    run_id: str | None = None,
    capability: dict[str, str] | None = None,
    efficiency: dict[str, str | None] | None = None,
    activation: dict[str, str] | None = None,
    reducer_probe: dict[str, str] | None = None,
    status: str = "settled",
):
    from onoda.ablation.contract import OutcomeVector

    return OutcomeVector(
        run_id=run_id or f"{arm}-{case_id}",
        arm=arm,
        case_id=case_id,
        model="mock-model",
        synthetic=True,
        status=status,
        capability=capability
        or {
            "false_success_claims": "0",
            "count.supported": "3",
            "count.contradicted": "0",
            "count.unsupported": "0",
            "count.missing": "0",
            "count.unresolved": "0",
        },
        efficiency=efficiency
        or {
            "context_bytes.total": "100",
            "context_bytes.agent": "80",
            "context_bytes.reducer": "10",
            "context_bytes.summary": "10",
            "requests.agent": "2",
            "requests.reducer": "1",
            "requests.summary": "1",
            "tokens.total": "500",
            "cost_usd": "0.01",
            "acquisitions": "1",
        },
        activation=activation or {},
        reducer_probe=reducer_probe
        or {"reducer.evidence_lines": "0", "reducer.evidence_lost": "0"},
        mechanism_config=None,
        evaluation_id=f"ev-{arm}-{case_id}",
    )


def pool_arm(arm: str, cases: list[str], **overrides) -> PooledArm:
    return pool([make_vector(arm=arm, case_id=c, **overrides) for c in cases])


TOL = Tolerances(justification="test fixture", capability={"count.contradicted": "0"})
# Every capability metric declared (0 worsening allowed): use where the test
# expects a capability_gate of "pass" rather than "unresolved".
FULL_TOL = Tolerances(
    justification="test fixture - full",
    capability={m: "0" for m in CAPABILITY_METRICS if m != FALSE_SUCCESS},
)


# ------------------------------------------------------------------------ pool


def test_pool_sums_metrics_across_runs():
    v1 = make_vector(
        arm="S1",
        case_id="c1",
        capability={
            "false_success_claims": "0",
            "count.supported": "2",
            "count.contradicted": "0",
            "count.unsupported": "0",
            "count.missing": "0",
            "count.unresolved": "0",
        },
    )
    v2 = make_vector(
        arm="S1",
        case_id="c2",
        capability={
            "false_success_claims": "1",
            "count.supported": "1",
            "count.contradicted": "0",
            "count.unsupported": "0",
            "count.missing": "0",
            "count.unresolved": "0",
        },
    )
    pooled = pool([v1, v2])
    assert pooled.arm == "S1"
    assert pooled.runs == 2
    assert pooled.case_ids == ("c1", "c2")
    assert pooled.capability["count.supported"] == "3"
    assert pooled.capability["false_success_claims"] == "1"


def test_pool_normalizes_single_value_decimal_text():
    pooled = pool_arm("S1", ["c1"])
    assert pooled.capability["count.supported"] == "3"
    assert pooled.efficiency["context_bytes.total"] == "100"


def test_pool_metric_none_when_any_run_leaves_it_unreported():
    v1 = make_vector(arm="S1", case_id="c1")
    v2 = make_vector(
        arm="S1",
        case_id="c2",
        efficiency={
            "context_bytes.total": None,
            "context_bytes.agent": "80",
            "context_bytes.reducer": "10",
            "context_bytes.summary": "10",
            "requests.agent": "2",
            "requests.reducer": "1",
            "requests.summary": "1",
            "tokens.total": "500",
            "cost_usd": "0.01",
            "acquisitions": "1",
        },
    )
    pooled = pool([v1, v2])
    assert pooled.efficiency["context_bytes.total"] is None
    # metrics both runs reported are still summed
    assert pooled.efficiency["acquisitions"] == "2"


def test_pool_rejects_mixed_arms():
    v1 = make_vector(arm="S1", case_id="c1")
    v2 = make_vector(arm="S2a", case_id="c1")
    try:
        pool([v1, v2])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_pool_rejects_empty():
    try:
        pool([])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ------------------------------------------------------------------ compare_arm


def test_compare_arm_eligible_when_both_gates_pass():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm(
        "S2a",
        ["c1", "c2"],
        efficiency={
            "context_bytes.total": "40",  # improves vs pooled baseline 200
            "context_bytes.agent": "80",
            "context_bytes.reducer": "10",
            "context_bytes.summary": "10",
            "requests.agent": "2",
            "requests.reducer": "1",
            "requests.summary": "1",
            "tokens.total": "500",
            "cost_usd": "0.01",
            "acquisitions": "1",
        },
    )
    cmp = compare_arm(baseline, candidate, FULL_TOL, EFFICIENCY_GATE)
    assert cmp.capability_gate == "pass"
    assert cmp.efficiency_gate == "pass"
    assert cmp.verdict == "eligible"
    total_check = next(c for c in cmp.efficiency if c.metric == "context_bytes.total")
    assert total_check.result == "pass"
    assert total_check.delta == "-120"


def test_compare_arm_capability_fail_beyond_tolerance():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm(
        "S2a",
        ["c1", "c2"],
        capability={
            "false_success_claims": "0",
            "count.supported": "3",
            "count.contradicted": "2",  # worsens by 2 per case > tolerance of 0
            "count.unsupported": "0",
            "count.missing": "0",
            "count.unresolved": "0",
        },
    )
    cmp = compare_arm(baseline, candidate, TOL, EFFICIENCY_GATE)
    assert cmp.capability_gate == "fail"
    assert cmp.verdict == "rejected"
    check = next(c for c in cmp.capability if c.metric == "count.contradicted")
    assert check.result == "fail"
    assert check.delta == "4"  # pooled over 2 cases
    assert check.tolerance == "0"


def test_compare_arm_capability_unresolved_when_tolerance_missing():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm(
        "S2a",
        ["c1", "c2"],
        capability={
            "false_success_claims": "0",
            "count.supported": "1",  # worsens vs baseline (higher is better)
            "count.contradicted": "0",
            "count.unsupported": "0",
            "count.missing": "0",
            "count.unresolved": "0",
        },
        # Tie on efficiency, plus one metric unreported: efficiency_gate is
        # "unresolved", not "fail", so the verdict below isolates the
        # capability gate's own unresolved-tolerance behavior.
        efficiency={
            "context_bytes.total": None,
            "context_bytes.agent": "80",
            "context_bytes.reducer": "10",
            "context_bytes.summary": "10",
            "requests.agent": "2",
            "requests.reducer": "1",
            "requests.summary": "1",
            "tokens.total": "500",
            "cost_usd": "0.01",
            "acquisitions": "1",
        },
    )
    # TOL declares no tolerance for count.supported.
    cmp = compare_arm(baseline, candidate, TOL, EFFICIENCY_GATE)
    check = next(c for c in cmp.capability if c.metric == "count.supported")
    assert check.result == "unresolved"
    assert cmp.capability_gate == "unresolved"
    assert cmp.efficiency_gate == "unresolved"
    assert cmp.verdict == "unresolved"


def test_compare_arm_no_tolerances_file_unresolved_except_false_success():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm("S2a", ["c1", "c2"])
    cmp = compare_arm(baseline, candidate, None, EFFICIENCY_GATE)
    for check in cmp.capability:
        if check.metric == "false_success_claims":
            assert check.tolerance == "0"
        else:
            assert check.result == "unresolved"
            assert check.tolerance is None
    assert cmp.capability_gate == "unresolved"


def test_compare_arm_false_success_increase_fails_even_without_tolerance_file():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm(
        "S2a",
        ["c1", "c2"],
        capability={
            "false_success_claims": "1",  # baseline pools to 0; any increase fails
            "count.supported": "3",
            "count.contradicted": "0",
            "count.unsupported": "0",
            "count.missing": "0",
            "count.unresolved": "0",
        },
    )
    cmp = compare_arm(baseline, candidate, None, EFFICIENCY_GATE)
    check = next(c for c in cmp.capability if c.metric == "false_success_claims")
    assert check.result == "fail"
    assert cmp.capability_gate == "fail"
    assert cmp.verdict == "rejected"


def test_compare_arm_ties_are_not_efficiency_improvements():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm("S2a", ["c1", "c2"])  # identical efficiency pools
    cmp = compare_arm(baseline, candidate, TOL, EFFICIENCY_GATE)
    for check in cmp.efficiency:
        assert check.result == "fail"
    assert cmp.efficiency_gate == "fail"
    assert cmp.verdict == "rejected"


def test_compare_arm_efficiency_unresolved_when_unreported_and_no_pass():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm(
        "S2a",
        ["c1", "c2"],
        efficiency={
            "context_bytes.total": None,
            "context_bytes.agent": "80",
            "context_bytes.reducer": "10",
            "context_bytes.summary": "10",
            "requests.agent": "2",
            "requests.reducer": "1",
            "requests.summary": "1",
            "tokens.total": "500",
            "cost_usd": "0.01",
            "acquisitions": "2",  # tie, not an improvement
        },
    )
    cmp = compare_arm(baseline, candidate, TOL, EFFICIENCY_GATE)
    total_check = next(c for c in cmp.efficiency if c.metric == "context_bytes.total")
    assert total_check.result == "unresolved"
    acq_check = next(c for c in cmp.efficiency if c.metric == "acquisitions")
    assert acq_check.result == "fail"
    assert cmp.efficiency_gate == "unresolved"
    assert cmp.verdict == "unresolved"


def test_compare_arm_missing_run_case_mismatch_is_unresolved():
    baseline = pool_arm("S1", ["c1", "c2"])
    candidate = pool_arm("S2a", ["c1"])  # c2 missing on the candidate side
    cmp = compare_arm(baseline, candidate, TOL, EFFICIENCY_GATE)
    assert cmp.capability_gate == "unresolved"
    assert cmp.efficiency_gate == "unresolved"
    assert cmp.verdict == "unresolved"
    for check in (*cmp.capability, *cmp.efficiency):
        assert check.result == "unresolved"
        assert check.delta is None


# --------------------------------------------------------------- non_dominated


def make_efficient_pool(arm: str, total: str, acquisitions: str) -> PooledArm:
    return pool_arm(
        arm,
        ["c1", "c2"],
        efficiency={
            "context_bytes.total": total,
            "context_bytes.agent": "80",
            "context_bytes.reducer": "10",
            "context_bytes.summary": "10",
            "requests.agent": "2",
            "requests.reducer": "1",
            "requests.summary": "1",
            "tokens.total": "500",
            "cost_usd": "0.01",
            "acquisitions": acquisitions,
        },
    )


def test_non_dominated_drops_arm_beaten_on_every_metric():
    baseline = pool_arm("S1", ["c1", "c2"])
    a = make_efficient_pool("S2a", "80", "2")  # beaten by b on both metrics
    b = make_efficient_pool("S2b", "40", "1")
    cmp_a = compare_arm(baseline, a, FULL_TOL, EFFICIENCY_GATE)
    cmp_b = compare_arm(baseline, b, FULL_TOL, EFFICIENCY_GATE)
    assert cmp_a.verdict == "eligible"
    assert cmp_b.verdict == "eligible"
    kept = non_dominated([cmp_a, cmp_b], {"S2a": a, "S2b": b}, EFFICIENCY_GATE)
    assert kept == ("S2b",)


def test_non_dominated_keeps_incomparable_tradeoff():
    baseline = pool_arm("S1", ["c1", "c2"])
    a = make_efficient_pool("S2a", "40", "2")  # better on total, worse on acquisitions
    b = make_efficient_pool("S2b", "80", "1")  # better on acquisitions, worse on total
    cmp_a = compare_arm(baseline, a, FULL_TOL, EFFICIENCY_GATE)
    cmp_b = compare_arm(baseline, b, FULL_TOL, EFFICIENCY_GATE)
    kept = non_dominated([cmp_a, cmp_b], {"S2a": a, "S2b": b}, EFFICIENCY_GATE)
    assert set(kept) == {"S2a", "S2b"}


def test_non_dominated_excludes_non_eligible_arms():
    baseline = pool_arm("S1", ["c1", "c2"])
    rejected_candidate = pool_arm("S2a", ["c1", "c2"])  # ties everywhere -> rejected
    cmp_rejected = compare_arm(baseline, rejected_candidate, FULL_TOL, EFFICIENCY_GATE)
    assert cmp_rejected.verdict == "rejected"
    kept = non_dominated([cmp_rejected], {"S2a": rejected_candidate}, EFFICIENCY_GATE)
    assert kept == ()


def test_non_dominated_preserves_input_order():
    baseline = pool_arm("S1", ["c1", "c2"])
    a = make_efficient_pool("S2a", "40", "2")
    b = make_efficient_pool("S2b", "80", "1")
    cmp_a = compare_arm(baseline, a, FULL_TOL, EFFICIENCY_GATE)
    cmp_b = compare_arm(baseline, b, FULL_TOL, EFFICIENCY_GATE)
    # b listed first: order of the result should follow input, not arm name.
    kept = non_dominated([cmp_b, cmp_a], {"S2a": a, "S2b": b}, EFFICIENCY_GATE)
    assert kept == ("S2b", "S2a")


# ----------------------------------------------------------------- interaction


def test_interaction_reports_full_delta_sum_and_difference():
    # baseline pools "100" over 2 cases -> 200. make_efficient_pool(arm, t, a)
    # pools t over 2 cases -> 2*t, so its delta from baseline is 2*t - 200.
    baseline = pool_arm("S1", ["c1", "c2"])
    singles = {
        "S2a": make_efficient_pool("S2a", "80", "1"),  # pooled 160, delta -40
        "S2b": make_efficient_pool("S2b", "60", "1"),  # pooled 120, delta -80
        "S2c": pool_arm("S2c", ["c1", "c2"]),  # pooled 200, delta 0
        "S2d": pool_arm("S2d", ["c1", "c2"]),  # pooled 200, delta 0
    }
    full = make_efficient_pool("S3", "30", "1")  # pooled 60, delta -140
    terms = interaction(baseline, singles, full)
    term = terms["context_bytes.total"]
    assert term.full_delta == "-140"
    assert term.sum_single_deltas == "-120"
    assert term.interaction == "-20"


def test_interaction_none_when_single_arm_absent():
    baseline = pool_arm("S1", ["c1", "c2"])
    singles = {
        "S2a": pool_arm("S2a", ["c1", "c2"]),
        "S2b": pool_arm("S2b", ["c1", "c2"]),
        "S2c": pool_arm("S2c", ["c1", "c2"]),
        # S2d missing entirely
    }
    full = pool_arm("S3", ["c1", "c2"])
    terms = interaction(baseline, singles, full)
    term = terms["context_bytes.total"]
    assert term.sum_single_deltas is None
    assert term.interaction is None
    # full_delta is still reportable independent of the missing single arm.
    assert term.full_delta == "0"


def test_interaction_none_when_full_metric_missing():
    baseline = pool_arm("S1", ["c1", "c2"])
    singles = {
        "S2a": pool_arm("S2a", ["c1", "c2"]),
        "S2b": pool_arm("S2b", ["c1", "c2"]),
        "S2c": pool_arm("S2c", ["c1", "c2"]),
        "S2d": pool_arm("S2d", ["c1", "c2"]),
    }
    full = pool_arm("S3", ["c1", "c2"], efficiency={"context_bytes.total": None})
    terms = interaction(baseline, singles, full)
    term = terms["context_bytes.total"]
    assert term.full_delta is None
    assert term.interaction is None
    # sum_single_deltas is still reportable independent of S3.
    assert term.sum_single_deltas == "0"


def test_interaction_covers_capability_metric_too():
    baseline = pool_arm("S1", ["c1", "c2"])
    singles = {
        "S2a": pool_arm("S2a", ["c1", "c2"]),
        "S2b": pool_arm("S2b", ["c1", "c2"]),
        "S2c": pool_arm("S2c", ["c1", "c2"]),
        "S2d": pool_arm("S2d", ["c1", "c2"]),
    }
    full = pool_arm("S3", ["c1", "c2"])
    terms = interaction(baseline, singles, full)
    assert "count.supported" in terms
    assert terms["count.supported"].full_delta == "0"
    assert terms["count.supported"].sum_single_deltas == "0"
    assert terms["count.supported"].interaction == "0"


def test_an_unsettled_run_leaves_the_comparison_unresolved():
    # A run cut off by its budget shows fewer bytes only because it stopped early.
    baseline = pool_arm("S1", ["c1"])
    cut = pool(
        [
            make_vector(
                arm="S2a",
                case_id="c1",
                status="budget-exhausted",
                efficiency={"context_bytes.total": "10", "acquisitions": "0"},
            )
        ]
    )
    assert cut.unsettled == ("S2a-c1",)
    comp = compare_arm(baseline, cut, FULL_TOL, EFFICIENCY_GATE)
    assert (comp.capability_gate, comp.efficiency_gate, comp.verdict) == ("unresolved",) * 3


def test_repeated_cases_must_match_in_count():
    baseline = pool_arm("S1", ["a", "a", "b"])
    candidate = pool_arm("S2a", ["a", "b"])
    assert compare_arm(baseline, candidate, FULL_TOL, EFFICIENCY_GATE).verdict == "unresolved"


def test_tolerances_must_be_plain_decimal_text():
    import pytest

    for bad in (" 1_0 ", "١", "NaN", "Infinity", "1\n"):
        with pytest.raises(ValueError):
            Tolerances(justification="t", capability={"count.missing": bad})
