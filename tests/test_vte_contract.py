"""VTE contract: bundle, claim, broker protocol, spool line, case and reference loading."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from onoda.adapters.pi.contract import (
    TASK_TOOLS,
    VARIANTS,
    VTE_TASK_TOOLS,
    GatewayPolicy,
    TaskExecute,
    parse_spool_line,
)
from onoda.adapters.pi.vte_contract import (
    BrokerAttach,
    BrokerReply,
    BrokerSubmit,
    ClaimedVte,
    VteClaim,
    VteTaskBundle,
)
from onoda.canonical import sha256_digest
from onoda.errors import IntegrityError
from onoda.science.vte import (
    VteTarget,
    load_vte_case,
    load_vte_references,
    normalize_character,
    normalize_irrep,
    parse_state_label,
    vte_case_manifests,
)

H = "a" * 64


def test_state_labels_from_questdb_tables():
    assert parse_state_label("1B1(,3s)") == (1, "B1", "pi,3s")
    assert parse_state_label("3A2(n,*)") == (3, "A2", "n,pi*")
    assert parse_state_label("1A’’(n,*)") == (1, "A''", "n,pi*")
    assert parse_state_label("1A’(n,3s)") == (1, "A'", "n,3s")
    # Fluorescence rows need an excited-state geometry and are not VTE targets.
    assert parse_state_label('1A"[F](,*)') is None
    assert normalize_irrep("A”") == "A''"
    assert normalize_character(" N , * ") == "n,pi*"


@pytest.mark.parametrize(
    "written,expected",
    [
        ("π,π*", "pi,pi*"),
        ("π→π*", "pi,pi*"),
        ("n → π*", "n,pi*"),
        ("n->pi*", "n,pi*"),
        ("n -> π*", "n,pi*"),
        ("n to pi*", "n,pi*"),
        ("π to 3s", "pi,3s"),
        ("Π,3s", "pi,3s"),
        ("σ,σ*", "sigma,sigma*"),
        ("δ,π*", "delta,pi*"),
        ("n,π∗", "n,pi*"),
    ],
)
def test_character_as_agents_write_it(written, expected):
    assert normalize_character(written) == expected


def test_target_irrep_must_be_normalized():
    VteTarget(spin=1, irrep="A''", count=2)
    with pytest.raises(ValidationError):
        VteTarget(spin=1, irrep="A’’", count=1)
    with pytest.raises(ValidationError):
        VteTarget(spin=2, irrep="A1", count=1)


def test_variant_tools_by_task_kind():
    assert VARIANTS["S1"].tools()[:3] == TASK_TOOLS
    assert VARIANTS["S1"].tools("vte")[:3] == VTE_TASK_TOOLS
    assert "orca_run" not in VARIANTS["S0"].tools("vte")
    assert (
        GatewayPolicy(allowed_tools=("read",), max_requests=1, max_acquisitions=0).max_executions
        == 0
    )


def _bundle(**kw):
    base = dict(
        case_id="questdb-ketene",
        instruction="Compute the target states.",
        molecule="ketene",
        point_group="C2v",
        structure_name="ketene.xyz",
        structure_sha256="sha256:" + H,
        targets=(VteTarget(spin=1, irrep="B1", count=1),),
        review_status="pending-human-review",
    )
    return VteTaskBundle(**{**base, **kw})


def test_bundle_refuses_includes_paths_and_bad_docs():
    assert _bundle().max_executions == 24
    with pytest.raises(ValidationError):
        _bundle(instruction="@/etc/passwd")
    with pytest.raises(ValidationError):
        _bundle(structure_name="../x.xyz")
    with pytest.raises(ValidationError):
        _bundle(docs=("notes.txt",))
    dumped = _bundle().model_dump(mode="json", by_alias=True)
    assert dumped["schema"] == "onoda.pi-vte-task/1"
    assert "references" not in json.dumps(dumped)


def test_claim_states_name_job_and_ev():
    state = {"spin": 1, "irrep": "B1", "character": "pi,3s", "job_id": "j0003"}
    ClaimedVte(**state, energy={"text": "6.069", "unit": "eV"})
    with pytest.raises(ValidationError):
        ClaimedVte(**state, energy={"text": "0.2230", "unit": "au"})
    with pytest.raises(ValidationError):
        ClaimedVte(**{**state, "job_id": "job-00001"}, energy={"text": "6.069", "unit": "eV"})
    claim = VteClaim(case_id="questdb-ketene")
    assert claim.model_dump(by_alias=True)["schema"] == "onoda.pi-vte-claim/1"


def test_broker_protocol_consistency():
    BrokerSubmit(
        tool_call_id="c1",
        input="! CASSCF\n",
        attach=(BrokerAttach(job_id="j0001", name="job.gbw"),),
    )
    with pytest.raises(ValidationError):
        BrokerAttach(job_id="j0001", name="../job.gbw")
    with pytest.raises(ValidationError):
        BrokerSubmit(tool_call_id="c1", input="")
    BrokerReply(status="refused", reason="max_executions")
    with pytest.raises(ValidationError):
        BrokerReply(status="refused", job_id="j0001")
    done = dict(
        status="completed", job_id="j0001", input_sha256="sha256:" + H, output_sha256="sha256:" + H
    )
    BrokerReply(**done, executed_here=True)
    BrokerReply(**done, cache_hit=True)
    with pytest.raises(ValidationError):
        BrokerReply(**done, cache_hit=True, executed_here=True)
    with pytest.raises(ValidationError):
        BrokerReply(status="completed", job_id="j0001")


def test_task_execute_spool_line():
    line = {
        "seq": 4,
        "type": "task.execute",
        "tool_call_id": "c1",
        "job_id": "j0001",
        "status": "completed",
        "input": H,
        "output": "b" * 64,
        "cache_hit": False,
        "executed_here": True,
    }
    assert isinstance(parse_spool_line(line), TaskExecute)
    with pytest.raises(ValidationError):
        parse_spool_line({**line, "cache_hit": True})


def _write_case(tmp_path, *, case_id="questdb-x", refs_case_id=None):
    d = tmp_path / "x"
    d.mkdir()
    xyz = b"3\n\nO 0 0 0\nH 0 0 1\nH 0 1 0\n"
    (d / "x.xyz").write_bytes(xyz)
    refs = {
        "schema": "onoda.vte-references/1",
        "case_id": refs_case_id or case_id,
        "method": "SC-NEVPT2/aug-cc-pVTZ",
        "source": {"repository": "https://github.com/pfloos/QUESTDB", "commit": "460482b"},
        "rows": [
            {
                "label": "1B1(n,3s)",
                "spin": 1,
                "irrep": "B1",
                "character": "n,3s",
                "energy_ev": "7.1",
            }
        ],
    }
    refs_bytes = json.dumps(refs).encode()
    (d / "references.json").write_bytes(refs_bytes)
    manifest = {
        "case_id": case_id,
        "title": "x",
        "molecule": "x",
        "point_group": "C2v",
        "method": "SA-CASSCF/SC-NEVPT2/aug-cc-pVTZ",
        "split": "test",
        "structure": {"path": "x.xyz", "sha256": sha256_digest(xyz)},
        "targets": [{"spin": 1, "irrep": "B1", "count": 1}],
        "references": {"path": "references.json", "sha256": sha256_digest(refs_bytes)},
        "provenance": {
            "origin": "questdb",
            "repository": "https://github.com/pfloos/QUESTDB",
            "commit": "460482b",
            "upstream_path": "geometries/xyz/x.xyz",
            "license": "CC-BY-SA-4.0",
            "license_file": "LICENSE",
            "retrieved": "2026-10-03",
            "executed_here": False,
        },
        "review": {"status": "pending-human-review"},
    }
    path = d / "x.vte.yaml"
    path.write_text(yaml.safe_dump(manifest))
    return path


def test_load_case_pins_and_keeps_references_separate(tmp_path):
    path = _write_case(tmp_path)
    case = load_vte_case(path)
    assert case.structure_bytes.startswith(b"3\n")
    assert not hasattr(case, "references_bytes")
    refs = load_vte_references(case)
    assert refs.rows[0].energy_ev == "7.1"
    assert vte_case_manifests(tmp_path) == {"questdb-x": path}
    (path.parent / "references.json").write_bytes(b"{}")
    with pytest.raises(IntegrityError):
        load_vte_case(path)


def test_references_for_another_case_are_refused(tmp_path):
    case = load_vte_case(_write_case(tmp_path, refs_case_id="questdb-y"))
    with pytest.raises(IntegrityError):
        load_vte_references(case)
