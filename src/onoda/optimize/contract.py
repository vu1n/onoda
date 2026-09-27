"""Optimizer contract: what the M4 optimizer lanes share (SOL_PI_DELTA §3, §5).

GEPA (`gepa==0.1.4`, provenance S17) searches over `HarnessCandidate`s
(`onoda.adapters.pi.contract`). Every trial is a real recorded run; its
evaluator verdicts become an `OutcomeVector`. SoL-Pi's two promotion gates,
fixed by a `GateSpec`, decide acceptance: every capability metric within its
predeclared tolerance of the parent, no increase at all in false-success
claims, and at least one efficiency metric strictly better.

The optimizer sees only the acceptance slice of a `SplitAssignment`; the
final slice is never evaluated during a search. Every candidate and decision
is kept as a `CandidateRecord`. GEPA's own `gepa_state.bin` in its run dir is
operational scratch (A46): never imported, exported, or authoritative.

Metric values are decimal text; nothing here is a float.
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from onoda.adapters.pi.contract import DECIMAL_RE

# Higher is better. `supported_claims`: claims the deterministic parser
# supports; `settled_runs`: runs whose spool ended `settled`.
CAPABILITY_METRICS = ("supported_claims", "settled_runs")
# Lower is better. Parent and child runs both count (children charge the parent).
EFFICIENCY_METRICS = ("requests", "total_tokens", "cost_usd")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


def _decimals(v: dict[str, str], names: tuple[str, ...], what: str) -> dict[str, str]:
    if set(v) != set(names):
        raise ValueError(f"{what} metrics must be exactly {', '.join(names)}")
    for k, x in v.items():
        if not DECIMAL_RE.match(x):
            raise ValueError(f"{what} metric {k} is not decimal text: {x!r}")
    return v


class OutcomeVector(_Frozen):
    """One candidate on one case (or, via `total`, on a slice)."""

    schema_: Literal["onoda.outcome-vector/1"] = Field("onoda.outcome-vector/1", alias="schema")
    candidate: str  # HarnessCandidate.digest()
    cases: tuple[str, ...]  # one case id, or the slice a total covers
    runs: tuple[str, ...]  # run ids in the agent store
    capability: dict[str, str]
    efficiency: dict[str, str]
    false_success_claims: int = Field(ge=0)  # contradicted + unsupported (evaluator metric)

    @field_validator("capability")
    @classmethod
    def _cap(cls, v: dict[str, str]) -> dict[str, str]:
        return _decimals(v, CAPABILITY_METRICS, "capability")

    @field_validator("efficiency")
    @classmethod
    def _eff(cls, v: dict[str, str]) -> dict[str, str]:
        return _decimals(v, EFFICIENCY_METRICS, "efficiency")

    @classmethod
    def total(cls, vectors: list[OutcomeVector]) -> OutcomeVector:
        """Sum per-case vectors of one candidate into a slice total."""
        if not vectors or len({v.candidate for v in vectors}) != 1:
            raise ValueError("total() needs vectors of exactly one candidate")

        def add(field: str, names: tuple[str, ...]) -> dict[str, str]:
            return {n: str(sum(Decimal(getattr(v, field)[n]) for v in vectors)) for n in names}

        return cls(
            candidate=vectors[0].candidate,
            cases=tuple(c for v in vectors for c in v.cases),
            runs=tuple(r for v in vectors for r in v.runs),
            capability=add("capability", CAPABILITY_METRICS),
            efficiency=add("efficiency", EFFICIENCY_METRICS),
            false_success_claims=sum(v.false_success_claims for v in vectors),
        )


class GateSpec(_Frozen):
    """SoL-Pi's promotion gates with operator-set tolerances (A18, A47).

    Every capability metric needs a tolerance (decimal text, >= 0): a
    candidate may fall at most that far below its parent. `None` means the
    operator has not chosen one, and the spec refuses to exist, so the loop
    cannot run on a guessed threshold. `false_success_tolerance` is fixed at
    0 by SOL_PI_DELTA §5.
    """

    schema_: Literal["onoda.gate-spec/1"] = Field("onoda.gate-spec/1", alias="schema")
    capability_tolerance: dict[str, str | None]
    false_success_tolerance: int = 0
    justification: str = Field(min_length=1)  # why these tolerances (A18: predeclared, justified)

    @field_validator("capability_tolerance")
    @classmethod
    def _tolerances(cls, v: dict[str, str | None]) -> dict[str, str | None]:
        if set(v) != set(CAPABILITY_METRICS):
            raise ValueError(f"a tolerance is required for each of {', '.join(CAPABILITY_METRICS)}")
        unset = sorted(k for k, x in v.items() if x is None)
        if unset:
            raise ValueError(f"gate tolerance unresolved for {', '.join(unset)} (A47)")
        for k, x in v.items():
            assert x is not None
            if not DECIMAL_RE.match(x) or Decimal(x) < 0:
                raise ValueError(f"tolerance for {k} is not a non-negative decimal: {x!r}")
        return v

    @field_validator("false_success_tolerance")
    @classmethod
    def _zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError("false_success_tolerance is fixed at 0 (SOL_PI_DELTA §5)")
        return v


class SplitAssignment(_Frozen):
    """Which cases the optimizer may use (acceptance) and which it never sees (final).

    Stored once as an immutable artifact; a search cites its digest. While
    any case is pending human review the split is `exploratory` (A48).
    """

    schema_: Literal["onoda.split/1"] = Field("onoda.split/1", alias="schema")
    seed: int
    acceptance: tuple[str, ...] = Field(min_length=1)
    final: tuple[str, ...] = Field(min_length=1)
    exploratory: bool

    @model_validator(mode="after")
    def _disjoint(self) -> SplitAssignment:
        overlap = set(self.acceptance) & set(self.final)
        if overlap:
            raise ValueError(f"cases in both slices: {', '.join(sorted(overlap))}")
        if len(set(self.acceptance)) != len(self.acceptance) or len(set(self.final)) != len(
            self.final
        ):
            raise ValueError("a slice lists a case twice")
        return self

    @classmethod
    def derive(
        cls, case_ids: list[str], *, seed: int, final_count: int, exploratory: bool
    ) -> SplitAssignment:
        """Deterministic: shuffle the sorted ids with `random.Random(seed)`; the
        last `final_count` form the final slice."""
        ids = sorted(set(case_ids))
        if not 0 < final_count < len(ids):
            raise ValueError(f"final_count must leave both slices non-empty ({len(ids)} cases)")
        random.Random(seed).shuffle(ids)
        return cls(
            seed=seed,
            acceptance=tuple(sorted(ids[:-final_count])),
            final=tuple(sorted(ids[-final_count:])),
            exploratory=exploratory,
        )


Decision = Literal["seed", "accepted", "rejected", "invalid"]


class CandidateRecord(_Frozen):
    """One proposed candidate and what happened to it. Written once, never updated.

    `invalid`: failed `HarnessCandidate` validation and was never run.
    `outcomes`: store digests of the OutcomeVector JSON per evaluated case.
    `feedback`: digest of the reflective-dataset packet the proposal came from.
    """

    schema_: Literal["onoda.optimizer-candidate/1"] = Field(
        "onoda.optimizer-candidate/1", alias="schema"
    )
    search_id: str
    iteration: int = Field(ge=0)
    candidate: str | None  # HarnessCandidate.digest(); None when invalid
    components: dict[str, str]
    parents: tuple[str, ...] = ()  # candidate digests
    origin: Literal["seed", "reflection", "merge"]
    decision: Decision
    reasons: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    feedback: str | None = None
