"""Offline evidence packets over one archived ORCA output.

The full parser observation and evidence inventory are lossless relative to the
recorded parser version. Only the separate text presentation is budgeted. Neither
completeness nor integrity establishes scientific readiness, job provenance, or
the identity of an absent wavefunction. No existing agent or grader consumes this
format; it is an operator artifact and a boundary for future shadow experiments.
"""

from __future__ import annotations

from typing import Any

from onoda.canonical import canonical_bytes, sha256_digest
from onoda.errors import OnodaError
from onoda.science.orca_parser import parse_orca_output

PACKET_SCHEMA = "onoda.orca-evidence-packet/1"
MAX_EVIDENCE_CHARS = 12000


def _evidence(value: Any) -> list[dict[str, Any]]:
    """Unique evidence lines from the parser schema, including warnings/problems."""
    found: dict[int, dict[str, Any]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if {"artifact", "line", "text"} <= item.keys():
                found[item["line"]] = item
            else:
                for child in item.values():
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return [found[line] for line in sorted(found)]


def _line_spans(data: bytes) -> list[tuple[int, int]]:
    # Match str.splitlines in the parser, including Unicode separators. Surrogate
    # escape round-trips undecodable bytes without moving later byte offsets.
    lines = data.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    spans, start = [], 0
    for line in lines:
        end = start + len(line.encode("utf-8", errors="surrogateescape"))
        spans.append((start, end))
        start = end
    return spans


def _budget(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_EVIDENCE_CHARS:
        raise OnodaError(f"max_evidence_chars must be an integer in 0..{MAX_EVIDENCE_CHARS}")
    return value


# preserve the parser observation; packets never supply graded values
def build_packet(
    data: bytes, *, max_evidence_chars: int = MAX_EVIDENCE_CHARS, synthetic: bool = False
) -> dict[str, Any]:
    """Build a deterministic, float-free packet, without reading any other artifact.

    Byte spans are half-open and include original whitespace and line endings.
    `synthetic` is a caller declaration, not independently verified authenticity.
    The presentation budget applies only to `presentation.text`, not the packet's
    full observation/inventory or any future provider request.
    """
    budget = _budget(max_evidence_chars)
    if type(synthetic) is not bool:
        raise OnodaError("synthetic must be a boolean caller declaration")
    observation = parse_orca_output(data, synthetic=synthetic).to_json()
    spans = _line_spans(data)
    evidence = [
        {
            **entry,
            "byte_start": spans[entry["line"] - 1][0],
            "byte_end": spans[entry["line"] - 1][1],
        }
        for entry in _evidence(observation)
    ]
    try:
        data.decode("utf-8")
        utf8_valid = True
    except UnicodeDecodeError:
        utf8_valid = False

    text: list[str] = []
    retained, omitted, size = [], [], 0
    for entry in evidence:
        cost = len(entry["text"]) + bool(text)
        if size + cost <= budget:
            text.append(entry["text"])
            retained.append(entry["line"])
            size += cost
        else:
            omitted.append(entry["line"])

    return {
        "schema": PACKET_SCHEMA,
        "source": {
            "digest": sha256_digest(data),
            "bytes": len(data),
            "utf8_valid": utf8_valid,
        },
        "observation": observation,
        "evidence": evidence,
        "presentation": {
            "max_chars": budget,
            "text": "\n".join(text),
            "retained_lines": retained,
            "omitted_lines": omitted,
            "truncated": bool(omitted),
        },
        "provenance": {
            "job_binding": "unavailable",
            "wavefunction_binding": "unavailable",
        },
        "interpretations": [],
        "unknowns": [
            "Supplied bytes establish content identity; job/run association is unavailable.",
            "No wavefunction or orbital-map artifact was supplied; their identity is unavailable.",
            "Input echoes and MORead filenames do not establish artifact lineage or availability.",
            "Complete inventory means this parser's evidence, not all scientific diagnostics.",
            "State character, physical correctness, readiness and model quality are unassessed.",
            "Source authenticity and synthetic status are not independently verified.",
            "Source completeness is unknown; see parser termination/problems for truncation.",
        ],
    }


# an integrity check is not a scientific grade or an agent-visible decision
def check_packet(packet: Any, data: bytes) -> dict[str, Any]:
    """Check a v1 packet against supplied bytes and a fresh parse, failing closed.

    This checks content consistency only. Replacing both packet and source with a
    mutually consistent pair cannot be detected without an independent archive
    binding, which this format explicitly lacks. Caller-declared synthetic status
    and the bounded presentation budget are the only reconstruction parameters.
    """
    errors: list[str] = []
    if not isinstance(packet, dict) or packet.get("schema") != PACKET_SCHEMA:
        return {"valid": False, "errors": ["packet_schema_mismatch"]}
    try:
        budget = _budget(packet["presentation"]["max_chars"])
        synthetic = packet["observation"]["synthetic"]
        expected = build_packet(data, max_evidence_chars=budget, synthetic=synthetic)
        if packet.keys() != expected.keys():
            errors.append("packet_fields_mismatch")
        for field, value in expected.items():
            # Canonicalization builds diagnostic paths recursively. First compare
            # with the known shape so arbitrary nested input cannot amplify them.
            if field not in packet or packet[field] != value:
                errors.append(f"{field}_mismatch")
            elif canonical_bytes(packet[field]) != canonical_bytes(value):
                errors.append(f"{field}_mismatch")
    except (OnodaError, KeyError, TypeError, ValueError, RecursionError):
        return {"valid": False, "errors": ["malformed_packet"]}
    return {"valid": not errors, "errors": errors}
