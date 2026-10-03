# ORCA 6.1.1 SA-CASSCF / SC-NEVPT2 fixtures

Authentic ORCA output, executed locally on 2026-10-02 (QUESTDB scoping, phases
0 and 0c; see `experiments/questdb-phase0/README.md`). Bytes are exactly as ORCA
wrote them, including one `Working dir.` line with the local path.

- Program: ORCA 6.1.1, macOS arm64 build (`orca_6_1_1_macosx_arm64_openmpi411`),
  on an Apple M4 Max. Parallel runs used a locally built OpenMPI 4.1.8.
- Command, run in the job directory, one job at a time:

      PATH=<repo>/orca-openmpi/bin:$PATH /usr/bin/time -p <repo>/orca-6.1.1/orca <job>.inp > <job>.out 2> <job>.err

- Only `.inp` and `.out` are kept. Inputs that `MORead` a `.gbw` name a file
  from the preceding step, which is not included, so they cannot be rerun from
  this folder alone.
- Geometries are QUESTDB's CC3/aug-cc-pVTZ structures.
- All jobs use 4 MPI processes.

| job | what | symmetry | terminated normally |
|---|---|---|---|
| formaldehyde_guess | SA-CASSCF(6,5)/cc-pVDZ, starting orbitals | off | yes |
| formaldehyde_nevpt2_np4 | SA-CASSCF(6,5) + SC-NEVPT2 / aug-cc-pVTZ / RIJCOSX / AutoAux | off | yes |
| ethylene_guess | SA-CASSCF(2,2)/cc-pVDZ, starting orbitals | off | yes |
| ethylene_nevpt2_np4 | SA-CASSCF(2,2) + SC-NEVPT2 / aug-cc-pVTZ, `orbstep TRAH` | off | yes |
| ethylene_nevpt2_np4_default_orbstep | same with ORCA's default orbital step | off | no: CASSCF convergence failure after 75 macro-iterations (ORCA exit code 0) |
| ketene_a_rhf | RHF/cc-pVDZ | on | yes |
| ketene_b_valence_dz | valence CASSCF(10,9)/cc-pVDZ | on | yes |
| ketene_c_valence_avtz_nosym | valence CASSCF(10,9)/aug-cc-pVTZ | off | yes |
| ketene_d_valence_avtz_sym | same, re-read with symmetry | on | yes |
| ketene_f_target | SA (1 A1 + 1 B1) CASSCF(10,10) + SC-NEVPT2, QUESTDB row 1B1(pi,3s) | on | yes |
| acetone_a_rhf | RHF/cc-pVDZ | on | yes |
| acetone_b_valence_dz | valence CASSCF(6,5)/cc-pVDZ | on | yes |
| acetone_c_valence_avtz_nosym | valence CASSCF(6,5)/aug-cc-pVTZ | off | yes |
| acetone_g_trah | SA (1 A1 + 2 B2) CASSCF(6,7) + SC-NEVPT2, `orbstep TRAH`, QUESTDB rows 1B2(n,3s)/(n,3pz) | on | yes |

Known results, for tests that check a parser against these files:

- ketene_f_target: SA-CASSCF 5.217 eV, SC-NEVPT2 6.069 eV for the B1 root
  (QUESTDB: 5.216 / 6.067).
- acetone_g_trah: SA-CASSCF B2 roots 6.622 and 9.350 eV; SC-NEVPT2 prints them
  in the other order (root 1 at 4.614 eV, root 0 at 6.914 eV). This is a
  different CASSCF solution from QUESTDB's (5.502 / 6.438 CASSCF, 6.697 / 7.800
  SC-NEVPT2) and must not be used as a reproduction of the table.
- formaldehyde_nevpt2_np4 and ethylene_nevpt2_np4 ran without symmetry, so
  their roots carry no irrep labels and are not assigned to QUESTDB states.
