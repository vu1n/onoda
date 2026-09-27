"""The shipped gate files (A49) load as the contracts the CLI reads."""

from pathlib import Path

import yaml

from onoda.ablation.contract import CAPABILITY_METRICS, Tolerances
from onoda.optimize.contract import GateSpec

GATES = Path(__file__).resolve().parents[1] / "gates"


def test_ablation_tolerances_declare_every_capability_metric_at_zero():
    tol = Tolerances.model_validate(yaml.safe_load((GATES / "tolerances.yaml").read_text()))
    assert tol.capability == {m: "0" for m in CAPABILITY_METRICS}
    assert tol.justification.startswith("A49")


def test_gate_spec_matches_the_ablation_tolerances():
    spec = GateSpec.model_validate(yaml.safe_load((GATES / "gate-spec.yaml").read_text()))
    ablation = Tolerances.model_validate(yaml.safe_load((GATES / "tolerances.yaml").read_text()))
    assert spec.tolerances == ablation
    assert spec.efficiency_gate == ("context_bytes.total", "acquisitions")
