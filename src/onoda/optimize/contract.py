"""Optimizer contract: what the M4 optimizer lanes share (SOL_PI_DELTA §3, §5).

GEPA (`gepa==0.1.4`, provenance S17) searches over `HarnessCandidate`s
(`onoda.adapters.pi.contract`). Every trial is a real recorded run; its
outcome is M3b's per-run `onoda.ablation.contract.OutcomeVector` (built by
`onoda.ablation.metrics.outcome_vector`, `arm` = the run's variant), and a
minibatch or slice total is `onoda.ablation.gates.pool` -> `PooledArm`. The
optimizer has no metric names of its own: capability and efficiency metrics
are `onoda.ablation.contract.CAPABILITY_METRICS` / `EFFICIENCY_METRICS`.

SoL-Pi's two promotion gates are M3b's `compare_arm`, fixed by a `GateSpec`:
every capability metric within its predeclared tolerance of the parent (the
false-success tolerance is always 0), and at least one declared efficiency
metric strictly better. A candidate is promoted only on an `eligible`
verdict; `unresolved` is never promotion.

The optimizer sees only the acceptance slice of a `SplitAssignment`; the
final slice is never evaluated during a search. Every candidate and decision
is kept as a `CandidateRecord`. GEPA's own `gepa_state.bin` in its run dir is
operational scratch (A46): never imported, exported, or authoritative.

Metric values are decimal text; nothing here is a float.
"""

from __future__ import annotations

import random
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from onoda.ablation.contract import (
    CAPABILITY_METRICS,
    EFFICIENCY_METRICS,
    FALSE_SUCCESS,
    Tolerances,
)

GATE_SPEC_SCHEMA = "onoda.gate-spec/2"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class GateSpec(_Frozen):
    """SoL-Pi's promotion gates with operator-set tolerances (A18, A47).

    `tolerances` must declare every capability metric except
    `false_success_claims` (always 0, via `tolerance_for`): an undeclared one
    would leave every comparison `unresolved`, so the spec refuses to exist
    and the loop cannot run on a missing threshold. `efficiency_gate` names
    the efficiency metrics that may count as an improvement, declared before
    the search (SOL_PI_DELTA §5).
    """

    schema_: Literal["onoda.gate-spec/2"] = Field(GATE_SPEC_SCHEMA, alias="schema")
    tolerances: Tolerances
    efficiency_gate: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _declared(self) -> GateSpec:
        required = [m for m in CAPABILITY_METRICS if m != FALSE_SUCCESS]
        unset = [m for m in required if m not in self.tolerances.capability]
        if unset:
            raise ValueError(f"gate tolerance unresolved for {', '.join(unset)} (A47)")
        unknown = [m for m in self.efficiency_gate if m not in EFFICIENCY_METRICS]
        if unknown:
            raise ValueError(f"unknown efficiency metric {', '.join(map(repr, unknown))}")
        return self


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
    `outcomes`: store digests of the ablation OutcomeVector JSON per evaluated case.
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
