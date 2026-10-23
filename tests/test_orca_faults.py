"""Broker fault injection (broker-fault-injection): the plan, the trigger, the cut, the broker.

The broker tests run a labelled stand-in "ORCA" that prints the bytes of an
authentic ORCA 6.1.1 fixture output chosen by the input, so the parser and the
NEVPT2 cut see real output; the jobs themselves are stand-in executions.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_orca_broker import broker, runs, scheduler, stand_in, submit

from onoda.adapters.pi.vte_contract import BrokerAttach
from onoda.canonical import sha256_digest
from onoda.errors import OnodaError
from onoda.science.orca_broker import BrokerReplay, read_journal
from onoda.science.orca_faults import (
    ELIGIBLE_BASES,
    FaultPlan,
    cut_before_nevpt2,
    ineligible,
    load_fault_plan,
    not_a_success,
    requests_nevpt2,
)
from onoda.science.orca_parser import parse_orca_output

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "orca611-nevpt2"
PLAN_YAML = (
    b"schema: onoda.fault-plan/1\nid: kill-nevpt2\n"
    b"fault: killed-before-nevpt2\ntrigger: first-eligible-nevpt2\n"
)

# An eligible single point in the shape `orca_input` writes (ketene, SA-CASSCF + SC-NEVPT2).
TARGET = """! aug-cc-pVTZ RIJCOSX AutoAux TightSCF UseSym
%maxcore 2000
%pal nprocs 4 end
%casscf
  nel 10
  norb 10
  mult 1,1
  irrep 0,2
  nroots 1,1
  bweight = 1,1
  trafostep ri
  maxiter 150
  PTMethod SC_NEVPT2
end
* xyz 0 1
C  0.00000000 0.00000000 -1.29547901
C  0.00000000 0.00000000 0.01851350
O  0.00000000 0.00000000 1.18357799
H  0.00000000 0.93892975 -1.81881304
H  0.00000000 -0.93892975 -1.81881304
*
"""
CASSCF_ONLY = TARGET.replace("  PTMethod SC_NEVPT2\n", "")


def plan() -> tuple[FaultPlan, str]:
    p, canonical = load_fault_plan(PLAN_YAML)
    return p, sha256_digest(canonical)


# --- the plan --------------------------------------------------------------------------


def test_a_plan_loads_to_canonical_bytes_and_refuses_anything_else():
    p, canonical = load_fault_plan(PLAN_YAML)
    assert (p.id, p.fault, p.trigger) == (
        "kill-nevpt2",
        "killed-before-nevpt2",
        "first-eligible-nevpt2",
    )
    # The same plan as JSON pins the same bytes.
    as_json = json.dumps(json.loads(canonical)).encode()
    assert load_fault_plan(as_json)[1] == canonical
    for bad in (
        PLAN_YAML.replace(b"killed-before-nevpt2", b"killed-mid-casscf"),
        PLAN_YAML.replace(b"kill-nevpt2", b"Kill NEVPT2"),
        PLAN_YAML + b"extra: 1\n",
        b"[",
    ):
        with pytest.raises(OnodaError):
            load_fault_plan(bad)


# --- the trigger -----------------------------------------------------------------------


def test_fixture_inputs_requesting_nevpt2_are_eligible_and_others_are_not_triggers():
    for inp in sorted(FIXTURES.glob("*.inp")):
        text = inp.read_text()
        wants = "PTMethod" in text
        assert requests_nevpt2(text) is wants, inp.name
        if wants:
            assert ineligible(text) is None, inp.name


@pytest.mark.parametrize(
    "change",
    [
        ("! aug-cc-pVTZ", "! aug-cc-pVTZ Opt"),
        ("! aug-cc-pVTZ", "! Opt"),  # a run type passed where the basis goes
        ("! aug-cc-pVTZ", "! aug-cc-pVTZ Freq"),
        ("! aug-cc-pVTZ", "! aug-cc-pVTZ NumGrad"),
        ("! aug-cc-pVTZ", "! aug-cc-pVTZ cc-pVDZ"),
        ("! aug-cc-pVTZ", "! 6-31G*"),
        ("%maxcore 2000", "%maxcore 2000\n%method\n  RunTyp Opt\nend"),
        ("%maxcore 2000", "%maxcore 2000\n%geom\nend"),
        ("%maxcore 2000", "%maxcore 2000\n%tddft\nend"),
        ("%maxcore 2000", "%maxcore 2000 # inline"),
        ("  maxiter 150", "  maxiter 150\n  RunTyp Gradient"),
        ("*\n", "*\n$new_job\n! aug-cc-pVTZ\n* xyz 0 1\nH 0 0 0\n*\n"),
        ("PTMethod SC_NEVPT2", "PTMethod SC_NEVPT2\n  PTMethod FIC_NEVPT2"),
        ("end\n* xyz", "end\n%scf\n  NEVPT2 true\nend\n* xyz"),
    ],
)
def test_anything_off_the_single_point_allowlist_is_ineligible(change):
    old, new = change
    assert old in TARGET
    text = TARGET.replace(old, new, 1)
    assert requests_nevpt2(text)
    assert ineligible(text) is not None


def test_casscf_only_is_not_a_trigger():
    assert not requests_nevpt2(CASSCF_ONLY)
    assert ineligible(TARGET) is None


def _orca_input_builds(bases: list[str], tmp_path: Path) -> list[str]:
    """Every NEVPT2 input `orca_input` builds over a grid of its options, via esbuild + node."""
    esbuild = ROOT / "pi" / "node_modules" / ".bin" / "esbuild"
    node = shutil.which("node")
    if node is None or not esbuild.exists():
        pytest.skip("node or pi/node_modules (scripts/pi-setup.sh) not installed")
    bundle = tmp_path / "orca-input.mjs"
    subprocess.run(
        [str(esbuild), str(ROOT / "pi/extensions/lib/orca-input.ts"), "--format=esm",
         f"--outfile={bundle}", "--log-level=warning"],
        check=True,
    )  # fmt: skip
    script = tmp_path / "enumerate.mjs"
    script.write_text(
        """import { buildOrcaInput } from "./orca-input.mjs";
const out = [];
const atoms = ["H 0 0 0", "H 0 0 0.74"];
const two = [{mult: 1, irrep: 0, nroots: 1}, {mult: 3, irrep: 1, nroots: 2}];
for (const basis of JSON.parse(process.argv[2]))
for (const symmetry of [undefined, true, false]) for (const ri of [undefined, true, false])
for (const read_orbitals of [undefined, "j0001"]) for (const rotate of [undefined, [{i: 1, j: 2}]])
for (const roots of [undefined, two]) for (const trah of [undefined, true])
for (const maxiter of [undefined, 300]) for (const charge of [undefined, 1])
  out.push(buildOrcaInput({method: "nevpt2", basis, symmetry, ri, read_orbitals, rotate,
    nel: 2, norb: 2, roots, trah, maxiter, charge}, atoms, new Set(["j0001"])).input);
console.log(JSON.stringify(out));
"""
    )
    done = subprocess.run(
        [node, str(script), json.dumps(bases)], capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def test_every_input_orca_input_builds_with_a_listed_basis_is_eligible(tmp_path):
    built = _orca_input_builds(list(ELIGIBLE_BASES), tmp_path)
    assert len(built) > 1000
    assert [(t, ineligible(t)) for t in built if ineligible(t) is not None] == []


# --- the fault -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["ketene_f_target", "formaldehyde_nevpt2_np4", "ethylene_nevpt2_np4", "acetone_g_trah"]
)
def test_the_cut_keeps_everything_before_the_nevpt2_header(name):
    full = (FIXTURES / f"{name}.out").read_bytes()
    assert not_a_success(full) is None
    cut = cut_before_nevpt2(full)
    assert cut is not None and full.startswith(cut) and cut.endswith(b"\n")
    rest = full[len(cut) :]
    assert rest.startswith(b"-" * 20) and b"< NEVPT2  >" in rest.splitlines()[1]
    assert b"< NEVPT2  >" not in cut and b"NEVPT2 TRANSITION ENERGIES" not in cut
    # The cut only removes: no NEVPT2 state, no normal termination, so nothing can be credited.
    obs = parse_orca_output(cut)
    assert obs.termination == "absent"
    assert not any(s.method.endswith("NEVPT2") for s in obs.mr_states)
    assert not_a_success(cut) is not None


def test_no_header_means_no_cut_and_a_failed_casscf_is_not_a_success():
    assert cut_before_nevpt2((FIXTURES / "ketene_d_valence_avtz_sym.out").read_bytes()) is None
    failed = (FIXTURES / "ethylene_nevpt2_np4_default_orbstep.out").read_bytes()
    assert not_a_success(failed) is not None


# --- the broker ------------------------------------------------------------------------

# Prints a fixture output chosen by the input, and leaves the products a NEVPT2 job leaves.
FIXTURE_ORCA = f"""#!/usr/bin/env {sys.executable}
import os, sys
open("ran.stand-in", "w").write("")
text = open(sys.argv[1]).read()
here = os.path.dirname(os.path.abspath(__file__))
name = (
    "ethylene_nevpt2_np4_default_orbstep" if "FAILCAS" in text
    else "ketene_f_target" if "NEVPT2" in text
    else "ketene_d_valence_avtz_sym"
)
sys.stdout.buffer.write(open(os.path.join(here, name + ".out"), "rb").read())
open("job.gbw", "w").write("orbitals for " + text)
open("job_property.txt", "w").write("NEVPT2 results for " + text)
"""


def fixture_orca(tmp_path: Path) -> Path:
    binary = stand_in(tmp_path, FIXTURE_ORCA)
    for out in FIXTURES.glob("*.out"):
        shutil.copy(out, binary.parent / out.name)
    return binary


def out(reply) -> bytes:
    return base64.b64decode(reply.output_b64)


def faulted(tmp_path: Path, binary: Path, name: str = "run1", **kw):
    return broker(
        tmp_path, name, scheduler=scheduler(tmp_path, binary, f"w-{name}"), fault_plan=plan(), **kw
    )


def test_the_first_eligible_nevpt2_job_is_executed_then_served_as_a_timeout(tmp_path):
    binary = fixture_orca(tmp_path)
    cache = tmp_path / "cache"
    b = faulted(tmp_path, binary, cache_dir=cache)
    first = b.submit(submit(CASSCF_ONLY, "c1"))  # not a trigger
    assert (
        first.status == "completed"
        and out(first) == (FIXTURES / "ketene_d_valence_avtz_sym.out").read_bytes()
    )
    r = b.submit(submit(TARGET, "c2"))
    real = (FIXTURES / "ketene_f_target.out").read_bytes()
    assert (r.status, r.reason, r.executed_here, r.cache_hit) == ("failed", "timeout", True, False)
    assert out(r) == cut_before_nevpt2(real)
    assert r.files == ("job.gbw",)  # what a kill before NEVPT2 leaves; job_property.txt is withheld
    assert runs(binary) == 2 and b.fault_job == "j0002"
    # Nothing in the reply says it was injected.
    shown = r.model_dump_json().lower()
    assert "inject" not in shown and "kill-nevpt2" not in shown and "fault" not in shown
    # The timing tell: the reply states neither the wall limit nor the job's duration.
    assert "wall" not in shown and "duration" not in shown

    entry = read_journal(b.journal_path)[-1]
    assert entry.injected is not None and entry.injected.outcome == "fired"
    assert entry.injected.plan_sha256 == plan()[1] and entry.injected.transform_version == "1"
    assert entry.output_sha256 == sha256_digest(out(r))
    assert entry.real_output_sha256 == sha256_digest(real)
    assert (entry.real_status, entry.real_reason) == ("completed", None)
    assert [f.name for f in entry.real_files] == ["job.gbw", "job_property.txt"]
    assert [f.name for f in entry.files] == ["job.gbw"]
    assert (b.real_dir / sha256_digest(real).removeprefix("sha256:")).read_bytes() == real
    # The real output stays out of the shared cache (only the clean CASSCF job is there).
    assert len([p for p in cache.iterdir() if not p.name.startswith(".")]) == 1
    # The withheld product cannot be attached.
    prop = (BrokerAttach(job_id="j0002", name="job_property.txt"),)
    assert b.submit(submit(CASSCF_ONLY + "\n", "c3", prop)).reason == "unknown_attachment"


def test_the_fault_is_transient_and_the_retry_is_a_normal_job(tmp_path):
    binary = fixture_orca(tmp_path)
    b = faulted(tmp_path, binary, cache_dir=tmp_path / "cache")
    assert b.submit(submit(TARGET, "c1")).reason == "timeout"
    again = b.submit(submit(TARGET, "c2"))
    assert again.status == "completed" and again.executed_here and not again.cache_hit
    assert out(again) == (FIXTURES / "ketene_f_target.out").read_bytes()
    assert read_journal(b.journal_path)[-1].injected is None
    assert b.jobs == 2 and runs(binary) == 2  # the faulted job counts toward max_executions


def test_the_trigger_skips_a_warm_cache(tmp_path):
    binary = fixture_orca(tmp_path)
    cache = tmp_path / "cache"
    clean = broker(
        tmp_path, "clean", scheduler=scheduler(tmp_path, binary, "w-clean"), cache_dir=cache
    )
    assert clean.submit(submit(TARGET)).status == "completed"
    b = faulted(tmp_path, binary, cache_dir=cache)
    r = b.submit(submit(TARGET))
    assert r.executed_here and not r.cache_hit and r.reason == "timeout"
    assert runs(binary) == 2


def test_an_ineligible_nevpt2_job_runs_normally_and_keeps_the_trigger(tmp_path):
    binary = fixture_orca(tmp_path)
    b = faulted(tmp_path, binary)
    r = b.submit(submit(TARGET.replace("UseSym", "UseSym LargePrint Freq"), "c1"))
    assert r.status == "completed" and read_journal(b.journal_path)[-1].injected is None
    assert b.submit(submit(TARGET, "c2")).reason == "timeout"


def test_a_real_failure_is_served_as_is_and_consumes_the_trigger(tmp_path):
    binary = fixture_orca(tmp_path)
    b = faulted(tmp_path, binary)
    real = b.submit(submit(TARGET.replace("maxiter 150", "maxiter 150\n  # FAILCAS"), "c1"))
    assert out(real) == (FIXTURES / "ethylene_nevpt2_np4_default_orbstep.out").read_bytes()
    entry = read_journal(b.journal_path)[-1]
    assert entry.injected is not None and entry.injected.outcome == "skipped"
    assert entry.injected.skip_reason == "termination error"
    assert entry.real_output_sha256 is None
    later = b.submit(submit(TARGET, "c2"))
    assert later.status == "completed" and read_journal(b.journal_path)[-1].injected is None
    assert not b.real_dir.exists()


def test_replay_serves_the_served_bytes_and_keeps_the_injected_record(tmp_path):
    binary = fixture_orca(tmp_path)
    b = faulted(tmp_path, binary)
    r = b.submit(submit(TARGET))
    recorded = read_journal(b.journal_path)
    replay = BrokerReplay.from_journal(recorded, {r.output_sha256: out(r)})
    again = broker(tmp_path, "replay", replay=replay)
    got = again.submit(submit(TARGET))
    assert (got.status, got.reason, out(got), got.files) == (r.status, r.reason, out(r), r.files)
    entry = read_journal(again.journal_path)[-1]
    assert entry.replay and not entry.executed_here
    assert entry.injected == recorded[-1].injected
    assert entry.real_output_sha256 == recorded[-1].real_output_sha256
    assert runs(binary) == 1


def test_a_fault_plan_needs_a_fresh_live_broker(tmp_path):
    binary = fixture_orca(tmp_path)
    live = broker(tmp_path, "live", scheduler=scheduler(tmp_path, binary))
    r = live.submit(submit(TARGET))
    replay = BrokerReplay.from_journal(read_journal(live.journal_path), {r.output_sha256: out(r)})
    with pytest.raises(OnodaError, match="fault plan"):
        broker(tmp_path, "r", replay=replay, fault_plan=plan())
    with pytest.raises(OnodaError, match="fault plan"):
        faulted(tmp_path, binary, "fork", prefix=replay)


def test_the_pilot_plan_file_is_this_plan():
    shipped = (ROOT / "experiments/questdb/fault-plan-killed-before-nevpt2.yaml").read_bytes()
    assert load_fault_plan(shipped)[1] == load_fault_plan(PLAN_YAML)[1]
