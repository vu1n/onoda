"""M3b ablation contract: the surface the metrics, gates, and runner lanes share.

SOL_PI_DELTA §3/§5 asks for a SoL-Pi *add-one* ablation on Pi: the baseline
arm, each mechanism alone, and the full stack, on the same cases, tools,
information, and evaluator. This module fixes:

- the default arms (`DEFAULT_ARMS`: S1 baseline, S2a-d, S3) and which arms
  are single-mechanism (`SINGLE_ARMS`) versus the full stack (`FULL_ARM`);
- the declared ablation (`AblationSpec`), read from YAML by the runner;
- the outcome vector of one run (`OutcomeVector`) and the metric names in it,
  each with the direction that counts as better;
- the predeclared tolerances (`Tolerances`, read through `tolerance_for`) and
  the per-arm gate result (`ArmComparison`) of SoL-Pi's two-gate rule.

Every number is a decimal string (A21). A metric with no declared tolerance
makes its capability check `unresolved`, never `pass` (A18, A39). Scripted
runs are synthetic (A32): a report over them exercises the harness and is
never a verdict about a mechanism (A40).

Nothing here launches Pi, reads a store, or writes anything.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, field_validator, model_validator

from onoda.adapters.pi.contract import VARIANTS
from onoda.records import Frozen as _Frozen

SPEC_SCHEMA = "onoda.ablation-spec/1"
REPORT_SCHEMA = "onoda.ablation-report/1"
TOLERANCES_SCHEMA = "onoda.ablation-tolerances/1"

# ---------------------------------------------------------------------- arms

BASELINE_ARM = "S1"
SINGLE_ARMS = ("S2a", "S2b", "S2c", "S2d")  # one SoL-Pi mechanism each (A31)
FULL_ARM = "S3"
DEFAULT_ARMS = (BASELINE_ARM, *SINGLE_ARMS, FULL_ARM)

# Split values an ablation may read. SOL_PI_DELTA §5: search and gating never
# read the untouched final slice; only development/acceptance cases qualify.
ALLOWED_SPLITS = ("development", "acceptance")

# ------------------------------------------------------------------- metrics

Direction = Literal["lower", "higher"]

# Capability metrics: summed over an arm's cases from each run's claim
# `Evaluation` (`onoda.adapters.pi.task.evaluate_claims`). Names are the
# evaluation's own metric keys.
CAPABILITY_METRICS: dict[str, Direction] = {
    "false_success_claims": "lower",
    "count.supported": "higher",
    "count.contradicted": "lower",
    "count.unsupported": "lower",
    "count.missing": "lower",
    "count.unresolved": "lower",
}
FALSE_SUCCESS = "false_success_claims"  # tolerance fixed at zero increase (SOL_PI_DELTA §5)

# Efficiency metrics, all measured from the ingested run in the agent store
# and summed over an arm's cases. `context_bytes.*` are byte lengths of the
# provider payloads Pi actually built (measured, a proxy for token traffic, not
# tokens). `tokens.*` and `cost_usd` come from provider responses; for scripted
# runs they are the script's synthetic figures (A32) and the report says so.
EFFICIENCY_METRICS: dict[str, Direction] = {
    "context_bytes.total": "lower",
    "context_bytes.agent": "lower",
    "context_bytes.reducer": "lower",
    "context_bytes.summary": "lower",
    "requests.agent": "lower",
    "requests.reducer": "lower",
    "requests.summary": "lower",
    "tokens.total": "lower",
    "cost_usd": "lower",
    "acquisitions": "lower",  # orca_run acquisitions: the solver-submission stand-in
}

# Activation counts per mechanism (SOL_PI_DELTA §3: log activation per run):
# the number of the run's own events of this type. `actionFusion` counts
# distinct `fusion_id`s instead, since one fused call is several steps.
ACTIVATION_METRICS: dict[str, str] = {
    "observationPack": "context.observation-archived",
    "observationPack.recall": "context.page",  # obs_recall calls
    "evidencePreservingReducer": "context.reduced",
    "onlineContextCompact": "context.compaction",
    "actionFusion": "context.fused-step",
}

# A15 probe: over each `context.reduced` result, the deterministic parser's
# evidence lines found in the raw text and how many are absent from the
# visible text. Receipts are never graded; this only measures loss.
REDUCER_PROBE_METRICS = ("reducer.evidence_lines", "reducer.evidence_lost")


# ASCII digits only: Decimal() also accepts " 1_0 ", non-ASCII digits, and NaN.
_DECIMAL = re.compile(r"[+-]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?")


def _decimal_text(v: str) -> str:
    if not _DECIMAL.fullmatch(v):
        raise ValueError(f"not decimal text: {v!r}")
    return v


DecimalText = Annotated[str, AfterValidator(_decimal_text)]  # A21: never a float


class OutcomeVector(_Frozen):
    """What one ingested, evaluated run contributes to its arm."""

    run_id: str
    arm: str
    case_id: str
    model: str  # the agent model id the spool recorded (A16: activation per model)
    synthetic: bool  # scripted provider (A32)
    status: str | None  # `spool.end` status; None if the spool never ended
    capability: dict[str, DecimalText]  # CAPABILITY_METRICS keys
    efficiency: dict[str, DecimalText | None]  # EFFICIENCY_METRICS keys; None = not reported
    activation: dict[str, DecimalText]  # ACTIVATION_METRICS keys, integer text
    reducer_probe: dict[str, DecimalText]  # REDUCER_PROBE_METRICS keys, integer text
    mechanism_config: str | None  # digest of the sol-pi.json in force, or None (S0/S1)
    evaluation_id: str


class Tolerances(_Frozen):
    """Predeclared worst-case change a candidate arm may show against the baseline.

    `capability[m]` is how much metric `m` may get *worse* than the baseline
    (in the metric's own units, summed over the arm's cases) and still pass.
    A metric left out is `unresolved`, never assumed. `false_success_claims`
    must be "0" when given, and is treated as "0" when omitted, because SOL_PI_DELTA §5
    fixes it. `justification` cites the ASSUMPTIONS row that chose the values
    (A18); an empty justification is refused.
    """

    schema_: Literal["onoda.ablation-tolerances/1"] = Field(TOLERANCES_SCHEMA, alias="schema")
    justification: str = Field(min_length=1)
    capability: dict[str, DecimalText] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @field_validator("capability")
    @classmethod
    def _known(cls, v: dict[str, str]) -> dict[str, str]:
        for name, tol in v.items():
            if name not in CAPABILITY_METRICS:
                raise ValueError(f"unknown capability metric {name!r}")
            if Decimal(tol) < 0:
                raise ValueError(f"tolerance for {name} is negative: {tol}")
        if FALSE_SUCCESS in v and Decimal(v[FALSE_SUCCESS]) != 0:
            raise ValueError("false_success_claims tolerance is fixed at 0 (SOL_PI_DELTA §5)")
        return v


def tolerance_for(metric: str, tolerances: Tolerances | None) -> Decimal | None:
    """The declared tolerance for `metric`, or None if undeclared (or no file was given).

    `false_success_claims` is always zero, with or without a file (SOL_PI_DELTA §5).
    """
    if metric == FALSE_SUCCESS:
        return Decimal(0)
    tol = tolerances.capability.get(metric) if tolerances is not None else None
    return None if tol is None else Decimal(tol)


class AblationCase(_Frozen):
    case_id: str
    # Path to an onoda.pi-script/1 JSON, relative to the spec file; used for every arm.
    # A scripted ablation needs one per case; a live ablation refuses any.
    script: str | None = None


class AblationSpec(_Frozen):
    """The declared ablation: arms, cases, and the gates, fixed before any run."""

    schema_: Literal["onoda.ablation-spec/1"] = Field(SPEC_SCHEMA, alias="schema")
    ablation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    arms: tuple[str, ...] = DEFAULT_ARMS
    baseline: str = BASELINE_ARM
    cases: tuple[AblationCase, ...] = Field(min_length=1)
    ladder: str | None = None  # path, relative to the spec; required when any arm needs one
    synthetic_ladder: bool = False
    tolerances: str | None = None  # path to a Tolerances YAML/JSON; None = every check unresolved
    # Efficiency metrics the efficiency gate may count as an improvement. SOL_PI_DELTA §5
    # asks for them to be declared before the comparison.
    efficiency_gate: tuple[str, ...] = ("context_bytes.total", "acquisitions")
    max_requests: int = Field(16, ge=1)  # scripted runs; a live run uses its own cap (A35)
    # Runs of every arm on every case. Live runs vary, so repeats pool several.
    repeats: int = Field(1, ge=1, le=20)
    # A55: every arm also loads the RLM extension with the seed candidate (S1r), its
    # mechanisms set through the candidate's `sol_pi.config`, so the add-one question
    # is asked on the RLM harness. Arm names keep their mechanism meaning.
    rlm: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _arms(self) -> AblationSpec:
        for arm in self.arms:
            if arm not in VARIANTS:
                raise ValueError(f"unknown arm {arm!r}")
            if VARIANTS[arm].rlm:
                # An RLM ablation names the mechanism arms and sets `rlm` (A55).
                raise ValueError(f"arm {arm} is an RLM arm; set `rlm: true` to run arms on S1r")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("arms repeat")
        if self.rlm and any(not VARIANTS[a].ladder for a in self.arms):
            raise ValueError("an RLM ablation runs every arm on S1r, which needs the ladder")
        if self.baseline not in self.arms:
            raise ValueError(f"baseline {self.baseline!r} is not one of the arms")
        ids = [c.case_id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("cases repeat")
        for m in self.efficiency_gate:
            if m not in EFFICIENCY_METRICS:
                raise ValueError(f"unknown efficiency metric {m!r}")
        if not self.efficiency_gate:
            raise ValueError("efficiency_gate declares no metric")
        if any(VARIANTS[a].ladder for a in self.arms) and self.ladder is None:
            raise ValueError("an arm needs the advisory ladder, but the spec names none (A33)")
        return self


CheckResult = Literal["pass", "fail", "unresolved"]
Verdict = Literal["eligible", "rejected", "unresolved"]


class MetricCheck(_Frozen):
    metric: str
    baseline: DecimalText | None
    candidate: DecimalText | None
    delta: DecimalText | None  # candidate - baseline; None when either side is unreported
    tolerance: DecimalText | None  # capability only
    result: CheckResult  # efficiency: pass = strictly better, fail = not better


class ArmComparison(_Frozen):
    """One candidate arm against the baseline under the two gates (SOL_PI_DELTA §5).

    capability: every capability metric within tolerance; any `fail` fails the
    gate, else any `unresolved` leaves it unresolved. efficiency: at least one
    declared efficiency metric strictly improves (`pass`); `unresolved` if none
    improves and some declared metric was unreported. `verdict` is `eligible`
    only when both gates pass, `rejected` when either fails.
    """

    arm: str
    baseline: str
    capability: tuple[MetricCheck, ...]
    efficiency: tuple[MetricCheck, ...]
    capability_gate: CheckResult
    efficiency_gate: CheckResult
    verdict: Verdict
