"""Live reflection for the optimizer: one OpenRouter chat completion per call (A51).

`LiveReflection` is GEPA's `LanguageModel` shape (`str -> str`). Each call
sends the reflection prompt as one user message to OpenRouter's
OpenAI-compatible endpoint with `max_tokens` set and usage accounting on,
and returns the reply text. After every call that got a response, successful
or not, `last_receipt` holds what the optimizer records: the request body
(no credential), the raw response bytes, the model OpenRouter actually
served, token usage and the billed cost as decimal text. The loop journals it
as `optimizer.reflection-call` and counts its cost against the search's
spend cap.

It fails closed: a response without a cost figure, a cost above the per-call
cap, an HTTP error, an empty reply, or a truncated reply (`finish_reason`
"length") raises `OnodaError` after the receipt is set, so the spend is still
recorded. Every setting is explicit (A35): nothing is inferred from a present
API key. Only the provider "openrouter" is implemented.

The transport is injectable (`post`), so tests never reach the network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from onoda.adapters.pi.contract import DECIMAL_RE
from onoda.errors import CapabilityError, OnodaError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REFLECTION_PROVIDERS = ("openrouter",)
RECEIPT_SCHEMA = "onoda.reflection-receipt/1"

# (url, body, headers, timeout_s) -> (http status, response bytes)
Post = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def _urllib_post(url: str, body: bytes, headers: dict[str, str], timeout_s: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass(frozen=True)
class ReflectionSettings:
    """Every field must be given (A35); `validate` names each one that is not."""

    provider: str | None = None
    model: str | None = None
    credential_env: str | None = None
    max_tokens: int | None = None
    max_cost_usd: str | None = None  # per call, decimal text

    def validate(self) -> None:
        problems = []
        if self.provider not in REFLECTION_PROVIDERS:
            problems.append(f"provider (one of {', '.join(REFLECTION_PROVIDERS)})")
        if not self.model:
            problems.append("model")
        if not self.credential_env:
            problems.append("credential_env")
        if not self.max_tokens or self.max_tokens <= 0:
            problems.append("max_tokens")
        if not self.max_cost_usd or not DECIMAL_RE.match(self.max_cost_usd):
            problems.append("max_cost_usd")
        if problems:
            missing = ", ".join(problems)
            raise CapabilityError(f"live reflection missing/invalid settings: {missing}")
        if self.credential_env not in os.environ:
            raise CapabilityError(
                f"live reflection credential env var {self.credential_env!r} is not set"
            )


def _decimal_text(value: Any) -> str | None:
    """A JSON number (parsed as Decimal) or numeric string as plain decimal text."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0:
        return None
    return format(d, "f")


@dataclass
class LiveReflection:
    settings: ReflectionSettings
    post: Post = _urllib_post
    timeout_s: float = 300
    last_receipt: dict[str, Any] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.settings.validate()

    def __call__(self, prompt: str) -> str:
        s = self.settings
        self.last_receipt = None
        request = {
            "model": s.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": s.max_tokens,
            "usage": {"include": True},
        }
        body = json.dumps(request, sort_keys=True).encode()
        assert s.credential_env is not None  # validate() checked it
        headers = {
            "Authorization": f"Bearer {os.environ[s.credential_env]}",
            "Content-Type": "application/json",
        }
        try:
            status, raw = self.post(OPENROUTER_URL, body, headers, self.timeout_s)
        except OSError as exc:  # network failure: nothing was billed that we can see
            raise OnodaError(f"reflection request failed: {type(exc).__name__}") from None
        try:
            data = json.loads(raw, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        text = message.get("content") if isinstance(message, dict) else None
        cost = _decimal_text(usage.get("cost")) if isinstance(usage, dict) else None
        self.last_receipt = {
            "schema": RECEIPT_SCHEMA,
            "provider": s.provider,
            "model_requested": s.model,
            "model_served": data.get("model") if isinstance(data, dict) else None,
            "generation_id": data.get("id") if isinstance(data, dict) else None,
            "http_status": status,
            "request": request,
            "response": raw.decode(errors="replace"),
            "tokens": {
                k: usage.get(k)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(usage, dict) and isinstance(usage.get(k), int)
            },
            "cost_usd": cost,
            "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
        }
        if status != 200:
            raise OnodaError(f"reflection request returned HTTP {status}")
        if cost is None:
            raise OnodaError("reflection response carries no cost figure (fail closed, A51)")
        if Decimal(cost) > Decimal(str(s.max_cost_usd)):
            raise OnodaError(f"reflection call cost {cost} exceeds its cap {s.max_cost_usd}")
        if self.last_receipt["finish_reason"] == "length":
            raise OnodaError("reflection reply was cut off at max_tokens")
        if not isinstance(text, str) or not text.strip():
            raise OnodaError("reflection reply is empty")
        return text
