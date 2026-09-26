"""T11 (ledger half): admission limits, quotas, cost views, unknown costs."""

from __future__ import annotations

import pytest

from labrun.budgets import cost_views, physical_cost
from labrun.errors import BudgetExceeded


def test_run_limit_blocks_before_side_effect(make_env):
    env = make_env()
    env.controller.start_fixture_run("r", limits={"fixture_steps": 3})
    with pytest.raises(BudgetExceeded):
        env.controller.advance("r")
    # coarse calc, parse, decide ran; the refine submission was refused before submit
    assert env.scheduler.executions == 1


def test_quota_spans_runs(make_env):
    env = make_env()
    quota = {"id": "q", "caps": {"fixture_steps": 8}}
    env.controller.run_fixture("a", quota=quota)  # uses 6
    env.controller.start_fixture_run("b", quota=quota)
    with pytest.raises(BudgetExceeded):
        env.controller.advance("b")
    assert env.scheduler.executions == 3


def test_cost_views_unforked(fixture_run):
    env, _ = fixture_run
    views = cost_views(env.store, "demo")
    assert views["inherited_prefix"]["invocations"] == 0
    assert views["marginal"] == views["full_path"]
    assert views["marginal"]["units"] == {"decision_calls": 2, "fixture_steps": 6}
    assert views["marginal"]["cost_usd"] == "0.80"
    assert views["marginal"]["simulated"] is True
    assert physical_cost(env.store, ["demo"]) == views["marginal"]


def test_unknown_cost_is_not_zero(make_env):
    env = make_env()
    env.controller.start_fixture_run("r")
    params = env.controller.state("r").visible["params"]
    env.controller.run_job("r", "sample:1", "fixture-sample", {"params": params}, {})
    views = cost_views(env.store, "r")
    assert views["marginal"]["cost_usd"] is None
    assert views["marginal"]["unknown_cost_invocations"] == 1
    assert views["marginal"]["known_cost_usd"] == "0"
