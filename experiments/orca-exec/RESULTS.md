# First real ORCA execution (2026-10-03, Vu's Mac, arm64)

Differences are reported, not judged: the acquired
logs are ORCA 5.0.0 / 6.0.0 / 6.0.1 and these runs are ORCA 6.1.1, and
`orca-reexecution@1` has no tolerance (A6/A30), so every reference comparison
is `unresolved` by design.

## Setup

- ORCA 6.1.1 macOS arm64 (`orca_6_1_1_macosx_arm64_openmpi411.tar.bz2`, from
  the ORCA Forum) extracted to `orca-6.1.1/`; quarantine cleared. Binary
  sha256 `ae760c7b81d474cdf4d9f249b50f7c9cde40b402b1d85ef4213a817bdee1400a`.
- OpenMPI: five of the six pinned inputs ask for `nprocs 4` and the Mac had no
  MPI. Built OpenMPI 4.1.8 from source (C only) into `orca-openmpi/`; nothing
  installed system-wide. ORCA's `*_mpi` binaries reference `libmpi.40.dylib`
  by bare name and macOS strips `DYLD_*` through `/bin/sh`, so
  `orca-openmpi/etc/openmpi-mca-params.conf` carries
  `mca_base_env_list = DYLD_LIBRARY_PATH=<repo>/orca-openmpi/lib`.
  ORCA was built against 4.1.1; 4.1.8 is the same `libmpi.40` ABI.
- `.git/info/exclude`: `orca-6.1.1/`, `orca-jobs/`, `orca-store*/`,
  `orca-eval*/`, `orca-openmpi/`.
- Command per case (main c69b8fd; the STEOM rerun used branch `orca-exec`):

      PATH=$PWD/orca-openmpi/bin:$PATH uv run onoda case ingest <case> \
        --run-id exec-<case> --store ./orca-store --execute \
        --enable-live-execution --orca-binary $PWD/orca-6.1.1/orca \
        --work-root ./orca-jobs --max-wall-seconds 1800
      uv run onoda case evaluate exec-<case> --store ./orca-store \
        --evaluator-store ./orca-eval --reference-case <case>

## Summary

Final energy diff is run minus acquired log, in Eh. State diffs are in au.

| case | job | wall (s) | termination | final energy diff | states (run/log) | largest state diff | evaluate |
|---|---|---|---|---|---|---|---|
| orca6-water-mp2 | job-00002 | 1.9 | normal | -0.000000018263 | 0/0 | n/a | exec pass, parse pass, states pass, reference unresolved |
| orca6-dvb-tddft | job-00001 | 8.7 | normal | -0.000011548282 | 10/10 | 0.000040 (m1.r4) | exec pass, parse pass, states pass, reference unresolved |
| orca5-dvb-tddft | job-00003 | 5.5 | normal | -0.000003985701 | 10/10 | 0.000014 (m3.r5) | exec pass, parse pass, states pass, reference unresolved |
| orca6-dvb-adc2 | job-00004 | 6.6 | normal | -0.000000000012 | 2/2 | 0.000000 | exec pass, parse pass, states unresolved (A28), reference unresolved |
| orca6-dvb-eom-ccsd | job-00005 | 52.4 | normal | 0.201083684615 | 2/2 | 0.000000 | exec pass, parse pass, states unresolved (A28), reference unresolved |
| orca6-dvb-steom-ccsd | job-00006 | 61.6 | normal | 0.172385713237 | 0/2 (parser error) | n/a | exec pass, parse FAIL, states unresolved, reference unresolved |
| orca6-dvb-steom-ccsd, rerun with fixed parser (`exec2-`) | job-00007 | 49.6 | normal | 0.172385713237 | 2/2 | 0.000018 (m1.r2) | exec pass, parse pass, states pass, reference unresolved |

All seven jobs: scheduler `completed`, return code 0, well inside the 1800 s limit.

## Findings

1. **STEOM state lines changed in 6.1.1.** They now end in `<S**2> = value`
   (`IROOT=  1:  0.172386 au     4.691 eV   37834.3 cm**-1 <S**2> =   0.000000`).
   `orca-parser@1` refused them ("state line does not have 3 value/unit
   pairs"), so run `exec-orca6-dvb-steom-ccsd` is finalized with
   `excited_states_status: error`. That record is immutable and stays as is.
   Fix on branch `orca-exec` (74d16b6): validate and ignore the
   suffix, as on TD-DFT `STATE` lines; A56 added; tests added. Rerun as
   `exec2-orca6-dvb-steom-ccsd` parses 2 states. The fix was then released
   as `orca-parser@2`. The rerun happened before that bump, so its stored
   observation says `orca-parser@1` although the fixed code produced it;
   that local record is not a clean `@1` or `@2` example.
2. **`FINAL SINGLE POINT ENERGY` means something different for EOM/STEOM in
   6.1.1.** The run prints ground state plus the first root's excitation
   energy; the 6.0.1 logs print the ground-state total. The ground-state
   `E(TOT)` lines agree: EOM -380.500397408 in both; STEOM -380.500397776
   (run) vs -380.500397734 (log). So the 0.201 / 0.172 Eh "final energy"
   differences are that reporting change, not a different ground state.
3. **ORCA exits 0 on error termination.** Seen while setting up MPI (a
   parallel run whose ranks could not load libmpi printed "ORCA finished by
   error termination in Startup" and returned 0). The scheduler would record
   such a job as `completed`; only the parser's termination check catches it.
   Not changed.
4. **The runner passes only `PATH` to ORCA**, which is enough once OpenMPI's
   `bin` is on `PATH` and the MCA config above is in place. No runner change
   was needed.
5. EOM-CCSD took 52 s here; the acquired log's own `TOTAL RUN TIME` is 42 min.
6. `uv run pytest` on this Mac: 14 tests fail or error on both `main` and
   `orca-exec` (`test_rlm_kernel.py` AF_UNIX path too long, two Pi tests that
   expect Linux). The ORCA parser/local/case suites pass.

## Where things are

- ORCA outputs: `orca-jobs/job-0000N/job.out` (+ `job.inp`, `job.err`),
  index in `orca-jobs/scheduler.json`. `orca-jobs/smoke/` holds the H2
  smoke tests.
- Agent store `orca-store/`, evaluator store `orca-eval/`.
- Per-case CLI output: `experiments/orca-exec/<case>.{ingest,evaluate}.json`
  (`ingest2`/`evaluate2` are the STEOM rerun; the first two cases' CLI output
  was not saved to files, their records are in the stores).
- `experiments/orca-exec/compare.py` regenerates the tables below from
  `orca-jobs/` with the current parser, so job-00006 shows the states the
  fixed parser reads from it, not what was recorded for that run.

# Per-case detail (compare.py output)

## orca6-dvb-tddft (job-00001)

- scheduler: status=completed returncode=0 reason=None wall=8.7 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (8 cycles), problems=0
- reference: ORCA 6.0.0, termination=normal, scf=converged (8 cycles), problems=0
- final energy (Eh): candidate -381.858430594571, reference -381.858419046289, diff -0.000011548282
- excited states: candidate 10 (present), reference 10 (present)

| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |
|---|---|---|---|---|---|
| m1.r1 | 1/1 | Bu/Bu | 0.196679 | 0.196688 | -0.000009 |
| m1.r2 | 2/2 | Bu/Bu | 0.210637 | 0.210622 | 0.000015 |
| m1.r3 | 3/3 | Ag/Ag | 0.228781 | 0.228756 | 0.000025 |
| m1.r4 | 4/4 | Bu/Bu | 0.261652 | 0.261612 | 0.000040 |
| m1.r5 | 5/5 | Ag/Ag | 0.272540 | 0.272503 | 0.000037 |
| m3.r1 | 6/6 | Bu/Bu | 0.115007 | 0.114999 | 0.000008 |
| m3.r2 | 7/7 | Ag/Ag | 0.155771 | 0.155762 | 0.000009 |
| m3.r3 | 8/8 | Bu/Bu | 0.172965 | 0.172971 | -0.000006 |
| m3.r4 | 9/9 | Bu/Bu | 0.182144 | 0.182145 | -0.000001 |
| m3.r5 | 10/10 | Bu/Bu | 0.194717 | 0.194703 | 0.000014 |

## orca6-water-mp2 (job-00002)

- scheduler: status=completed returncode=0 reason=None wall=1.9 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (8 cycles), problems=0
- reference: ORCA 6.0.1, termination=normal, scf=converged (8 cycles), problems=0
- final energy (Eh): candidate -74.999374616370, reference -74.999374598107, diff -1.8263E-8
- excited states: candidate 0 (absent), reference 0 (absent)

## orca5-dvb-tddft (job-00003)

- scheduler: status=completed returncode=0 reason=None wall=5.5 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (8 cycles), problems=0
- reference: ORCA 5.0.0, termination=normal, scf=converged (9 cycles), problems=0
- final energy (Eh): candidate -381.858430594572, reference -381.858426608871, diff -0.000003985701
- excited states: candidate 10 (present), reference 10 (present)

| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |
|---|---|---|---|---|---|
| m1.r1 | 1/1 | Bu/None | 0.196679 | 0.196682 | -0.000003 |
| m1.r2 | 2/2 | Bu/None | 0.210637 | 0.210641 | -0.000004 |
| m1.r3 | 3/3 | Ag/None | 0.228781 | 0.228790 | -0.000009 |
| m1.r4 | 4/4 | Bu/None | 0.261652 | 0.261652 | 0.000000 |
| m1.r5 | 5/5 | Ag/None | 0.272540 | 0.272542 | -0.000002 |
| m3.r1 | 6/1 | Bu/None | 0.115007 | 0.115007 | 0.000000 |
| m3.r2 | 7/2 | Ag/None | 0.155771 | 0.155775 | -0.000004 |
| m3.r3 | 8/3 | Bu/None | 0.172965 | 0.172978 | -0.000013 |
| m3.r4 | 9/4 | Bu/None | 0.182144 | 0.182144 | 0.000000 |
| m3.r5 | 10/5 | Bu/None | 0.194717 | 0.194703 | 0.000014 |

## orca6-dvb-adc2 (job-00004)

- scheduler: status=completed returncode=0 reason=None wall=6.6 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (11 cycles), problems=0
- reference: ORCA 6.0.1, termination=normal, scf=converged (11 cycles), problems=0
- final energy (Eh): candidate -380.165181816972, reference -380.165181816960, diff -1.2E-11
- excited states: candidate 2 (present), reference 2 (present)

| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |
|---|---|---|---|---|---|
| mNone.r1 | 1/1 | None/None | 0.204110 | 0.204110 | 0.000000 |
| mNone.r2 | 2/2 | None/None | 0.256694 | 0.256694 | 0.000000 |

## orca6-dvb-eom-ccsd (job-00005)

- scheduler: status=completed returncode=0 reason=None wall=52.4 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (11 cycles), problems=0
- reference: ORCA 6.0.1, termination=normal, scf=converged (11 cycles), problems=0
- final energy (Eh): candidate -380.299313723501, reference -380.500397408116, diff 0.201083684615
- excited states: candidate 2 (present), reference 2 (present)

| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |
|---|---|---|---|---|---|
| mNone.r1 | 1/1 | None/None | 0.201084 | 0.201084 | 0.000000 |
| mNone.r2 | 2/2 | None/None | 0.280220 | 0.280220 | 0.000000 |

## orca6-dvb-steom-ccsd (job-00006)

- scheduler: status=completed returncode=0 reason=None wall=61.6 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (11 cycles), problems=0
- reference: ORCA 6.0.1, termination=normal, scf=converged (11 cycles), problems=0
- final energy (Eh): candidate -380.328012020386, reference -380.500397733623, diff 0.172385713237
- excited states: candidate 2 (present), reference 2 (present)

| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |
|---|---|---|---|---|---|
| m1.r1 | 1/1 | None/None | 0.172386 | 0.172381 | 0.000005 |
| m1.r2 | 2/2 | None/None | 0.281108 | 0.281090 | 0.000018 |

## orca6-dvb-steom-ccsd (job-00007)

- scheduler: status=completed returncode=0 reason=None wall=49.6 s
- candidate: ORCA 6.1.1, termination=normal, scf=converged (11 cycles), problems=0
- reference: ORCA 6.0.1, termination=normal, scf=converged (11 cycles), problems=0
- final energy (Eh): candidate -380.328012020386, reference -380.500397733623, diff 0.172385713237
- excited states: candidate 2 (present), reference 2 (present)

| pair | root (cand/ref) | sym (cand/ref) | cand au | ref au | diff au |
|---|---|---|---|---|---|
| m1.r1 | 1/1 | None/None | 0.172386 | 0.172381 | 0.000005 |
| m1.r2 | 2/2 | None/None | 0.281108 | 0.281090 | 0.000018 |
