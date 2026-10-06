# Memory, project tier: QUESTDB excited states with ORCA 6.1.1 (memory-project@2)

Procedures and pitfalls from onoda's own ORCA 6.1.1 runs. They hold for this
project's molecules in general; none is a result for a particular target state,
and no energy here is a reference value. Each names the job or run it came
from in brackets (the `tests/fixtures/orca611-nevpt2` jobs ran on the host, the
others are agent runs).

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
lists of equal length: `mult`, `irrep`, `nroots`):

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
  trafostep ri
  orbstep TRAH
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

## Getting CASSCF to converge

- In aug-cc-pVTZ, ORCA's default orbital step failed to converge a small
  CASSCF after 75 macro-iterations, and the job still ended normally.
  `orbstep TRAH` in `%casscf` converged it. [ethylene_nevpt2_np4]
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

## Reporting

- Write a state's character as two orbitals separated by a comma (`n,pi*`,
  `pi,3s`). Give each reported state its own character; two states with the
  same label cannot both be matched. [ketene live run, 2026-10-03]
