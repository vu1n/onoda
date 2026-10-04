"""A51: live reflection over OpenRouter and the search-wide spend cap.

Offline: the HTTP transport is a fake, the runner is `test_optimize`'s
`SpyRunner`, and every cost is a test fixture value, not a price.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from test_optimize import ScriptedReflection, SpyRunner, journal, optimize

from onoda.clock import LogicalClock
from onoda.errors import CapabilityError, OnodaError
from onoda.optimize.adapter import BudgetExhausted, SearchLedger, SpendCap
from onoda.optimize.reflection import OPENROUTER_URL, LiveReflection, ReflectionSettings
from onoda.store import Store

KEY_ENV = "ONODA_TEST_OPENROUTER_KEY"
SECRET = "sk-test-secret-value"


def settings(**kw) -> ReflectionSettings:
    base = dict(
        provider="openrouter",
        model="vendor/open-model",
        credential_env=KEY_ENV,
        max_tokens=512,
        max_cost_usd="0.05",
    )
    base.update(kw)
    return ReflectionSettings(**base)


def response(text="```\nnew text\n```", cost=0.0012, finish="stop", **extra) -> bytes:
    usage = {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940}
    if cost is not None:
        usage["cost"] = cost
    body = {
        "id": "gen-1",
        "model": "vendor/open-model-served",
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": usage,
        **extra,
    }
    return json.dumps(body).encode()


class FakePost:
    def __init__(self, *replies: tuple[int, bytes]):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, body, headers, timeout_s):
        self.calls.append((url, json.loads(body), headers))
        return self.replies.pop(0)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, SECRET)


# ------------------------------------------------------------ reflection


def test_every_reflection_setting_is_explicit(monkeypatch):
    with pytest.raises(CapabilityError) as exc:
        ReflectionSettings().validate()
    for name in ("provider", "model", "credential_env", "max_tokens", "max_cost_usd"):
        assert name in str(exc.value)
    with pytest.raises(CapabilityError, match="provider"):
        settings(provider="anthropic").validate()
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(CapabilityError, match="not set"):
        settings().validate()


def test_a_call_returns_the_reply_and_a_float_free_receipt():
    post = FakePost((200, response()))
    reflect = LiveReflection(settings(), post=post)
    assert reflect("revise this") == "```\nnew text\n```"
    url, body, headers = post.calls[0]
    assert url == OPENROUTER_URL
    assert body["model"] == "vendor/open-model" and body["max_tokens"] == 512
    assert body["usage"] == {"include": True}
    assert headers["Authorization"] == f"Bearer {SECRET}"
    r = reflect.last_receipt
    assert r is not None
    assert r["cost_usd"] == "0.0012" and r["model_served"] == "vendor/open-model-served"
    assert r["tokens"]["total_tokens"] == 940
    # The receipt is what the store keeps: no credential and no floats.
    dumped = json.dumps(r)
    assert SECRET not in dumped
    assert not any(isinstance(v, float) for v in _walk(r))


def _walk(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)
    else:
        yield x


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((200, response(cost=None)), "no cost figure"),
        ((200, response(cost=0.09)), "exceeds its cap"),
        ((500, b'{"error": "boom"}'), "HTTP 500"),
        ((200, response(finish="length")), "cut off"),
        ((200, response(text="  ")), "empty"),
    ],
)
def test_a_refused_reply_still_leaves_its_receipt(reply, message):
    reflect = LiveReflection(settings(), post=FakePost(reply))
    with pytest.raises(OnodaError, match=message):
        reflect("revise this")
    assert reflect.last_receipt is not None
    assert reflect.last_receipt["http_status"] == reply[0]


# ------------------------------------------------------------ spend cap


def cap(total="1.00", trial="0.20", reflection="0.05") -> SpendCap:
    return SpendCap(Decimal(total), Decimal(trial), Decimal(reflection))


def test_ledger_reserves_trials_then_settles_them_from_the_journal(tmp_path):
    store = Store(tmp_path / "opt", clock=LogicalClock())
    ledger = SearchLedger(store, "s", max_trials=99, max_reflections=9, spend=cap())
    t1 = ledger.charge_trial("cand", "c1")
    assert ledger.spent_usd == Decimal("0.20")  # reserved at the per-trial cap
    ledger.event("optimizer.outcome", refs={}, payload={"trial_id": t1, "cost_usd": "0.03"})
    assert ledger.spent_usd == Decimal("0.03")  # settled at the reported cost
    ledger.charge_reflection("system_prompt")
    ledger.record_reflection_call("system_prompt", "p", "r", {"cost_usd": "0.01"})
    assert ledger.spent_usd == Decimal("0.04")
    # A resumed ledger counts the same spend from the journal.
    again = SearchLedger(store, "s", max_trials=99, max_reflections=9, spend=cap())
    assert again.spent_usd == Decimal("0.04")
    assert again.can_afford(4, 1) and not again.can_afford(5, 0)


def test_ledger_refuses_what_the_cap_cannot_cover(tmp_path):
    store = Store(tmp_path / "opt", clock=LogicalClock())
    ledger = SearchLedger(store, "s", max_trials=99, max_reflections=9, spend=cap(total="0.29"))
    ledger.charge_trial("cand", "c1")  # 0.20 reserved, never settled (a failed trial)
    with pytest.raises(BudgetExhausted, match="spend cap"):
        ledger.charge_trial("cand", "c2")
    ledger.record_reflection_call("x", "p", None, {"cost_usd": None})  # unpriced: reserve
    assert ledger.spent_usd == Decimal("0.25")
    with pytest.raises(BudgetExhausted, match="spend cap"):
        ledger.charge_reflection("x")


def test_search_stops_before_an_iteration_the_cap_cannot_cover(tmp_path):
    # Seed evaluation: 3 trials at a 0.50 fixture cost each (SpyRunner's `vec`).
    # One more iteration needs up to 2*2 + 3 trials at the 0.60 reserve.
    result = optimize(
        tmp_path, SpyRunner(), ScriptedReflection(["EFFICIENT"]), spend=cap("3.00", "0.60")
    )
    assert "spend cap" in result.stop_reason
    assert result.trials == 3 and result.reflections == 0
    assert Decimal(result.spent_usd) == Decimal("1.50")


def test_search_refuses_a_seed_evaluation_the_cap_cannot_cover(tmp_path):
    runner = SpyRunner()
    result = optimize(tmp_path, runner, ScriptedReflection([]), spend=cap("0.50", "0.60"))
    assert result.stop_reason == "spend cap cannot cover the seed evaluation"
    assert runner.calls == []


def test_the_seed_evaluation_check_counts_repeats(tmp_path):
    # 3 cases x 2 repeats at the 0.60 reserve need 3.60; one pass alone would fit in 3.00.
    runner = SpyRunner()
    result = optimize(
        tmp_path, runner, ScriptedReflection([]), spend=cap("3.00", "0.60"), repeats=2
    )
    assert result.stop_reason == "spend cap cannot cover the seed evaluation"
    assert runner.calls == []
    result = optimize(tmp_path / "t", runner, ScriptedReflection([]), max_trials=5, repeats=2)
    assert result.stop_reason == "trial budget cannot cover the seed evaluation"
    assert runner.calls == []


# ------------------------------------------------------------ recording


class ReceiptReflection(ScriptedReflection):
    """A scripted reflection that also leaves a live-style receipt."""

    def __init__(self, markers, fail_after: int | None = None):
        super().__init__(markers)
        self.fail_after = fail_after
        self.last_receipt = None

    def __call__(self, prompt: str) -> str:
        self.last_receipt = {"schema": "onoda.reflection-receipt/1", "cost_usd": "0.002"}
        if self.fail_after is not None and len(self.prompts) >= self.fail_after:
            self.prompts.append(prompt)
            raise OnodaError("reflection reply is empty")
        return super().__call__(prompt)


def test_every_reflection_call_is_recorded_with_its_receipt(tmp_path):
    optimize(tmp_path, SpyRunner(), ReceiptReflection(["EFFICIENT", "EFFICIENT"]), spend=cap("99"))
    store, calls = journal(tmp_path, "optimizer.reflection-call")
    assert calls
    for e in calls:
        assert set(e.payload_refs) == {"prompt", "reply", "receipt"}
        assert e.payload["cost_usd"] == "0.002" and e.payload["live"] is True
        assert store.get_json(e.payload_refs["receipt"])["cost_usd"] == "0.002"


def test_a_refused_reflection_is_still_recorded_and_charged(tmp_path):
    # GEPA treats a failed proposal as "no candidate" and moves on; every
    # refused call is still journalled and charged, and budgets still end it.
    result = optimize(tmp_path, SpyRunner(), ReceiptReflection([], fail_after=0), spend=cap("99"))
    assert result.accepted == ()
    store, calls = journal(tmp_path, "optimizer.reflection-call")
    assert len(calls) == result.reflections == 3
    for e in calls:
        assert "reply" not in e.payload_refs and e.payload["cost_usd"] == "0.002"
