"""T01/T09 through the CLI: clean-workspace round trip with the network blocked."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest
import yaml

from labrun.cli import main

TEMPLATE = Path(__file__).parents[1] / "docs/handoff/examples/fork.template.yaml"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield calls
    assert calls == []


def labrun(capsys, *argv):
    assert main([str(a) for a in argv]) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def fork_declaration(path, run_id, checkpoint, new_run, policy):
    decl = yaml.safe_load(TEMPLATE.read_text())
    decl["new_run_id"] = new_run
    decl["parent"].update(run_id=run_id, checkpoint_digest=checkpoint)
    decl["intervention"]["to_policy"] = policy
    path.write_text(yaml.safe_dump(decl))
    return path


def test_clean_workspace_sequence(tmp_path, capsys):
    a, b = tmp_path / "origin" / "store-a", tmp_path / "elsewhere" / "store-b"
    first = labrun(capsys, "fixture", "run", "--run-id", "demo", "--store", a)
    labrun(capsys, "export", "demo", "--store", a, "--out", tmp_path / "demo.tar.gz")
    shutil.rmtree(tmp_path / "origin")  # original store, index and scheduler are gone

    labrun(capsys, "import", tmp_path / "demo.tar.gz", "--store", b)
    caps = labrun(capsys, "inspect", "demo", "--store", b, "--capabilities")["capabilities"]
    assert {k: v["status"] for k, v in caps.items()} == dict.fromkeys(caps, "supported")

    replay = labrun(capsys, "replay", "demo", "--store", b, "--offline")
    assert replay["final_digest"] == first["final"] and replay["external_calls"] == 0

    same = labrun(capsys, "reanalyze", "demo", "--store", b)
    assert all(r["matches"] for r in same)

    cps = labrun(capsys, "checkpoint", "list", "demo", "--store", b)
    cp = next(c["checkpoint"] for c in cps if c["boundary"] == "diagnostics-committed")
    parent_journal = (b / "events" / "demo.jsonl").read_bytes()
    for new_run, policy in (("v1", "mock-policy-v1"), ("v2", "mock-policy-v2")):
        decl = fork_declaration(tmp_path / f"{new_run}.yaml", "demo", cp, new_run, policy)
        labrun(
            capsys,
            "fork",
            "demo",
            "--checkpoint",
            cp,
            "--declaration",
            decl,
            "--new-run",
            new_run,
            "--store",
            b,
        )
        assert labrun(capsys, "resume", new_run, "--store", b)["phase"] == "done"

    cmp = labrun(capsys, "compare", "demo", "v1", "v2", "--store", b, "--include-cost-lineage")
    runs = {r["run_id"]: r for r in cmp["runs"]}
    assert cmp["comparison_kind"] == "conditional-branch-not-independent-full-run"
    assert runs["v1"]["actions"] == ["refine", "accept"] and runs["v2"]["actions"] == ["accept"]
    assert runs["v2"]["costs"]["inherited_prefix"]["cost_usd"] == "0.40"
    assert cmp["physical_cost"]["cost_usd"] == "1.20"
    # the parent journal only grew by the reanalysis derivations; its prefix is unchanged
    assert (b / "events" / "demo.jsonl").read_bytes().startswith(parent_journal)

    graded = labrun(capsys, "evaluate", "v2", "--store", b, "--evaluator-store", tmp_path / "ev")
    assert graded["assignments"]["execution_status"] == "pass"
    assert not any((b / "records").glob("evaluation/*"))


def test_cli_errors_are_reported_not_raised(tmp_path, capsys):
    assert main(["replay", "missing", "--store", str(tmp_path / "s")]) == 2
    assert "offline" in capsys.readouterr().err
