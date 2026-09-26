"""Completely synthetic, deterministic fixture workflow. NOT chemistry.

Programs emit text logs shaped loosely like a scan: per-point "energies"
computed by integer arithmetic from a seed, a convergence line, and warnings.
A second output stands in for a wavefunction so tests can exercise
artifact-bound mappings (a mapping is only valid for the wavefunction digest
that produced it). Every output is headed with an explicit synthetic label.

Parsers are pure, versioned transforms registered in `PARSERS`; policies are
deterministic mock decision rules registered in `POLICIES`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from labrun.canonical import digest_of, sha256_digest
from labrun.decisions import DecisionRequest, MockPolicyProvider
from labrun.errors import IntegrityError

SYNTHETIC_HEADER = "# SYNTHETIC FIXTURE OUTPUT - NOT A SOLVER RESULT"
DEFAULT_PARAMS: dict[str, Any] = {"system": "fixture-A", "points": 5, "seed": 7}
QUESTION_SET = "fixture-questions@1"
QUESTION = "Is the synthetic scan ready to finalize?"
CANDIDATES = ("accept", "refine", "escalate")
TOOL_SCHEMA = {"name": "choose_action", "parameters": {"action": list(CANDIDATES)}}


def _milli_to_str(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 1000}.{value % 1000:03d}"


def _params(inputs: dict[str, bytes]) -> dict[str, Any]:
    return json.loads(inputs["params"])


def calc_program(inputs: dict[str, bytes], config: dict[str, Any], job_id: str) -> dict[str, bytes]:
    """Deterministic synthetic 'calculation'. Coarse level emits warnings."""
    p = _params(inputs)
    level = config.get("level", "coarse")
    scale = 10 if level == "coarse" else 1
    lines = [SYNTHETIC_HEADER, f"LEVEL {level}", f"SYSTEM {p['system']}"]
    for i in range(p["points"]):
        milli = -76000 - ((p["seed"] * 31 + i * 17) % 97) * scale
        lines.append(f"POINT {i} ENERGY {_milli_to_str(milli)}")
    if level == "coarse":
        lines.append("WARNING near-degenerate root at point 1")
        lines.append("WARNING near-degenerate root at point 3")
    lines.append("CONVERGED yes")
    wavefunction = f"SYNTHETIC-WAVEFUNCTION level={level} seed={p['seed']}\n".encode()
    return {"log": ("\n".join(lines) + "\n").encode(), "wavefunction": wavefunction}


def sample_program(
    inputs: dict[str, bytes], config: dict[str, Any], job_id: str
) -> dict[str, bytes]:
    """Nondeterministic mock: identical specs give different outputs per execution."""
    n = int(job_id.rsplit("-", 1)[1])
    return {"sample": f"{SYNTHETIC_HEADER}\nSAMPLE {(n * 7919) % 1000}\n".encode()}


PROGRAMS = {"fixture-calc": calc_program, "fixture-sample": sample_program}


def _parse(inputs: dict[str, bytes], *, with_spread: bool) -> dict[str, Any]:
    text = inputs["log"].decode()
    energies: dict[str, str] = {}
    warnings: list[str] = []
    converged: bool | None = None
    level = None
    synthetic = text.startswith(SYNTHETIC_HEADER)
    for line in text.splitlines():
        parts = line.split()
        if line.startswith("POINT ") and len(parts) == 4 and parts[2] == "ENERGY":
            energies[parts[1]] = parts[3]
        elif line.startswith("WARNING "):
            warnings.append(line[len("WARNING ") :])
        elif line.startswith("CONVERGED "):
            converged = parts[1] == "yes"
        elif line.startswith("LEVEL "):
            level = parts[1]
    missing = [
        name
        for name, absent in (("converged", converged is None), ("energies", not energies))
        if absent
    ]
    observation: dict[str, Any] = {
        "synthetic": synthetic,
        "level": level,
        "energies": energies,
        "units": "synthetic-hartree",
        "converged": converged,
        "warnings": warnings,
        "missing": missing,
        "source": {"log": sha256_digest(inputs["log"])},
    }
    if energies:
        order = sorted(energies, key=lambda k: Decimal(energies[k]))
        observation["min_point"] = order[0]
        if with_spread:
            spread = Decimal(energies[order[-1]]) - Decimal(energies[order[0]])
            observation["energy_spread"] = str(spread)
        mapping = {
            "wavefunction": sha256_digest(inputs["wavefunction"]),
            "state_map": {k: f"S{rank}" for rank, k in enumerate(order)},
        }
    else:
        mapping = {"wavefunction": sha256_digest(inputs["wavefunction"]), "state_map": {}}
    return {"observation": observation, "mapping": mapping}


PARSERS = {
    "fixture-parser@1": lambda inputs: _parse(inputs, with_spread=False),
    "fixture-parser@2": lambda inputs: _parse(inputs, with_spread=True),
}


def check_mapping(mapping: dict[str, Any], wavefunction_digest: str) -> None:
    """A state mapping is only valid for the exact wavefunction that produced it."""
    if mapping["wavefunction"] != wavefunction_digest:
        raise IntegrityError(
            f"mapping built for {mapping['wavefunction']} applied to {wavefunction_digest}"
        )


def decision_request(evidence: dict[str, str], policy: str) -> DecisionRequest:
    name, _, version = policy.rpartition("-")
    return DecisionRequest(
        question_set=QUESTION_SET,
        question=QUESTION,
        evidence=evidence,
        candidates=CANDIDATES,
        tool_schema_digest=digest_of(TOOL_SCHEMA),
        policy={"name": name, "version": version},
    )


def _policy_v1(request: DecisionRequest, evidence: dict[str, Any]):
    obs = evidence["observation"]
    if obs["missing"]:
        return (None, "insufficient_evidence")
    return "refine" if obs["warnings"] else "accept"


def _policy_v2(request: DecisionRequest, evidence: dict[str, Any]):
    obs = evidence["observation"]
    if obs["converged"] is None:
        return (None, "insufficient_evidence")
    return "accept" if obs["converged"] else "refine"


POLICIES = {
    "mock-policy-v1": MockPolicyProvider("mock-policy-v1", _policy_v1),
    "mock-policy-v2": MockPolicyProvider("mock-policy-v2", _policy_v2),
}
