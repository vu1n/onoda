# An orca_submit reply shows the parser's summary of the output, not the whole output

**Status at migration:** draft. This migration does not change that status.


<a id="parser-summary-reply"></a>
## Show the agent the parser's summary of each job, and let it list its jobs

Every broker reply carries `summary`: the deterministic parser's reading of the job's output
(termination, final single-point energy, symmetry and point group, CASSCF convergence, every
transition energy by method with spin, irrep or that none was printed, and its line number in the
output, and the parser's problems). The `orca_submit` reply shows those lines, the advisory checks,
the output's line count, where to read the rest (`orca_output`, the workspace copy) and the
output's last 25 lines. `orca_output` with no job id lists every job of the run with its `!`
keyword lines and summary.

**Why.** In the first 8-molecule run (2026-10-04) each reply pasted 200 to 600 KB, the context
reached about 375k tokens after two dozen jobs, and the agent lost track of its own results and
searched the disk for them.

### Invariant
- The summary is built only from `parse_orca_output` and lines ORCA printed; it reads no reference
  value (every method's states are listed, since the graded method comes from the references).
- The summary is advisory: the journal records it, replays recompute it from the same bytes, and
  the grader never reads it.
- The spool and the store keep every byte of every output; only what the reply shows is shorter.
- Both arms get the same reply.

### Consequences
- Runs before and after this change are not comparable.
- ObservationPack (S2a, S3) fires only on whole-output `orca_output` reads, not on `orca_submit`
  replies.
