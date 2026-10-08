# Memory, project tier: QUESTDB excited states with ORCA 6.1.1 (memory-project@10)

Procedures and pitfalls from onoda's own ORCA 6.1.1 runs. They hold for this
project's molecules in general; none is a result for a particular target state,
and no energy here is a reference value. Each names the job or run it came
from in brackets (the `tests/fixtures/orca611-nevpt2` jobs ran on the host, the
others are agent runs).

## Use the converged CI expansion

- After confirming CASSCF convergence, assign character from the final
  `CAS-SCF STATES FOR BLOCK` sections, not from `INITIAL CI STATE CHECK`.
  Decode the final ground-state and excited-root occupation strings with the
  active-MO ordering and orbital composition of that same final wavefunction;
  do not combine early CI digits with a later orbital table. Orbital
  optimization can change the dominant occupations and orbital ordering, so
  redo the assignment after convergence. Record `(job, mult, irrep, root)` and
  pair it with that root's printed SC-NEVPT2 transition energy, preserving the
  root identifier if energy rows reorder. [codex-hc-2 S1-thioacetone-r3 j0007]

## Getting excitation energies printed in one job

- Put the ground state and the excited roots in one state-averaged CASSCF job
  and add `PTMethod SC_NEVPT2` in `%casscf`. ORCA then prints a
  `NEVPT2 TRANSITION ENERGIES` table for that job. [ketene_f_target]
- With `UseSym`, give one block per irrep, for example `mult 1,1`,
  `irrep 0,3`, `nroots 1,2` for one A1 root plus two roots of the fourth irrep.
  Irrep numbers follow ORCA's order for the point group (C2v: A1=0, A2=1,
  B1=2, B2=3). Each state in the table then carries its irrep. [acetone_g_trah]
- A single-root job prints no transition table, so it cannot be cited for an
  excitation energy. [acetaldehyde runs, 2026-10-04]
- Every aug-cc-pVTZ CASSCF job here used `trafostep ri` with RIJCOSX. [fixtures]
- `trafostep ri` needs `RIJCOSX AutoAux` on the `!` line: without them ORCA
  stops at once ("Trafostep RI is set without a valid RI basis"). Drop the two
  together, or let `orca_input` build the input (its `ri` option sets both).
  Three hand-written inputs in two gate runs kept the line without RIJCOSX.
  [gate-4, gate-4 rerun]

## Inputs that ran in ORCA 6.1.1

These are the shapes of inputs that terminated normally here, with the active
space left as `NEL`/`NORB` and the per-irrep lists as `...` to fill in. Keep the
keyword spelling exactly. Each step reads the previous step's orbitals: attach
that job's `job.gbw` in `orca_submit` (it arrives as `<job_id>.gbw`, for example
`j0001.gbw`) and name it in `%moinp`. Give the geometry inline; the job
directory holds only the input and its attachments. [ketene a-f, acetone a-g,
orca_submit]

The full route that ran for ketene had five steps: RHF/cc-pVDZ, valence CASSCF/cc-pVDZ,
the same CASSCF in aug-cc-pVTZ without `UseSym`, that one re-read with
`UseSym`, then the state-averaged target job. The three shapes below cover
every step: drop `UseSym` for the step without symmetry. See "Keeping the
intended orbitals active" for what re-reading with `UseSym` does to the
orbital order.

Step 1, RHF with symmetry, small basis:

```
! RHF cc-pVDZ TightSCF UseSym
%maxcore 2000
%pal nprocs 4 end
* xyz 0 1
(the structure's atom lines)
*
```

Valence CASSCF steps, one root each (so they print no table):

```
! aug-cc-pVTZ RIJCOSX AutoAux TightSCF UseSym MORead
%moinp "j0001.gbw"
%maxcore 2000
%pal nprocs 4 end
%casscf
  nel NEL
  norb NORB
  mult 1
  irrep 0
  nroots 1
  trafostep ri
end
* xyz 0 1
(the structure's atom lines)
*
```

Target job, state-averaged CASSCF with SC-NEVPT2, one block per irrep (comma
lists of equal length: `mult`, `irrep`, `nroots`, and `bweight` repeating
`nroots`):

```
! aug-cc-pVTZ RIJCOSX AutoAux TightSCF UseSym MORead
%moinp "j0002.gbw"
%maxcore 2000
%pal nprocs 4 end
%casscf
  nel NEL
  norb NORB
  mult 1,1
  irrep 0,...
  nroots 1,...
  bweight = 1,...
  trafostep ri
  maxiter 150
  PTMethod SC_NEVPT2
end
* xyz 0 1
(the structure's atom lines)
*
```

- Every `%` block ends with its own `end`. `%pal nprocs 4 end` fits on one
  line. [fixtures]
- Put the ground state first (`mult 1`, `irrep 0`, `nroots 1` leading each
  list). The parser rejects a table whose lowest root is not a singlet ground
  state. [orca_parser]
- Weight every root equally with one line in `%casscf`: `bweight = ` the
  `nroots` list, in the same order (e.g. `nroots 1,2,1` takes
  `bweight = 1,2,1`). Without it ORCA gives each block an equal share and
  splits it among that block's roots, so blocks of different sizes leave the
  roots unequal, while QUESTDB's references weight every root equally.
  Per-root `weights[0] = ...` lines do not fix that, and hand-written weights
  of other shapes failed to parse eight times in one run ("Last token: 0.250",
  "[", ","). `orca_input` writes the `bweight` line itself. The host check
  after each CASSCF job says whether every root got the same weight.
  [gate-1 S0-thioacetone-r1; host test, ORCA 6.1.1]

## Getting CASSCF to converge

- Start with ORCA's default orbital step. On thioacetone, a one-root
  CASSCF(6,4) in aug-cc-pVTZ converged in 93 s with it; the same input with
  `orbstep TRAH` took 26 min and stopped at a solution 0.12 Eh higher. TRAH
  also landed acetone on a CASSCF solution different from QUESTDB's.
  [gate-2 S0-thioacetone-r1 j0006, host rerun; acetone_g_trah]
- When the default step fails, rerun the same input with `orbstep TRAH`
  (orca_input `trah`) from the same starting orbitals. Read the job reply's
  summary for this: a CASSCF that did not converge usually still ends
  normally. In aug-cc-pVTZ the default step failed to converge ethylene's
  small CASSCF after 75 macro-iterations, and TRAH converged it; 8 of 88 jobs
  in one gate run did not converge with the default step.
  [ethylene_nevpt2_np4; gate-3]
- Build starting orbitals in steps and read them with `MORead`, attaching the
  earlier job's `.gbw`: an RHF or valence CASSCF in cc-pVDZ, then the same
  active space in aug-cc-pVTZ, then the state-averaged job. [ketene a-f, acetone a-g]

## Keeping the intended orbitals active

- Reading orbitals from a job without symmetry into a job with `UseSym`
  re-sorts them by energy, which can move intended active orbitals out of the
  active space. `%scf rotate {i,j,90} end end` swaps orbitals i and j back.
  Compare the host check "active orbitals per irrep" with what you intended.
  [acetone_g_trah]
- A target with Rydberg character (3s, 3p) needed one more active orbital than
  the valence active space. [ketene_f_target]
- Each (mult, irrep) block needs active orbitals that can make that symmetry.
  In one job the active orbitals read from an earlier job were three B1 and
  two B2 with no A1, so a B1 block had no configurations; ORCA stopped at once
  with "NORB==0 is invalid" and still exited 0. Check the host's "active
  orbitals per irrep" line before asking for a block, and bring in the
  orbital the state needs (for a 3s Rydberg state, the 3s). Turning off `UseSym` avoids the error only by dropping the
  irreps, and the table then has no symmetry labels to grade.
  [gate-2 S1-ketene-r1 j0012, host rerun]
- ORCA has no per-irrep count of active orbitals inside `%casscf` (a nested
  `nact` block failed: "Unknown identifier in CASSCF block"). Choose the active
  orbitals with `MORead` and `rotate`. [gate-2 S1-ketene-r1 j0008, j0014]

## One active space per group of targets

- In gates 3 to 5 every run used one active space for all its targets: of
  101 SC-NEVPT2 inputs, 32 put every target (spin, irrep) in one job and 57
  one job per spin, and no run changed `nel`/`norb` or the active orbitals
  per irrep between its cited jobs. Targets that excite into different orbitals
  (valence pi*, 3s, 3p) need different active spaces. Give each such group
  its own state-averaged job whose active space holds the orbitals that
  group excites into, brought in with `MORead` and `rotate`.
  [gates 3-5, inputs]
- Check which orbitals are active, not how many per irrep. With "A1 1, B1 2,
  B2 1" active, a job asked for two singlet B2 roots and reported them as
  n,3s and n,3p; one b2 and one a1 orbital allow a single b2-to-a1
  excitation, so the second B2 root came out 4.7 eV higher and is not an
  n,3p state.
  Two ketene jobs both had "A1 1, B1 3, B2 2" active and put the triplet A1
  2 eV apart: the count per irrep does not say which orbitals they are. Read
  your own description of each active orbital before asking for a block.
  [gate-3 S1-thioacetone-r2 j0010; gate-5 S1-ketene-r2 j0012, gate-4
  S0-ketene-r1 j0016]
- The targets list counts excited states. The singlet block that holds the
  ground state needs one root more than its count: two excited singlet A1
  states need `nroots 3` in the singlet A1 block. Triplet blocks do not hold
  the ground state, so they need only their count. Asking for 2 there gives only one
  excited A1 state; 9 of 13 thioacetone runs did this.
  [gate-5 S1-thioacetone-r2 j0010]
- Which states share the average moves the energies. Two jobs with the same
  CAS(8,6) read from the same orbitals put ketene's 1B1 2.5 eV apart: one
  averaged the singlets only, the other added three triplet blocks. Average
  only the states one target group needs, singlets and triplets in separate
  jobs. A triplet job still leads with the singlet ground state (`mult 1`,
  `irrep 0`, `nroots 1` first, as above), or it prints no excitation
  energies from S0; the separate triplet job that ran had
  `mult 1,3,3,3`. [gate-4-rerun S0-ketene-r1 j0005, S0-ketene-r2 j0005]

## Reporting

- Write a state's character as two orbitals separated by a comma (`n,pi*`,
  `pi,3s`). Give each reported state its own character; two states with the
  same label cannot both be matched. [ketene live run, 2026-10-03]
