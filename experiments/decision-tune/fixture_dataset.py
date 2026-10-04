"""Hand-labelled fixture examples for `onoda decide tune` (README.md; not grader labels).

Usage: uv run python experiments/decision-tune/fixture_dataset.py ds.jsonl
"""

import sys
from pathlib import Path
from types import SimpleNamespace

from onoda.adapters.pi.vte_shadow import job_state
from onoda.decision_tune import Example, dump_dataset
from onoda.science.orca_checks import job_checks, job_facts
from onoda.science.vte import load_vte_case

FIXTURES = Path("tests/fixtures/orca611-nevpt2")
CHAINS = {  # each molecule's jobs in the order they were run
    "formaldehyde": ["formaldehyde_guess", "formaldehyde_nevpt2_np4"],
    "ethylene": ["ethylene_guess", "ethylene_nevpt2_np4", "ethylene_nevpt2_np4_default_orbstep"],
    "ketene": [
        "ketene_a_rhf",
        "ketene_b_valence_dz",
        "ketene_c_valence_avtz_nosym",
        "ketene_d_valence_avtz_sym",
        "ketene_f_target",
    ],
    "acetone": [
        "acetone_a_rhf",
        "acetone_b_valence_dz",
        "acetone_c_valence_avtz_nosym",
        "acetone_g_trah",
    ],
}
READY = {"ketene_f_target"}  # reproduces QUESTDB; acetone_g_trah is a different CASSCF solution
UNLABELLED = {"formaldehyde_nevpt2_np4", "ethylene_nevpt2_np4"}  # no symmetry: no target match


def stub_case(molecule: str, irrep: str):
    """Formaldehyde and ethylene have no QUESTDB case here; one target stands in."""
    target = SimpleNamespace(count=1, spin=1, irrep=irrep)
    return SimpleNamespace(manifest=SimpleNamespace(molecule=molecule, targets=[target]))


def main(out: str) -> None:
    cases = {m: load_vte_case(f"cases-dev/questdb/{m}/{m}.vte.yaml") for m in ("ketene", "acetone")}
    cases["formaldehyde"] = stub_case("formaldehyde", "A2")
    cases["ethylene"] = stub_case("ethylene", "B1u")
    examples = []
    for molecule, jobs in CHAINS.items():
        earlier = []
        for job in jobs:
            log = (FIXTURES / f"{job}.out").read_bytes()
            facts = job_facts(log)
            checks = job_checks(job, facts, earlier)
            earlier.append((job, facts))
            if job not in UNLABELLED:
                state = job_state(cases[molecule], job, log, checks)
                examples.append(Example(job, state, {"ready": "yes" if job in READY else "no"}))
    Path(out).write_text(dump_dataset(examples))
    print(f"{len(examples)} examples -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
