"""Write the phase-0 timing inputs from questdb_subset.json (QUESTDB ground-state geometries)."""

import json
from pathlib import Path

here = Path(__file__).parent
subset = json.loads((here / "questdb_subset.json").read_text())["molecules"]
# molecule -> (active electrons, active orbitals, roots for singlets,triplets, step-B extras)
# Ethylene's default SuperCI_PT orbital step diverges in aug-cc-pVTZ; TRAH converges.
SPECS = {"formaldehyde": (6, 5, "3,2", ""), "ethylene": (2, 2, "3,1", "  orbstep TRAH\n")}

CASSCF = "%casscf\n  nel {nel}\n  norb {norb}\n  mult 1,3\n  nroots {roots}\n{extra}end\n"
(here / "inputs").mkdir(exist_ok=True)
for name, (nel, norb, roots, extra) in SPECS.items():
    geom = subset[name]["geometries"][0]
    xyz = "".join(f"{a['element']} {a['x']} {a['y']} {a['z']}\n" for a in geom["atoms"])
    block = f"* xyz 0 1\n{xyz}*\n"
    (here / "inputs" / f"{name}_guess.inp").write_text(
        f"# Step A: valence starting orbitals. SA-CASSCF({nel},{norb}) in cc-pVDZ, where the\n"
        "# frontier virtuals are valence rather than diffuse.\n"
        "! RHF cc-pVDZ TightSCF\n%maxcore 2000\n%pal nprocs 4 end\n"
        + CASSCF.format(nel=nel, norb=norb, roots=roots, extra="")
        + block
    )
    for nprocs in (4, 16):
        (here / "inputs" / f"{name}_nevpt2_np{nprocs}.inp").write_text(
            f"# Step B (timed): SA-CASSCF({nel},{norb}) + SC-NEVPT2 / aug-cc-pVTZ / RIJCOSX /\n"
            f"# AutoAux, orbitals projected from step A. Geometry: QUESTDB {geom['file']}.\n"
            f'! aug-cc-pVTZ RIJCOSX AutoAux TightSCF MORead\n%moinp "{name}_guess.gbw"\n'
            f"%maxcore 2000\n%pal nprocs {nprocs} end\n"
            + CASSCF.format(
                nel=nel,
                norb=norb,
                roots=roots,
                extra=f"  trafostep ri\n  PTMethod SC_NEVPT2\n{extra}",
            )
            + block
        )
