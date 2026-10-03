# QUESTDB phase 0 (2026-10-03)

Scoping notes: what QUESTDB provides for eight small molecules, and how long a
small SA-CASSCF + SC-NEVPT2 job takes on this Mac. The raw QUESTDB files for the
proposed eight molecules are in `questdb-raw/` (CC BY-SA 4.0, see its
`LICENSE.md`); the real ORCA outputs are in `tests/fixtures/orca611-nevpt2/`.

## Source

- Repository: https://github.com/pfloos/QUESTDB, commit
  `460482bcc6231dab41fe32188dc43959a7c2bdaa` (2026-07-30), shallow clone.
- Paper: Loos, Boggio-Pasqua, Blondel, Lipparini, Jacquemin, "The QUEST
  database of highly-accurate excitation energies", J. Chem. Theory Comput.
  21, 8010 (2025), arXiv:2506.11590, doi:10.1021/acs.jctc.5c00975.
- Archive DOI given in the repo README: 10.5281/zenodo.15671384.
- Website: `questdb.irsamc.ups-tlse.fr` did not resolve from this machine;
  https://lcpq.github.io/QUESTDB_website/ answers but was not used. All data
  here come from the GitHub repository.
- Licence: the repository's `LICENSE` file is Creative Commons
  Attribution-ShareAlike 4.0 International (GitHub reports CC-BY-SA-4.0). The
  README badge reads "CC BY SA 4.0" but links to the CC BY 4.0 deed. ShareAlike
  means adapted material that is shared must carry the same licence, so
  `questdb_subset.json` should be treated as CC-BY-SA-4.0 with attribution if
  it is ever committed or redistributed. Not legal advice; check before
  vendoring.

## Files

- `extract.py`: builds `questdb_subset.json` from a QUESTDB checkout. Values
  are copied unchanged (energies in eV as JSON numbers, coordinates as the
  printed angstrom strings).
- `questdb_subset.json`: per molecule, the geometries, every listed
  transition (state label, spin multiplicity, valence/Rydberg, type, special
  flag, TBE/aug-cc-pVTZ, the TBE's method, safe flag, TBE/aug-cc-pVQZ, %T1,
  oscillator strength, multireference values, all other method columns), and
  the active-space table where QUESTDB has one.
- `make_inputs.py`, `inputs/`: the ORCA inputs for the timing runs.
- `timings.json`: wall times and the printed transition energies.
- ORCA outputs: the local job folders are git-excluded; the ones worth keeping
  are copied to `tests/fixtures/orca611-nevpt2/`.

## What QUESTDB provides for the eight candidates

All geometries are CC3(Full)/aug-cc-pVTZ. Every transition has a theoretical
best estimate (TBE) at aug-cc-pVTZ, and all 71 are flagged safe.

| molecule | transitions (singlet/triplet) | TBE reference method | CASSCF/NEVPT2 values | geometries |
|---|---|---|---|---|
| water | 6 (3/3) | exFCI/AVTZ | none | 1 |
| ammonia | 5 (4/1) | CCSDTQP/AVDZ composite; exFCI/AVTZ for the triplet | none | 1 |
| hydrogen sulfide | 4 (2/2) | exFCI/AVTZ | none | 1 |
| formaldehyde | 16 (9/7) | CCSDTQP/6-31+G(d) composites; exFCI/AVTZ for the double excitation | 1 of 16 (the 1A1 double excitation) | 2 (ground state, S1 for one fluorescence entry) |
| ethylene | 7 (4/3) | CCSDTQP, CCSDTQ and exFCI composites | 1 of 7 (the 1Ag double excitation) | 1 |
| acetylene | 7 (4/3) | exFCI/AVTZ or exFCI/AVDZ + CCSDT correction | none | 3 (ground state, two excited-state geometries for fluorescence entries) |
| carbon monoxide | 12 (6/6) | exFCI/AVDZ or CCSDTQP/AVDZ + CCSDTQ correction | none | 1 |
| dinitrogen | 14 (8/6) | exFCI/AVDZ or CCSDTQP/AVDZ + CCSDTQ correction | none | 1 |

- No transition in these eight molecules uses CASSCF or NEVPT2 as its
  reference. The references are exFCI or high-order coupled cluster.
- Multireference columns (CASSCF, CASPT2 with and without IPEA, CASPT3,
  SC-NEVPT2, PC-NEVPT2) exist only for the two genuine double excitations,
  each with its active space and state-averaging in `data/cas/json/`:
  formaldehyde 1A1, active (a1,b1,b2,a2) = (2,2,1,0), 3 A1 states, SC-NEVPT2
  10.296 eV against TBE 10.426; ethylene 1Ag, active (1,1,1,1,0,0,0,0),
  2 Ag states, SC-NEVPT2 13.262 eV against TBE 12.899.
- Water, ammonia and hydrogen sulfide list only Rydberg transitions; acetylene
  lists only valence ones; formaldehyde, ethylene, CO and N2 list both.
- `data/cas/README.md` mentions raw CASSCF/MRPT outputs under `data/cas/out/`;
  that directory is not in the repository at this commit.

## Timing

One run each, jobs run one at a time, wall time from `/usr/bin/time`.

| job | active space, roots | basis functions | 4 processes | 16 processes | termination |
|---|---|---|---|---|---|
| formaldehyde | CAS(6,5), 3 singlets + 2 triplets | 138 | 16.7 s | 22.9 s | normal |
| ethylene (`orbstep TRAH`) | CAS(2,2), 3 singlets + 1 triplet | 184 | 14.7 s | 23.7 s | normal |
| ethylene (default orbital step) | same | 184 | 63.4 s | 70.6 s | error: CASSCF did not converge |

Each was preceded by an SA-CASSCF/cc-pVDZ job for starting orbitals (3.4 s
and 2.5 s).

## Surprises

1. 16 processes is slower than 4 for jobs this small (about 23 s against
   15 to 17 s). The M4 Max has 12 performance and 4 efficiency cores.
2. Ethylene's CASSCF(2,2) diverges in aug-cc-pVTZ with ORCA's default orbital
   step (energy jumps from -77.84 to -66.77 Eh on the first step and never
   recovers in 75 iterations). Level shifts, singlets only, and fewer roots
   did not help; `orbstep TRAH` converged. One run of each variant.
3. ORCA again returned exit code 0 on that error termination.
4. With 16 processes OpenMPI prints "A system call failed during shared
   memory initialization" (unlink of a vader segment) to stderr; the jobs
   still finished normally.
5. The printed states are not assigned. No point-group symmetry was used, so
   ORCA gives root numbers and multiplicities only; mapping them to QUESTDB
   state labels is exactly the state-identification problem the harness has
   to solve. A valence-only active space will also not describe the Rydberg
   transitions, which are all that water, ammonia and H2S have.

# Phase 0b: QUESTDB's multireference data

Built by `cas_inventory.py` into `cas_inventory.json` (every table row, the
docx caption and footnotes, the matching data and geometry files).

## What the active-space tables record

- 86 tables in `data/cas/json/` (not 87, as phase 0 said), 571 state rows in
  total. Each is the JSON conversion of a hand-curated Word table in
  `data/cas/docx/`, captioned "MRPT/aug-cc-pVTZ vertical transition energies
  (eV) of <molecule>".
- Per state row: a state label with character, e.g. `1B2(n,3s)`; the number
  of active orbitals per irreducible representation; the state-averaging
  scheme as states per irrep (ground state always included); and excitation
  energies for CASSCF, CASPT2 with and without IPEA shift, SC-NEVPT2,
  PC-NEVPT2, and usually CASPT3 with and without IPEA. A few tables add
  MS/XMS-CASPT2, QD-NEVPT2 or frozen-core variants.
- The active space differs from state to state within a molecule. Acetone
  uses five: (6e,5o) valence, (4e,4o), and (6e,6o)/(6e,7o) with 3s or 3p
  Rydberg orbitals added for the Rydberg states.
- Not recorded in the JSON: the number of active electrons and which
  orbitals they are. Those are only in the docx footnotes (copied into
  `cas_inventory.json`); the repo's converter strips the footnote letters
  that tie a footnote to a row. State-average weights are not stated.
- State labels use a Symbol-font private-use character for pi and are not
  the same strings as the `State` field of the per-molecule data files, so
  joining the two is by order and value, not by key.
- Geometry: not in the tables. Every table's molecule has a
  `geometries/xyz/` file (CC3/aug-cc-pVTZ), found by name.

## Where the NEVPT2 values are

- Machine-readable in two places: the cas tables (3 decimals, strings) and
  the `SC-NEVPT2` / `PC-NEVPT2` / `CASSCF` / `CASPT2` / `CASPT3` fields of
  the per-molecule files in `data/json/` (keyed to the same record as the
  TBE, state label, spin and valence/Rydberg flag).
- 81 molecules have an `SC-NEVPT2` value in their data file: 50 in MAIN
  (316 transitions), 23 radicals (177), 8 transition-metal diatomics (45).
  None in CHROM or BIO. The paper's "84 molecules" matches neither 81 nor 86.
- The two places do not always agree: for 38 molecules the values are
  identical, for 27 the data file holds the cas value rounded to 2 decimals,
  for 18 the number of states differs, and for 3 the values differ. Pentalene
  has NEVPT2 values but no cas table; beryllium, Criegee intermediate, CuF,
  CuH, hexatriene and ZnH have a cas table but no NEVPT2 in the data file.
- Origin: the repository does not cite a source for these columns. The
  obvious candidate is Sarkar, Loos, Boggio-Pasqua, Jacquemin, "Assessing the
  Performances of CASPT2 and NEVPT2 for Vertical Excitation Energies",
  J. Chem. Theory Comput. 18, 2418 (2022), arXiv:2111.15386, plus the 2025
  QUEST paper's SI for later additions. I have not opened either to confirm
  values or protocol details (program, RI, frozen core).
- `data/cas/README.md` describes raw outputs in `data/cas/out/`; that
  directory is not in the repository.

## Proposed eight (closed shell, at most 4 non-hydrogen atoms)

Counts are transitions with an SC-NEVPT2 reference. All eight have cas tables
that agree with their data files to rounding.

| molecule | heavy atoms | atoms | transitions (singlet/triplet) | valence / Rydberg / mixed | active spaces in the cas table |
|---|---|---|---|---|---|
| acetaldehyde | 3 | 7 | 2 (1/1) | 2 / 0 / 0 | (6e,5o) |
| formamide | 3 | 6 | 6 (4/2) | 3 / 1 / 2 | (8e,7o), (10e,8o), (10e,10o) |
| ketene | 3 | 5 | 8 (4/4) | 4 / 4 / 0 | (10e,9o), (10e,10o) |
| diazomethane | 3 | 5 | 8 (4/4) | 5 / 3 / 0 | (10e,9o), (10e,10o) |
| acetone | 4 | 10 | 7 (5/2) | 3 / 4 / 0 | (4e,4o), (6e,5o), (6e,6o), (6e,7o) |
| thioacetone | 4 | 10 | 7 (5/2) | 4 / 3 / 0 | (4e,4o), (6e,5o), (6e,6o), (6e,7o) |
| methylenecyclopropene | 4 | 8 | 6 (4/2) | 4 / 2 / 0 | (4e,4o) to (4e,7o) |
| cyclopropenone | 4 | 6 | 11 (7/4) | 8 / 3 / 0 | (6e,7o) |

55 transitions: 33 valence, 20 Rydberg, 2 mixed; 34 singlet, 21 triplet.

- Ketene and diazomethane each include one fluorescence entry, which needs
  the excited-state geometry (`ketene_2.xyz`, `diazomethane_2.xyz` exist).
  Dropping those leaves 53.
- Alternates: butadiene (9; 4 valence, 5 Rydberg; active spaces up to
  (10e,12o) and one partial double), isobutene (3; 1 valence, 2 Rydberg),
  cyclopropenethione (10; 8/2), glyoxal (9; 8/1, (14e,12-13o)).
- Closed-shell molecules with 1 or 2 heavy atoms offer only double
  excitations: ethylene, formaldehyde and nitroxyl have a NEVPT2 value for
  one each and the carbon dimer for three. Water, ammonia, H2S, acetylene,
  CO and N2 have none.
- These are bigger than the phase-0 timing jobs (up to 10 atoms and
  (10e,10o)); timings above do not transfer.

# Phase 0c: reproducing one QUESTDB row each for ketene and acetone

SA-CASSCF + SC-NEVPT2 / aug-cc-pVTZ / RIJCOSX / AutoAux, 4 processes, point-group
symmetry on (`UseSym`, C2v), equal state weights (QUESTDB does not state its
weights). Inputs in `inputs_0c/`; outputs in `tests/fixtures/orca611-nevpt2/`.

| target row | active space, state average | wall time (target job) | termination | SA-CASSCF, eV (this run / QUESTDB) | SC-NEVPT2, eV (this run / QUESTDB) |
|---|---|---|---|---|---|
| ketene 1B1(pi,3s) | (10e,10o), (a1,b1,b2,a2) = (5,3,2,0); A1:1, B1:1 | 42.7 s | normal | 5.217 / 5.216 | 6.069 / 6.067 |
| acetone, lower B2 root vs 1B2(n,3s) | (6e,7o), (4,2,1,0); A1:1, B2:2 | 86.7 s (TRAH) | normal | 6.622 / 5.502 | 6.914 / 6.697 |
| acetone, upper B2 root vs 1B2(n,3pz), same job | same | same | same | 9.350 / 6.438 | 4.614 / 7.800 |

- Ketene reproduces the table to 0.002 eV.
- Acetone does not. The job ends normally with the right number of active
  orbitals per irrep, but the state-averaged orbitals converge to a different
  solution from QUESTDB's, and SC-NEVPT2 reorders the two B2 states (the root
  that is higher at CASSCF comes out at 4.614 eV, the lower one at 6.914 eV),
  so I have not assigned either to a QUESTDB row. Three orbital optimisers
  gave three outcomes: the default did not converge in 75 iterations (282 s);
  level-shifted SuperCI/DIIS converged in 356 s (run alongside another job)
  to two near-degenerate B2 states at 8.44 eV; TRAH converged in 87 s to the
  solution in the table. One attempt each; I stopped there.
- Preparation is the larger cost and was hand-steered: RHF and valence
  CASSCF in cc-pVDZ to pick valence orbitals by irrep (39 s ketene, 9 s
  acetone), valence CASSCF in aug-cc-pVTZ (20 s, 53 s), then adding the
  lowest a1 external orbitals as 3s/3pz.
- ORCA traps met on the way: projecting orbitals to a larger basis with
  `UseSym` on scrambles the active space (wrong irreps, energy 0.08 Eh too
  high, still "terminated normally"); reading acetone's orbitals with
  `UseSym` re-sorts them by energy, so the active orbitals had to be rotated
  back by index (ketene's were left in place); the default orbital listing
  is truncated, so finding them needed `Print[P_OrbEn] 2`.
- OpenMPI: the C-only build from the first session fails in ORCA with
  `MPI_Type_match_size ... MPI_ERR_ARG` (seen in the ketene CASSCF with 825
  configurations and in the acetone aug-cc-pVTZ RI startup). Rebuilt OpenMPI
  4.1.8 with Fortran support (gfortran 16.2) into the same `orca-openmpi/`;
  both then ran. Phase 0 and the six cclib cases ran on the old build without
  hitting this.
