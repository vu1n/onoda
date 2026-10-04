# Memory, harness tier (memory-harness@1)

Facts about this harness that hold for every molecule. They describe how the
tools behave, not chemistry. Each comes from onoda's own code or from an
earlier run's record, named in brackets.

- Your ORCA jobs run on the host, not in your shell. Your shell has no network
  and no ORCA. The only way to run ORCA is `orca_submit`. [A59, A65]
- `orca_output` with no job id lists every job of this run: its status, its
  `!` keyword lines and what the parser read from its output. Start there when
  you need an earlier result; there is nothing to search for. [A74]
- Each `orca_submit` reply is a summary, not the whole output. It is the same
  parser the grader uses: termination, symmetry and point group, CASSCF
  convergence, and every transition energy with its spin, irrep and the line
  number in the output. Read around that line with `orca_output` (job id plus
  `find`) or in `jobs/<job_id>/output.out` in your workspace. [A72, A74]
- ORCA exits normally on many failed calculations. A job counts only if its
  summary says `termination: normal` and, for CASSCF, `CASSCF: converged`.
  [first ORCA executions, 2026-10-02]
- Every `orca_submit` call spends one of this run's jobs, including a repeat of
  an identical input (a cache hit). Each reply says how many are left. [A59]
- One shell command is stopped after 10 minutes. A search of the whole disk
  (`find /`) once held a run for 76 minutes and found nothing, because job
  files are not on your disk outside `jobs/`. [A72, 8-molecule run 2026-10-04]
- A claim is graded from the one job you cite: that job must print the value in
  its SC-NEVPT2 transition energy table with the state's irrep. Energies
  subtracted across two jobs were all rejected in the first runs. [A71, A73]
