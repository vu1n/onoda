# QUESTDB memory hill climb 1

Stopped at the required preflight, before the seed or any paid call. The
prescribed pytest command exited 1: **17 passed, 3 skipped, 101 setup errors
in 1.00 s**. The first setup error was `FileNotFoundError` creating
`/Users/vuln/code/onoda/.pinned/79d1f77/.pt/t`; its parent `.pt` directory was
absent. No sandbox workaround or test rerun was attempted. This is a setup
failure, not evidence that a sandbox isolation assertion failed.

## Outcome

- Seed score: **not measured**. Historical S1 counts supplied in the task:
  ketene `[4, 1, 2, 2]`, thioacetone `[4, 3, 4, 3]`, total **23/56**.
  No fresh seed exists, so no pooled seed mean or batch spread can be computed.
- Final incumbent: unchanged **memory-project@7**; score **not measured**.
- Accepted changes: none. Candidate batches, confirmations, and paid runs: **0**.
- Held-out: not run; no candidate was accepted.
- Actual OpenRouter spend for this task: **$0.00**.
- Stop reason: the task requires stopping if any preflight check fails.

## Provenance and preflight

Branch: `codex/memory-hillclimb-1`. Base:
`79d1f775c01987238783dcfd33b8809abb7dcd9b` (verified `origin/main` after
`git fetch origin`). The main checkout remained at `1329f72`, with its existing
untracked `.mcp.json` untouched. Its stale memory was not used as the seed.

The required checks ran in the existing detached base checkout at
`/Users/vuln/code/onoda/.pinned/79d1f77`, after `uv sync` and an identical
`pi/package-lock.json` check against the main checkout. A separate branch
checkout holds this report. No tracked code, tests, task inputs, grader,
reference data, or memory changed.

Exact preflight command, from that detached checkout:

```sh
ONODA_TEST_ORCA_BINARY=$HOME/code/onoda/orca-6.1.1/orca ONODA_TEST_MPI_HOME=$HOME/code/onoda/orca-openmpi uv run pytest tests/test_pi_sandbox_seatbelt.py tests/test_orca_sandbox.py tests/test_orca_broker.py tests/test_pi_sandbox.py --basetemp=$PWD/.pt/t
```

Minimal failure evidence:

```text
self = PosixPath('/Users/vuln/code/onoda/.pinned/79d1f77/.pt/t')
os.mkdir(self, mode)
FileNotFoundError: [Errno 2] No such file or directory:
  '/Users/vuln/code/onoda/.pinned/79d1f77/.pt/t'
17 passed, 3 skipped, 101 errors in 1.00s
```

The missing parent was a preparation omission in this attempt. A future
authorized attempt should create `.pt` in the short pinned checkout before
running the same command. This attempt does not claim the Mac checks passed
and does not launch evaluations on their basis.

## Batch ledger

No batches launched or scored; `ledger.jsonl` is intentionally empty. There
are no per-run scores, verdicts, audits, run stores, provider-error reruns,
spend records, or batch wall times to report. Zero paid calls does not imply
a successful false-success or audit check; those checks were not run.

## Final incumbent diff against @7

Empty: `experiments/questdb/memory/memory-project.md` is byte-for-byte
unchanged from the base. No version bump was made because no candidate was
created.

## Uptake notes

Not assessed: no accepted bullets and no new agent transcripts.

## Held-out results

Not run for either seed or incumbent; no candidate was accepted.

## Proposals for Vu

None from this attempt. The setup failure provides no new evidence for a
grader, normalizer, reference, or task-input change. Reference files were
not opened; the requested existing wrong-state dig was read as background.

## Review and verification limits

The branch contains only this failure report and the empty batch ledger.
The report records the unsuccessful preflight without altering its contract.
No chemistry finding or guidance improvement is claimed. MAE is unavailable
because no batch ran.
