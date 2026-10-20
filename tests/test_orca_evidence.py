"""Source fidelity tests; these do not label scientific readiness."""

from __future__ import annotations

import copy
import importlib.util
import json
import tracemalloc
from pathlib import Path

import pytest

from onoda.canonical import canonical_bytes, sha256_digest
from onoda.cli import main
from onoda.errors import OnodaError
from onoda.science.orca_evidence import build_packet, check_packet
from onoda.science.orca_parser import parse_orca_output

FIXTURES = Path(__file__).parent / "fixtures/orca611-nevpt2"
KETENE = FIXTURES / "ketene_f_target.out"


def test_packet_keeps_exact_parser_facts_and_source_byte_locations():
    raw = KETENE.read_bytes()
    packet = build_packet(raw)
    assert packet == build_packet(raw)
    assert packet["observation"] == parse_orca_output(raw).to_json()
    assert packet["source"]["digest"] == sha256_digest(raw)
    assert packet["source"]["bytes"] == len(raw)
    assert {entry["line"] for entry in packet["evidence"]} == {1733, 4264, 4465}
    for entry in packet["evidence"]:
        assert entry["artifact"] == sha256_digest(raw)
        assert raw[entry["byte_start"] : entry["byte_end"]].decode().rstrip() == entry["text"]
        assert raw.decode().splitlines()[entry["line"] - 1].rstrip() == entry["text"]
    assert packet["observation"]["mr_states"][1]["energy_ev"]["value"] == "6.069"
    assert packet["provenance"] == {
        "job_binding": "unavailable",
        "wavefunction_binding": "unavailable",
    }
    assert packet["interpretations"] == []
    assert check_packet(packet, raw)["valid"]
    assert canonical_bytes(packet)


def test_inventory_is_complete_when_presentation_has_no_room():
    raw = (FIXTURES / "ethylene_nevpt2_np4.out").read_bytes()
    full, zero = build_packet(raw), build_packet(raw, max_evidence_chars=0)
    assert zero["evidence"] == full["evidence"]
    assert zero["observation"] == full["observation"]
    assert len(zero["evidence"]) == 15
    assert zero["presentation"]["text"] == ""
    assert zero["presentation"]["retained_lines"] == []
    assert zero["presentation"]["omitted_lines"] == [entry["line"] for entry in zero["evidence"]]
    assert zero["presentation"]["truncated"] is True
    assert check_packet(zero, raw)["valid"]


@pytest.mark.parametrize("budget", [-1, 12001, True, 1.5, "100"])
def test_invalid_or_over_cap_presentation_budget_is_refused(budget):
    with pytest.raises(OnodaError, match="max_evidence_chars"):
        build_packet(b"", max_evidence_chars=budget)


def test_presentation_never_splits_a_source_line_to_meet_budget():
    raw = KETENE.read_bytes()
    packet = build_packet(raw, max_evidence_chars=80)
    text = packet["presentation"]["text"]
    assert len(text) <= 80
    lines = {entry["line"]: entry["text"] for entry in packet["evidence"]}
    assert text.splitlines() == [lines[n] for n in packet["presentation"]["retained_lines"]]
    assert set(lines) == set(packet["presentation"]["retained_lines"]) | set(
        packet["presentation"]["omitted_lines"]
    )


@pytest.mark.parametrize(
    "mutation", ["fact", "line", "text", "offset", "omit", "job", "wavefunction", "coverage"]
)
def test_synthetic_packet_corruptions_fail_integrity(mutation):
    raw = KETENE.read_bytes()
    packet = copy.deepcopy(build_packet(raw))
    if mutation == "fact":
        packet["observation"]["mr_states"][1]["energy_ev"]["value"] = "0.000"
    elif mutation in ("line", "text"):
        packet["evidence"][0][mutation] = 1 if mutation == "line" else "invented"
    elif mutation == "offset":
        packet["evidence"][0]["byte_start"] += 1
    elif mutation == "omit":
        packet["evidence"].pop()
    elif mutation in ("job", "wavefunction"):
        packet["provenance"][f"{mutation}_binding"] = "verified"
    else:
        packet["presentation"]["truncated"] = True
    result = check_packet(packet, raw)
    assert result["valid"] is False and result["errors"]


def test_output_substitution_is_rejected_even_if_new_output_converged():
    packet = build_packet(KETENE.read_bytes())
    other = (FIXTURES / "ethylene_nevpt2_np4.out").read_bytes()
    result = check_packet(packet, other)
    assert not result["valid"]
    assert "source_mismatch" in result["errors"]


@pytest.mark.parametrize("bad", [None, [], {}, {"schema": "future"}, {"schema": float("nan")}])
def test_malformed_packets_return_invalid(bad):
    assert check_packet(bad, KETENE.read_bytes())["valid"] is False


def test_decode_replacements_and_incomplete_output_are_explicit():
    raw = b"\xff\r\nWARNING: synthetic \xcf\x80 diagnostic\r\nFINAL SINGLE POINT ENERGY -1.000\r\n"
    packet = build_packet(raw, synthetic=True)
    assert packet["source"]["utf8_valid"] is False
    assert packet["observation"]["synthetic"] is True
    assert packet["observation"]["termination"] == "absent"
    assert packet["observation"]["problems"]
    for entry in packet["evidence"]:
        assert (
            raw[entry["byte_start"] : entry["byte_end"]].decode("utf-8", errors="replace").rstrip()
            == entry["text"]
        )
    assert check_packet(packet, raw)["valid"]


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r", b"\x0b", "\u2028".encode()])
def test_offsets_follow_parser_line_numbers_across_line_endings(separator):
    raw = separator.join(
        [
            b"\xff",
            "WARNING: synthetic \u03c0 diagnostic".encode(),
            b"FINAL SINGLE POINT ENERGY -1.000",
            b"",
        ]
    )
    packet = build_packet(raw, synthetic=True)
    assert [entry["line"] for entry in packet["evidence"]] == [2, 3]
    for entry in packet["evidence"]:
        assert (
            raw[entry["byte_start"] : entry["byte_end"]].decode("utf-8", errors="replace").rstrip()
            == entry["text"]
        )


def test_noncanonical_annotations_are_invalid_instead_of_crashing():
    for value in (float("nan"), "\ud800", 1 << 70):
        raw = KETENE.read_bytes()
        packet = build_packet(raw)
        packet["unknowns"].append(value)
        assert check_packet(packet, raw)["valid"] is False


@pytest.mark.parametrize("field", ["unexpected", "unknowns", "evidence"])
def test_nested_packet_fields_do_not_amplify_memory_before_rejection(field):
    packet = build_packet(b"", synthetic=True)
    nested = {}
    for _ in range(200):
        nested = {"x" * 1000: nested}
    packet[field] = nested
    # The source is empty: measuring schema rejection, not ORCA-output size.
    # Eager canonicalization retains ~20 MB of ancestor paths for this 0.2 MB tree.
    tracemalloc.start()
    try:
        assert check_packet(packet, b"")["valid"] is False
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_cli_copied_artifacts_validate_and_corrupt_packet_exits_nonzero(tmp_path, capsys):
    assert main(["vte", "evidence", str(KETENE), "--max-evidence-chars", "0"]) == 0
    packet = json.loads(capsys.readouterr().out)
    packet_path, output_path = tmp_path / "packet.json", tmp_path / "renamed.out"
    packet_path.write_text(json.dumps(packet))
    output_path.write_bytes(KETENE.read_bytes())
    args = ["vte", "evidence-check", str(packet_path), str(output_path)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    packet["evidence"].clear()
    packet_path.write_text(json.dumps(packet))
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_cli_unreadable_or_malformed_files_are_reported(tmp_path, capsys):
    missing = str(tmp_path / "missing")
    assert main(["vte", "evidence", missing]) == 2
    assert "error" in json.loads(capsys.readouterr().err)
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert main(["vte", "evidence-check", str(broken), str(KETENE)]) == 2
    assert "error" in json.loads(capsys.readouterr().err)


def comparison_module():
    path = Path(__file__).parents[1] / "scripts/evaluate_evidence_packet.py"
    spec = importlib.util.spec_from_file_location("evidence_comparison", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_comparison_is_reproducible_and_excludes_heldout_molecules():
    module = comparison_module()
    result = module.comparison()
    saved = Path(__file__).parents[1] / "experiments/evidence-packet/report.json"
    assert result == json.loads(saved.read_text())
    assert result["molecule_groups"] == ["ethylene", "formaldehyde", "ketene"]
    assert len(result["lineage_groups"]) == 3
    assert result["totals"]["parser_evidence_lines"] == 47
    assert result["totals"]["legacy_excerpt_retained"] == 24
    assert result["totals"]["packet_rejections"] == 80
    assert result["totals"]["digest_only_rejections"] == 10
    assert result["provider_calls"] == result["solver_calls"] == 0
    assert result["model_cost_usd"] is None


def test_archive_comparison_refuses_changed_source_pin(tmp_path, monkeypatch):
    module = comparison_module()
    name, _ = module.SOURCES[0]
    directory = tmp_path / module.FIXTURES
    directory.mkdir(parents=True)
    (directory / f"{name}.out").write_bytes(b"synthetic substitution")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="archive bytes changed"):
        module.comparison()
