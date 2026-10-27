"""System One decision models: typed questions in, a probability per option out (A67).

Jev (TypeSafe, reached through OpenRouter's Decisions API) and Clef
(Cloudflare, Workers AI; Apache-2.0 open weights, Jev-compatible API) take a
`state` and `questions` (`choice`, `score`, `noul`) and answer every option
of every question with a probability, in one call. Both speak the same
request body, so one client covers both; only the endpoint and credentials
differ.

Onoda uses them as research levers, never as judges: a decision model may say
which ladder rule it would apply to a job, but the deterministic parser,
evaluator and gates stay the only graders.

Nothing here runs unless the caller enables live decisions; credentials come
from the environment at call time and are never recorded. A `ScriptedSystemOne`
answers without a model for tests and is labelled synthetic. Responses are
parsed with `Decimal` so probabilities never pass through floats.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from onoda.errors import CapabilityError, OnodaError

# provider -> (default model, endpoint template, credential env vars)
PROVIDERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "jev": (
        "typesafe/jev-1.13",
        "https://openrouter.ai/api/alpha/decisions",
        ("OPENROUTER_API_KEY",),
    ),
    "clef": (
        "clef-flash",
        "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}",
        ("CLOUDFLARE_AI_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    ),
}
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SystemOneAnswer:
    """One call: the exact bytes exchanged and the answers read from them."""

    provider: str
    model: str
    request: bytes  # the JSON body sent, credentials never included
    response: bytes  # the body received, as received
    answers: dict[str, dict[str, Any]]  # question id -> answer, numbers as Decimal
    cost_usd: str | None  # as the provider reported it; None when it did not
    synthetic: bool = False


def request_body(model: str, state: Any, questions: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {"model": model, "state": state, "questions": dict(questions)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _answers(response: bytes) -> tuple[dict[str, dict[str, Any]], str | None]:
    try:
        data = json.loads(response, parse_float=Decimal)
    except ValueError as exc:
        raise OnodaError("decision response is not JSON") from exc
    # Workers AI wraps a model's body as {"result": ..., "success": true}.
    if isinstance(data, dict) and "result" in data and "answers" not in data:
        if data.get("success") is not True:
            raise OnodaError(f"decision call failed: {data.get('errors')}")
        data = data["result"]
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise OnodaError("decision response has no answers")
    cost = (data.get("usage") or {}).get("cost")
    return answers, None if cost is None else format(Decimal(str(cost)), "f")


# live decision calls are off unless enabled explicitly
class LiveSystemOne:
    """Calls Jev or Clef over HTTPS. Construct only with live decisions enabled."""

    def __init__(self, provider: str, *, model: str | None = None, enabled: bool = False):
        if not enabled:
            raise CapabilityError("live decisions are off; pass --enable-live-decisions")
        if provider not in PROVIDERS:
            raise OnodaError(f"unknown decision provider {provider!r}; one of {sorted(PROVIDERS)}")
        default_model, self._endpoint, self._env = PROVIDERS[provider]
        missing = [v for v in self._env if not os.environ.get(v)]
        if missing:
            raise CapabilityError(f"{provider} needs {', '.join(missing)} in the environment")
        self.provider = provider
        self.model = model or default_model

    def ask(self, state: Any, questions: Mapping[str, Any]) -> SystemOneAnswer:
        body = request_body(self.model, state, questions)
        if self.provider == "jev":
            url, token = self._endpoint, os.environ["OPENROUTER_API_KEY"]
        else:
            url = self._endpoint.format(
                account=os.environ["CLOUDFLARE_ACCOUNT_ID"], model=self.model
            )
            token = os.environ["CLOUDFLARE_AI_TOKEN"]
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                response = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", errors="replace")
            raise OnodaError(f"{self.provider} answered HTTP {exc.code}: {detail}") from exc
        answers, cost = _answers(response)
        return SystemOneAnswer(self.provider, self.model, body, response, answers, cost)


class ScriptedSystemOne:
    """Answers from a function instead of a model; every answer is labelled synthetic."""

    def __init__(self, answer: Callable[[Any, Mapping[str, Any]], dict[str, Any]]):
        self.provider = "scripted"
        self.model = "scripted"
        self._answer = answer

    def ask(self, state: Any, questions: Mapping[str, Any]) -> SystemOneAnswer:
        body = request_body(self.model, state, questions)
        response = json.dumps({"answers": self._answer(state, questions)}, sort_keys=True).encode()
        answers, _ = _answers(response)
        return SystemOneAnswer(
            self.provider, self.model, body, response, answers, "0", synthetic=True
        )
