"""Extract the phase-0 candidate molecules from a QUESTDB checkout into questdb_subset.json.

Usage: uv run python extract.py /path/to/QUESTDB   (https://github.com/pfloos/QUESTDB)

Values are copied as QUESTDB's JSON holds them (energies in eV, geometries in
angstrom); nothing is converted or rounded. Stdlib only.
"""

import json
import subprocess
import sys
from pathlib import Path

src = Path(sys.argv[1])
here = Path(__file__).parent

# name -> (data/json/MAIN file, geometries/xyz files, data/cas/json file or None)
MOLECULES = {
    "water": ("Water", ["water"], None),
    "ammonia": ("Ammonia", ["ammonia"], None),
    "hydrogen_sulfide": ("Hydrogen_sulfide", ["hydrogen_sulfide"], None),
    "formaldehyde": ("Formaldehyde", ["formaldehyde_1", "formaldehyde_2"], "formaldehyde_mrpt"),
    "ethylene": ("Ethylene", ["ethylene"], "ethylene_mrpt"),
    "acetylene": ("Acetylene", ["acetylene_1", "acetylene_2", "acetylene_3"], None),
    "carbon_monoxide": ("Carbon_monoxide", ["carbon_monoxide"], None),
    "dinitrogen": ("Dinitrogen", ["dinitrogen"], None),
}
DESCRIPTIVE = {
    "Molecule", "Size", "Group", "State", "Spin", "V/R", "Type", "Special ?",
    "%T1 [CC3/AVTZ]", "f [LR-CC3/AVTZ]", "TBE/AVTZ", "Method", "Safe ? (~50 meV)",
    "TBE/AVQZ", "Corr. Method",
}  # fmt: skip
MULTIREF = (
    "CASSCF",
    "SC-NEVPT2",
    "PC-NEVPT2",
    "CASPT2",
    "CASPT2 (No IPEA)",
    "CASPT3",
    "CASPT3 (No IPEA)",
)


def geometry(name: str) -> dict:
    lines = (src / "geometries/xyz" / f"{name}.xyz").read_text().splitlines()
    atoms = [
        dict(zip(("element", "x", "y", "z"), ln.split(), strict=True))
        for ln in lines[2:]
        if ln.strip()
    ]
    assert len(atoms) == int(lines[0]), name
    return {
        "file": f"geometries/xyz/{name}.xyz",
        "comment": lines[1],
        "units": "angstrom",
        "atoms": atoms,
    }


out = {
    "source": {
        "repository": "https://github.com/pfloos/QUESTDB",
        "commit": subprocess.check_output(
            ["git", "-C", str(src), "rev-parse", "HEAD"], text=True
        ).strip(),
        "license": "CC-BY-SA-4.0",
        "energy_units": "eV",
    },
    "molecules": {},
}
for name, (data_file, geoms, cas_file) in MOLECULES.items():
    rows = json.loads((src / "data/json/MAIN" / f"{data_file}.json").read_text())
    transitions = []
    for row in rows:
        transitions.append(
            {
                "state": row["State"],
                "spin_multiplicity": row["Spin"],
                "valence_or_rydberg": row.get("V/R"),
                "type": row.get("Type", "").strip() or None,
                "special": row.get("Special ?"),
                "tbe_avtz_ev": row.get("TBE/AVTZ"),
                "tbe_method": row.get("Method"),
                "safe": row.get("Safe ? (~50 meV)"),
                "tbe_avqz_ev": row.get("TBE/AVQZ"),
                "percent_t1_cc3_avtz": row.get("%T1 [CC3/AVTZ]"),
                "oscillator_strength_lr_cc3_avtz": row.get("f [LR-CC3/AVTZ]"),
                "multireference_ev": {k: row[k] for k in MULTIREF if k in row},
                "other_methods_ev": {
                    k: v for k, v in row.items() if k not in DESCRIPTIVE and k not in MULTIREF
                },
            }
        )
    cas = (
        json.loads((src / "data/cas/json" / f"{cas_file}.json").read_text())["data"]
        if cas_file
        else None
    )
    out["molecules"][name] = {
        "questdb_name": rows[0]["Molecule"].strip(),
        "data_file": f"data/json/MAIN/{data_file}.json",
        "geometries": [geometry(g) for g in geoms],
        "active_space_file": f"data/cas/json/{cas_file}.json" if cas_file else None,
        "active_spaces": cas,
        "transitions": transitions,
    }

(here / "questdb_subset.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
for name, m in out["molecules"].items():
    t = m["transitions"]
    mr = sum(1 for x in t if x["multireference_ev"])
    print(
        f"{name}: {len(t)} transitions, {mr} with multireference values, "
        f"{len(m['geometries'])} geometries"
    )
