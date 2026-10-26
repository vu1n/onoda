"""Hill-climb a decision model's question wording against labelled examples (A70).

Reusable for any System One task (`onoda.systemone`): the inputs are a dataset
of examples, each a `state` plus the expected answer per question, and a
questions template (the typed `questions` body a decision model receives). The
tunable components are the `instructions` text of each question, named
`<question>.instructions`; everything else in the template (types, `criteria`)
is fixed, so a choice question's options can come from another source of truth,
such as the ladder file for `onoda vte shadow`, without two copies drifting.

The search is GEPA (`gepa==0.1.4`, the project's one optimizer): its reflection
model reads a candidate's misses and proposes new wording; a proposal is kept
when it scores higher on a minibatch, and the reported best is the candidate
with the highest mean score on the held-out examples.

Scores. Per labelled question, an example scores the probability the model gave
the expected answer: `noul` (yes/no) scores `p` for "yes" and `1 - p` for "no";
`choice` scores the expected option's probability. An example's score is the
mean over its labelled questions, computed as `Decimal` and turned into a float
only at the GEPA boundary. A question the model did not answer scores 0.

Labels are the caller's ground truth (for VTE: the grader's outcome per job).
Decision models never grade anything here; they are what is being tuned.

Money. Every decision call goes through `CachedDecisions`: answers are cached
on disk by the request's sha256, so a repeated (wording, example) pair is free
and a rerun replays, and calls and reported cost are counted against caps. The
reflection model is `LiveReflection` (OpenRouter, fail-closed on cost). The
search stops before a step that could cross a cap.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Protocol

from gepa.core.adapter import EvaluationBatch

from onoda.errors import OnodaError
from onoda.systemone import SystemOneAnswer, _answers, request_body

DATASET_SCHEMA = "onoda.decision-example/1"
RESULT_SCHEMA = "onoda.decision-tune/1"
SUFFIX = ".instructions"
STATE_CHARS_FOR_REFLECTION = 4000
NETWORK_ATTEMPTS = 3  # enough of the state to see why a miss happened
_Q6 = Decimal("0.000001")


@dataclass(frozen=True)
class Example:
    id: str
    state: Any
    labels: dict[str, str]  # question id -> "yes" | "no" (noul) or an option id (choice)


def load_dataset(path: str | Path) -> list[Example]:
    out: list[Example] = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema") != DATASET_SCHEMA:
            raise OnodaError(f"{path}:{n}: not a {DATASET_SCHEMA} line")
        out.append(Example(row["id"], row["state"], dict(row["labels"])))
    ids = [e.id for e in out]
    if len(set(ids)) != len(ids):
        raise OnodaError(f"{path}: example ids repeat")
    return out


def dump_dataset(examples: Sequence[Example]) -> str:
    return "".join(
        json.dumps(
            {"schema": DATASET_SCHEMA, "id": e.id, "state": e.state, "labels": e.labels},
            sort_keys=True,
        )
        + "\n"
        for e in examples
    )


def seed_candidate(questions: Mapping[str, Any]) -> dict[str, str]:
    """The tunable components of a questions template: each question's instructions."""
    return {f"{q}{SUFFIX}": spec["instructions"] for q, spec in sorted(questions.items())}


# only question instructions are tunable
def apply_candidate(questions: Mapping[str, Any], candidate: Mapping[str, str]) -> dict[str, Any]:
    """The template with the candidate's instructions; any other key is refused."""
    out = copy.deepcopy(dict(questions))
    for name, text in candidate.items():
        q = name.removesuffix(SUFFIX)
        if not name.endswith(SUFFIX) or q not in out:
            raise OnodaError(f"not a tunable component: {name!r}")
        out[q]["instructions"] = text
    return out


def check_labels(questions: Mapping[str, Any], examples: Sequence[Example]) -> None:
    for e in examples:
        for q, want in e.labels.items():
            spec = questions.get(q)
            if spec is None:
                raise OnodaError(f"example {e.id}: no question {q!r} in the template")
            allowed = ("yes", "no") if spec["type"] == "noul" else tuple(spec.get("criteria", ()))
            if spec["type"] not in ("noul", "choice") or want not in allowed:
                raise OnodaError(f"example {e.id}: label {q}={want!r} is not one of {allowed}")


def example_score(answers: Mapping[str, Any], labels: Mapping[str, str]) -> Decimal:
    """Mean probability of the expected answer over the labelled questions (module doc)."""
    if not labels:
        raise OnodaError("an example needs at least one label")
    total = Decimal(0)
    for q, want in labels.items():
        a = answers.get(q) or {}
        if "noul" in a:
            p = Decimal(str(a["noul"]))
            total += p if want == "yes" else 1 - p
        else:
            total += Decimal(str((a.get("probabilities") or {}).get(want, 0)))
    return (total / len(labels)).quantize(_Q6, ROUND_HALF_EVEN)


def split(examples: Sequence[Example], val_fraction: Decimal, seed: int):
    """Deterministic train/validation split by a hash of (seed, id); both sides non-empty."""
    if not Decimal(0) < val_fraction < Decimal(1) or len(examples) < 2:
        raise OnodaError("need at least 2 examples and 0 < val_fraction < 1")
    ranked = sorted(examples, key=lambda e: hashlib.sha256(f"{seed}:{e.id}".encode()).digest())
    n_val = min(len(ranked) - 1, max(1, math.ceil(len(ranked) * val_fraction)))
    return ranked[n_val:], ranked[:n_val]


class BudgetExhausted(OnodaError):
    pass


@dataclass
class Spend:
    """Calls and reported dollars, against caps; `None` cost means the provider gave none."""

    max_decision_calls: int
    max_cost_usd: Decimal
    decision_calls: int = 0
    reflection_calls: int = 0
    cost_usd: Decimal = Decimal(0)
    cost_unknown_calls: int = 0

    def charge(self, cost: str | None) -> None:
        if cost is None:
            self.cost_unknown_calls += 1
        else:
            self.cost_usd += Decimal(cost)

    def as_json(self) -> dict[str, Any]:
        return {
            "decision_calls": self.decision_calls,
            "reflection_calls": self.reflection_calls,
            "cost_usd": format(self.cost_usd, "f"),
            "calls_without_cost": self.cost_unknown_calls,
        }


class DecisionModel(Protocol):
    """What `CachedDecisions` wraps: `LiveSystemOne` or `ScriptedSystemOne`."""

    provider: str
    model: str

    def ask(self, state: Any, questions: Mapping[str, Any]) -> SystemOneAnswer: ...


@dataclass
class CachedDecisions:
    """Decision calls cached on disk by request sha256 and charged to `spend` (module doc)."""

    model: DecisionModel
    cache_dir: Path
    spend: Spend
    hits: int = 0
    synthetic: bool = False

    def ask(self, state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        body = request_body(self.model.model, state, questions)
        path = self.cache_dir / f"{hashlib.sha256(body).hexdigest()}.json"
        if path.exists():
            self.hits += 1
            return _answers(json.loads(path.read_text())["response"].encode())[0]
        if self.spend.decision_calls >= self.spend.max_decision_calls:
            raise BudgetExhausted(f"decision call cap {self.spend.max_decision_calls} reached")
        if self.spend.cost_usd >= self.spend.max_cost_usd:
            raise BudgetExhausted(f"spend cap {self.spend.max_cost_usd} USD reached")
        for attempt in range(NETWORK_ATTEMPTS):
            try:
                answer = self.model.ask(state, questions)
                break
            except OSError:  # a dropped connection; a decision call is safe to repeat
                if attempt == NETWORK_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
        self.spend.decision_calls += 1
        self.spend.charge(answer.cost_usd)
        self.synthetic |= answer.synthetic
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "provider": answer.provider,
                    "model": answer.model,
                    "request": answer.request.decode(),
                    "response": answer.response.decode(),
                    "cost_usd": answer.cost_usd,
                    "synthetic": answer.synthetic,
                },
                sort_keys=True,
            )
        )
        return answer.answers


def _summary(answers: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for q, a in sorted(answers.items()):
        if "noul" in a:
            out[q] = {"yes": _num(a["noul"])}
        else:
            probs = a.get("probabilities") or {}
            top = sorted(probs.items(), key=lambda kv: -Decimal(str(kv[1])))[:4]
            out[q] = {k: _num(v) for k, v in top}
    return out


def _num(v: Any) -> str:
    return format(Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_EVEN), "f")


@dataclass
class DecisionAdapter:
    """GEPA's adapter: evaluate wording on examples; show reflection the misses."""

    questions: Mapping[str, Any]
    decisions: CachedDecisions
    state_chars: int = STATE_CHARS_FOR_REFLECTION
    propose_new_texts: None = None  # GEPA's own proposer writes the new wording

    def evaluate(
        self, batch: list[Example], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvaluationBatch:
        qs = apply_candidate(self.questions, candidate)
        outputs, scores, traces = [], [], []
        for e in batch:
            answers = self.decisions.ask(e.state, qs)
            score = example_score(answers, e.labels)
            outputs.append({"id": e.id, "answers": _summary(answers), "score": str(score)})
            scores.append(float(score))
            traces.append({"example": e, "answers": answers, "score": score})
        return EvaluationBatch(outputs, scores, traces if capture_traces else None)

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch,
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for name in components_to_update:
            q = name.removesuffix(SUFFIX)
            rows = []
            for t in eval_batch.trajectories or []:
                e: Example = t["example"]
                if q not in e.labels:
                    continue
                state = json.dumps(e.state, sort_keys=True)[: self.state_chars]
                got = _summary(t["answers"]).get(q, "no answer")
                p = example_score({q: t["answers"].get(q) or {}}, {q: e.labels[q]})
                feedback = (
                    f"Expected answer to {q!r}: {e.labels[q]}. The model put {p} probability "
                    "on it (1 is best)."
                )
                rows.append(
                    (
                        p,
                        {
                            "Inputs": {"state": state},
                            "Generated Outputs": f"{q}: {got}",
                            "Feedback": feedback,
                        },
                    )
                )
            out[name] = [r for _, r in sorted(rows, key=lambda pr: pr[0])]  # worst misses first
        return out


class _FileLogger:
    """GEPA's progress lines go to a file, not the terminal."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path

    def log(self, message: str) -> None:
        with self._path.open("a") as f:
            f.write(message + "\n")


@dataclass
class TuneResult:
    seed: dict[str, str]
    best: dict[str, str]
    scores: dict[str, str | None]  # "seed"/"best" -> mean validation score; None: not scored
    candidates: list[dict[str, Any]]
    spend: dict[str, Any]
    cache_hits: int
    stop_reason: str
    synthetic: bool
    train_ids: list[str] = field(default_factory=list)
    validation_ids: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {"schema": RESULT_SCHEMA, **self.__dict__}


def _mean(
    adapter: DecisionAdapter, cand: dict[str, str], examples: Sequence[Example]
) -> str | None:
    """Mean score; GEPA scored every kept candidate on validation, so this reads the cache.

    None when that pass never completed (the budget ran out first).
    """
    try:
        batch = adapter.evaluate(list(examples), cand)
    except BudgetExhausted:
        return None
    total = sum((Decimal(o["score"]) for o in batch.outputs), Decimal(0))
    return format((total / len(examples)).quantize(_Q6, ROUND_HALF_EVEN), "f")


def tune(
    *,
    questions: Mapping[str, Any],
    examples: Sequence[Example],
    model: DecisionModel,
    reflect: Callable[[str], str],
    spend: Spend,
    cache_dir: Path,
    max_reflections: int,
    log_path: Path | None = None,
    val_fraction: Decimal = Decimal("0.3"),
    minibatch_size: int = 4,
    seed: int = 0,
    reflection_cost: Callable[[], str | None] = lambda: None,
) -> TuneResult:
    """Search wording for `questions` on `examples`; see the module docstring.

    `reflect` is GEPA's reflection model (prompt in, text out); after each call
    `reflection_cost()` returns what that call cost, charged to `spend`.
    """
    import gepa

    check_labels(questions, examples)
    train, val = split(examples, val_fraction, seed)
    decisions = CachedDecisions(model, cache_dir, spend)
    adapter = DecisionAdapter(questions, decisions)
    # A question no example labels has nothing to learn from; GEPA would spend reflections on it.
    labelled = {q for e in examples for q in e.labels}
    start = {
        k: v for k, v in seed_candidate(questions).items() if k.removesuffix(SUFFIX) in labelled
    }
    stop_reason = "reflection budget spent"

    def reflect_and_charge(prompt: str) -> str:
        if spend.reflection_calls >= max_reflections:
            raise BudgetExhausted("reflection budget spent")
        spend.reflection_calls += 1
        try:
            return reflect(prompt)
        finally:
            spend.charge(reflection_cost())

    # Before each iteration: a minibatch twice (parent, child) plus a full validation pass.
    per_iteration = 2 * min(minibatch_size, len(train)) + len(val)

    iterations = 0

    def stopper(_state: Any) -> bool:
        nonlocal stop_reason, iterations
        iterations += 1
        # GEPA logs a failed proposal and moves on; this bounds a run that keeps failing.
        if iterations > 2 * max_reflections + 2:
            stop_reason = f"{iterations - 1} iterations without spending the reflection budget"
            return True
        if spend.reflection_calls >= max_reflections:
            return True
        if spend.decision_calls + per_iteration > spend.max_decision_calls:
            stop_reason = "decision call cap cannot cover another iteration"
            return True
        if spend.cost_usd >= spend.max_cost_usd:
            stop_reason = "spend cap reached"
            return True
        return False

    result = None
    try:
        result = gepa.optimize(
            seed_candidate=start,
            trainset=list(train),
            valset=list(val),
            adapter=adapter,
            reflection_lm=reflect_and_charge,
            reflection_minibatch_size=min(minibatch_size, len(train)),
            candidate_selection_strategy="current_best",
            skip_perfect_score=False,
            stop_callbacks=[stopper],
            track_best_outputs=False,
            cache_evaluation=False,
            use_merge=False,
            seed=seed,
            raise_on_exception=True,
            logger=_FileLogger(log_path or cache_dir.parent / "gepa_log.txt"),
        )
    except BudgetExhausted as exc:
        stop_reason = str(exc)
    if result is not None:
        ranked = sorted(
            zip(result.val_aggregate_scores, range(len(result.candidates)), strict=True),
            key=lambda sc: (-sc[0], sc[1]),  # ties go to the earlier (simpler) candidate
        )
        best = dict(result.candidates[ranked[0][1]])
        explored = [
            {"candidate": dict(c), "parents": result.parents[i]}
            for i, c in enumerate(result.candidates)
        ]
    else:
        best, explored = start, [{"candidate": start, "parents": [None]}]
    for c in explored:
        c["validation"] = _mean(adapter, c["candidate"], val)
    scores = {"seed": explored[0]["validation"], "best": _mean(adapter, best, val)}
    best = {**seed_candidate(questions), **best}  # unlabelled questions keep their seed wording
    return TuneResult(
        seed=seed_candidate(questions),
        best=best,
        scores=scores,
        candidates=explored,
        spend=spend.as_json(),
        cache_hits=decisions.hits,
        stop_reason=stop_reason,
        synthetic=decisions.synthetic,
        train_ids=[e.id for e in train],
        validation_ids=[e.id for e in val],
    )
