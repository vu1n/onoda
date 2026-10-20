#!/usr/bin/env python3
"""Reproduce a mechanical archive comparison; no scientific labels or live calls.

Run from the checkout with `uv run python scripts/evaluate_evidence_packet.py`.
The fixed manifest excludes PR52's held-out molecules. Mutations test content
integrity, not physical correctness, and are not independent scientific samples.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from onoda.adapters.pi.vte_shadow import excerpt
from onoda.canonical import sha256_digest
from onoda.science.orca_evidence import build_packet, check_packet

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("tests/fixtures/orca611-nevpt2")
# Pinned authentic phase-0 output bytes, not run/job identity or scientific truth.
SOURCES = (
    (
        "ketene_a_rhf",
        "sha256:e3d4b60dd1eb42050777ffd671367c3c1511ba0f7980b939858832e5b04a2016",
    ),
    (
        "ketene_b_valence_dz",
        "sha256:292ba1e9e6a7a599b26ab898dbdcda234f428161c9629fb8629c401347c17a9d",
    ),
    (
        "ketene_c_valence_avtz_nosym",
        "sha256:3ff0a59244e03c92cd528df1e69b26c14383b1f0fa65d6888f15a5ea092c3294",
    ),
    (
        "ketene_d_valence_avtz_sym",
        "sha256:9b2803919b21eaf615bbb86c3fa4c15beb2cebae86e4d14d01acf42e0486110c",
    ),
    (
        "ketene_f_target",
        "sha256:51a44cebb2a22cbf563f32217ccb4d4dbae48463747b6ecbc04b9a73dc07a7e1",
    ),
    (
        "ethylene_guess",
        "sha256:cf10fcedcf15f590752c38525a6543ac40534487a44acd845927f1594b1865cd",
    ),
    (
        "ethylene_nevpt2_np4",
        "sha256:d73d1e00485380d1ec488d7ac406a488a0d5d86da8ccf568c233f9a2e020d71d",
    ),
    (
        "ethylene_nevpt2_np4_default_orbstep",
        "sha256:e26be768950fb186ab77f65affca1d1a6a82e84888d17c1b64e25ea2aaade99e",
    ),
    (
        "formaldehyde_guess",
        "sha256:e10f2de53a15401cea67374fe22c01e66ac994b5cab15af774c1a5929a8ea8a8",
    ),
    (
        "formaldehyde_nevpt2_np4",
        "sha256:21a4b9b59ba174e91d743f7fff423815e55ea228872799e8aa6ad6cbbc1fc96d",
    ),
)


def _corruptions(packet: dict, raw: bytes) -> list[dict]:
    variants = [("source_bytes_changed", packet, raw + b"\n")]
    for mutation in (
        "fact_changed",
        "evidence_location_forged",
        "evidence_text_forged",
        "evidence_omitted",
        "job_binding_invented",
        "wavefunction_binding_invented",
        "presentation_coverage_forged",
    ):
        changed = copy.deepcopy(packet)
        if mutation == "fact_changed":
            changed["observation"]["termination"] = "invented"
        elif mutation == "evidence_location_forged":
            changed["evidence"][0]["byte_start"] += 1
        elif mutation == "evidence_text_forged":
            changed["evidence"][0]["text"] = "invented source line"
        elif mutation == "evidence_omitted":
            changed["evidence"].pop()
        elif mutation == "job_binding_invented":
            changed["provenance"]["job_binding"] = "verified"
        elif mutation == "wavefunction_binding_invented":
            changed["provenance"]["wavefunction_binding"] = "verified"
        else:
            changed["presentation"]["omitted_lines"] = [0]
        variants.append((mutation, changed, raw))
    return [
        {
            "mutation": name,
            "synthetic": True,
            "rejected": not check_packet(changed, data)["valid"],
            "digest_only_rejected": changed["source"]["digest"] != sha256_digest(data),
        }
        for name, changed, data in variants
    ]


def comparison() -> dict:
    samples = []
    for name, digest in SOURCES:
        path = FIXTURES / f"{name}.out"
        raw = (ROOT / path).read_bytes()
        if sha256_digest(raw) != digest:
            raise ValueError(f"archive bytes changed: {path}")
        molecule = name.split("_", 1)[0]
        packet = build_packet(raw)
        old = excerpt(raw)
        old_lines = set(old.splitlines())
        samples.append(
            {
                "molecule": molecule,
                "lineage": f"phase-0-2026-10-02:{molecule}",
                "path": path.as_posix(),
                "source": digest,
                "parser": packet["observation"]["parser"],
                "parser_evidence_lines": len(packet["evidence"]),
                "direct_parser_inventory_retained": len(packet["evidence"]),
                "legacy_excerpt_retained": sum(
                    entry["text"] in old_lines for entry in packet["evidence"]
                ),
                "legacy_excerpt_chars": len(old),
                "packet_inventory_retained": len(packet["evidence"]),
                "packet_presentation_retained": len(packet["presentation"]["retained_lines"]),
                "packet_presentation_omitted": packet["presentation"]["omitted_lines"],
                "packet_presentation_chars": len(packet["presentation"]["text"]),
                "parser_warnings": len(packet["observation"]["warnings"]),
                "legacy_warnings_retained": sum(
                    entry["text"] in old_lines for entry in packet["observation"]["warnings"]
                ),
                "genuine_packet_valid": check_packet(packet, raw)["valid"],
                "zero_budget_packet_valid": check_packet(
                    build_packet(raw, max_evidence_chars=0), raw
                )["valid"],
                "job_binding": packet["provenance"]["job_binding"],
                "wavefunction_binding": packet["provenance"]["wavefunction_binding"],
                "corruptions": _corruptions(packet, raw),
            }
        )
    totals = {
        field: sum(row[field] for row in samples)
        for field in (
            "parser_evidence_lines",
            "direct_parser_inventory_retained",
            "legacy_excerpt_retained",
            "packet_inventory_retained",
            "packet_presentation_retained",
            "parser_warnings",
            "legacy_warnings_retained",
        )
    }
    mutations = [mutation for row in samples for mutation in row["corruptions"]]
    return {
        "schema": "onoda.evidence-packet-comparison/1",
        "molecule_groups": sorted({row["molecule"] for row in samples}),
        "lineage_groups": sorted({row["lineage"] for row in samples}),
        "excluded_molecules": ["acetone", "formamide", "cyclopropenone", "methylenecyclopropene"],
        "samples": samples,
        "totals": {
            **totals,
            "authentic_outputs": len(samples),
            "genuine_packets_valid": sum(row["genuine_packet_valid"] for row in samples),
            "zero_budget_packets_valid": sum(row["zero_budget_packet_valid"] for row in samples),
            "synthetic_corruptions": len(mutations),
            "packet_rejections": sum(row["rejected"] for row in mutations),
            "digest_only_rejections": sum(row["digest_only_rejected"] for row in mutations),
        },
        "provider_calls": 0,
        "solver_calls": 0,
        "model_arms": {"direct_clef": "not run", "clairic_then_clef": "not run"},
        "model_cost_usd": None,
        "model_latency_seconds": None,
        "limitations": [
            "Lexical retention and integrity only; no scientific-ready or harmful-mismatch labels.",
            "Packet facts are the direct parser baseline; no extraction accuracy gain is claimed.",
            "An excerpt's verbatim text match cannot verify source line or job identity.",
            "Presentation is capped at 12000 characters; full packet size is not provider-bounded.",
            "Logs share molecule lineage; mutations and repeated jobs are not independent samples.",
            "No training, tuning, calibrated probability, selective-risk or model comparison.",
            "A consistent substituted packet/source pair needs an independent archive binding.",
            "Wavefunctions, orbital maps and recorded run/job bindings were not supplied.",
            "Synthetic status and source authenticity remain caller declarations.",
            "Clairic/Clef quality, costs and scientific value remain unmeasured.",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(comparison(), indent=2, sort_keys=True))
