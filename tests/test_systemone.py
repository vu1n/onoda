"""A67: System One decision models (Jev, Clef) and the shadow pass's inputs. Offline only."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from onoda import systemone
from onoda.adapters.pi.vte_shadow import excerpt, ladder_rules, questions, scf_note
from onoda.errors import CapabilityError, OnodaError
from onoda.systemone import LiveSystemOne, ScriptedSystemOne

ROOT = Path(__file__).parents[1]
LADDER = (ROOT / "experiments/questdb/ladder-from-paper.md").read_bytes()
KETENE = (ROOT / "tests/fixtures/orca611-nevpt2/ketene_f_target.out").read_bytes()


def test_rule_criteria_are_the_ladder_paragraphs():
    rules = ladder_rules(LADDER)
    assert list(rules) == list("ABCDEFGH")
    assert rules["A"].startswith("Job failure. The job did not run")
    assert "MCRPA" in rules["G"]  # the whole paragraph, not just its title
    assert questions(LADDER)["rule"]["criteria"] == rules
    with pytest.raises(OnodaError, match="rules A-H"):
        ladder_rules(b"**A. Only one.** rule\n")


def test_excerpt_keeps_the_input_and_the_casscf_results_verbatim():
    text = excerpt(KETENE)
    assert text.startswith("|  1>")  # the input echo: what the agent asked for
    assert "Final CASSCF energy" in text and "NO   OCC" in text  # natural occupations
    assert "CAS-SCF STATES FOR BLOCK  1 MULT= 1 IRREP= B1" in text
    assert "NEVPT2 TRANSITION ENERGIES" in text
    assert len(text) <= 12000 + 20 and len(text) < len(KETENE) / 10


def test_an_absent_scf_is_said_in_plain_words():
    # Jev read the parser's bare "absent" as rule B (SCF failure) on jobs that ran no SCF.
    note = scf_note("absent", KETENE)
    assert note.startswith("no SCF ran in this job (its starting orbitals are read from a file")
    assert "not an SCF convergence failure" in note
    guess = (ROOT / "tests/fixtures/orca611-nevpt2/formaldehyde_guess.out").read_bytes()
    assert "(the output has no SCF section)" in scf_note("absent", guess)
    assert scf_note("converged", KETENE) is None


def test_live_decisions_are_off_unless_enabled_and_need_credentials(monkeypatch):
    with pytest.raises(CapabilityError, match="live decisions are off"):
        LiveSystemOne("jev")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(CapabilityError, match="OPENROUTER_API_KEY"):
        LiveSystemOne("jev", enabled=True)
    with pytest.raises(OnodaError, match="unknown decision provider"):
        LiveSystemOne("gpt", enabled=True)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_jev_call_sends_the_body_without_credentials_and_reads_decimals(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret")
    sent = {}

    def urlopen(req, timeout):
        sent["url"], sent["auth"], sent["body"] = (
            req.full_url,
            req.headers["Authorization"],
            req.data,
        )
        return _Resp(
            b'{"answers":{"ready":{"type":"noul","noul":0.123456789}},"usage":{"cost":2.3e-05}}'
        )

    monkeypatch.setattr(systemone.urllib.request, "urlopen", urlopen)
    ans = LiveSystemOne("jev", enabled=True).ask({"x": 1}, {"ready": {"type": "noul"}})
    assert sent["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert sent["auth"] == "Bearer sk-test-secret"
    assert json.loads(ans.request)["model"] == "typesafe/jev-1.13"
    assert b"sk-test-secret" not in ans.request + ans.response
    assert ans.answers["ready"]["noul"] == Decimal("0.123456789")
    assert ans.cost_usd == "0.000023" and not ans.synthetic


def test_clef_unwraps_the_workers_ai_envelope(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    urls = []

    def urlopen(req, timeout):
        urls.append(req.full_url)
        body = {"success": True, "result": {"answers": {"rule": {"choice": "E"}}, "usage": {}}}
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(systemone.urllib.request, "urlopen", urlopen)
    ans = LiveSystemOne("clef", enabled=True).ask("s", {"rule": {"type": "choice"}})
    assert urls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/@cf/cloudflare/clef-flash"
    ]
    assert ans.answers["rule"]["choice"] == "E" and ans.cost_usd is None  # unknown, never zero


def test_a_failed_or_empty_response_is_an_error():
    with pytest.raises(OnodaError, match="call failed"):
        systemone._answers(b'{"success": false, "result": null, "errors": ["auth"]}')
    with pytest.raises(OnodaError, match="no answers"):
        systemone._answers(b'{"model": "x"}')


def test_scripted_answers_are_synthetic():
    ans = ScriptedSystemOne(lambda s, q: {"ready": {"noul": 0.5}}).ask("s", {"ready": {}})
    assert ans.synthetic and ans.answers["ready"]["noul"] == Decimal("0.5")
