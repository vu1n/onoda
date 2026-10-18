# QUESTDB Opus memory hill climb 1

**Stopped at the mandatory audit gate during candidate 1 (#46).** The final
incumbent is unchanged `memory-project@7`, **28.5/46 accurate slots per batch**
(mean of seed scores 27 and 30). No guidance change was written or accepted.
Total billed-key spend: **$25.156561200** of $150. No further paid runs are queued.

Exploratory development work, not a chemistry result.

Candidate 1's diazomethane r1 settled at 5/7 accurate with zero false-success
claims, but the audit credited **five `other_job` mutations**. The launcher
stopped immediately on that audit result and terminated the other three runs.
The incomplete batch cannot establish an accuracy improvement or regression.
This is not evidence that #46 caused a grader vulnerability; it is a failure of
this search's required zero-false-accept gate.

## Batch ledger

Counts are slots both identified and within 0.3 eV, grader v3. Each complete
batch has 46 slots. Entries in molecule columns are r1, r2. `stopped` means no
settled result; it is not scored as zero. Costs are billed-key counter windows,
not Pi estimates. Wall time includes grading/reporting and any reporting recovery.

| Batch | Commit | Memory / change | Ketene | Thioacetone | Acetaldehyde | Diazomethane | Score | Decision and why | False successes | Audit false accepts | Billed | Wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Invalid seed attempt | ce0e974 | @7; shared scratch | — | — | not started | not started | invalid | Discarded before grading: scheduler race; four runs interrupted | ungraded | ungraded | $0.892615 | 8m39s |
| Seed 1 | ce0e974 | @7; unchanged | 5,5 | 3,3 | 2,2 | 5,2 | 27/46 | Second seed required: diazomethane spread 3 | 0 | 0 | $9.498921 | 56m52s |
| Seed 2 | ce0e974 | @7; unchanged | 5,5 | 3,3 | 2,2 | 5,5 | 30/46 | Incumbent mean 28.5/46 | 0 | 0 | $8.026745 | 48m24s |
| Candidate 1 | d9f94f5 | @7; #46 converged CI + active-MO summaries | 5,5 | stopped,stopped | 2,2 | 5,stopped | Incomplete: 19/32 observed slots | Rejected at mandatory audit gate; no confirmation | 0 in five settled runs | 5 (`other_job`) | $6.414951 | 32m03s |

Full code pins:
- Seed/current-main base: `ce0e9744e0b7f6b483724d51c6a0fc8ffa6adffe`.
- Candidate 1: `d9f94f511afb5d51dc541b365170179ed43ea509`, PR #46 implementation
  `4e8469d5d1112ddec8f13960f055b9c340c14d22` rebased onto that main.

The original launcher `candidate-1/score.json` subtotal is 14: it only records
runs that passed their individual audits. The reconciliation entry in
`ledger.jsonl` also includes the settled 5/7 run that triggered the stop, giving
19/32 observed slots. Neither subtotal is a full batch score. Three interrupted
runs have no claims or grades and cannot satisfy the acceptance rule.

## Frozen protocol and calibration

Scoring: ketene, thioacetone, acetaldehyde, diazomethane; two repeats each, S1 only.
Held out: formamide, acetone, cyclopropenone, methylenecyclopropene. The split was
written to `protocol.json` before any spend and never changed.

Fixed settings: `ENABLE_LIVE_EXECUTION=1 PROVIDER=openrouter
MODEL=anthropic/claude-opus-5.5:high CREDENTIAL_ENV=OPENROUTER_API_KEY
PI_MODELS=experiments/questdb/pi-models-claude-5.5.json MAX_REQUESTS=200
MAX_COST_USD=100 TIMEOUT=7200 REPORT_REMINDER=1 PARALLEL=4`.
The memory, model, task inputs, target lists, grader, normalizer, references,
ladder, parser and run caps were unchanged. #46 only changed advisory job summaries.

Acceptance required incumbent +3, no molecule losing more than one summed slot,
every run settled with a claim, zero false successes and zero audit false accepts.
Confirmation would require incumbent mean +2 and the same safety/claim guards.
After calibration, screening would require at least 32/46 and confirmation at
least 31/46. Molecule incumbent means were ketene 10, thioacetone 6,
acetaldehyde 4, diazomethane 8.5.

**Opus noise is larger than expected on diazomethane.** It scored 5,2,5,5 across
four seed repeats. Every other molecule repeated identically. Seed-1 diazomethane
r2 identified five slots but only two were within tolerance; its own notes flag
poorly optimized diffuse orbitals and discarded unphysical solutions. No claim
was paired to a reference by energy, and no labels were changed to fix the score.

## Step 0: diagnosis before edits or spend


Source: local `claude-baseline-1/opus/out/runs/<run>` agent stores and evaluator-v3
claim verdicts. Agent claims, spooled provider responses, original ORCA inputs embedded
in outputs, converged CI blocks and NEVPT2 transition tables were inspected. `diagnosis/`
contains local extracted agent-visible evidence. No `references.json` was opened.

Leakage note: the requested older wrong-state report and raw evaluator records
exposed reference labels embedded in prose/keys. Evaluation extraction was narrowed
to claim verdicts and numeric aggregates. No guidance candidate was authored, and
no exposed reference label was encoded in guidance. Agent
transcripts themselves also recall supposed literature energies before calculating; those
are model assertions, not verified reference evidence and not candidate instructions.

All 24 missed repeat-slots had a supported printed claim. Thus there is no demonstrated
uncomputed-state slot here. Twenty fail character identity; four are identified but outside
tolerance. A supported value alone does not prove the physical target was computed.
Without reference characters or values, ambiguity between a wrong assignment and a
reference/normalizer issue cannot be resolved by relabelling or nearest-energy matching.

| Run | Missed slot (agent claim, not reference label) | Job / root | Classification | Evidence and limit |
|---|---|---|---|---|
| S1-ketene-r1 | claim.03: 1A2 (pi,3p) | j0015 / 1 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r1 | claim.05: 3A1 (pi,pi*) | j0016 / 0 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r2 | claim.03: 1A2 (pi,3p) | j0008 / 1 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r2 | claim.05: 3A1 (pi,pi*) | j0009 / 0 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r3 | claim.03: 1A2 (pi,3p) | j0011 / 1 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r3 | claim.05: 3A1 (pi,pi*) | j0012 / 0 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r4 | claim.03: 1A2 (pi,3p) | j0008 / 1 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-ketene-r4 | claim.05: 3A1 (pi,pi*) | j0010 / 0 | Possible grader/normalizer/reference problem — Proposals for Vu | Supported; unidentified reason character. Converged CI and active-orbital analysis informed the agent label; same failure in all repeats. No reference-based rename proposed. |
| S1-thioacetone-r1 | claim.02: 1B2 (n,4s) | j0009 / 0 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r1 | claim.03: 1A1 (pi,pi*) | j0009 / 2 | Claimed, energy outside 0.3 eV | Identified; absolute error 0.403 eV. SC-NEVPT2/aug-cc-pVTZ in every repeat. Active-space/state-average/integral treatment is a hypothesis, not an established cause. |
| S1-thioacetone-r1 | claim.04: 1B2 (n,4pz) | j0009 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r1 | claim.05: 1A1 (n,4py) | j0009 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r2 | claim.02: 1B2 (n,4s) | j0008 / 0 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r2 | claim.03: 1A1 (pi,pi*) | j0008 / 2 | Claimed, energy outside 0.3 eV | Identified; absolute error 0.403 eV. SC-NEVPT2/aug-cc-pVTZ in every repeat. Active-space/state-average/integral treatment is a hypothesis, not an established cause. |
| S1-thioacetone-r2 | claim.04: 1B2 (n,4p) | j0008 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r2 | claim.05: 1A1 (n,4p) | j0008 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r3 | claim.04: 1A1 (pi,pi*) | j0011 / 1 | Claimed, energy outside 0.3 eV | Identified; absolute error 0.489 eV. SC-NEVPT2/aug-cc-pVTZ in every repeat. Active-space/state-average/integral treatment is a hypothesis, not an established cause. |
| S1-thioacetone-r3 | claim.05: 1B2 (n,4s) | j0010 / 0 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r3 | claim.06: 1B2 (n,4p) | j0010 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r3 | claim.07: 1A1 (n,4p) | j0010 / 2 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r4 | claim.02: 1B2 (n,4s) | j0011 / 0 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r4 | claim.03: 1A1 (pi,pi*) | j0011 / 2 | Claimed, energy outside 0.3 eV | Identified; absolute error 0.403 eV. SC-NEVPT2/aug-cc-pVTZ in every repeat. Active-space/state-average/integral treatment is a hypothesis, not an established cause. |
| S1-thioacetone-r4 | claim.04: 1B2 (n,4p) | j0011 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |
| S1-thioacetone-r4 | claim.05: 1A1 (n,4p) | j0011 / 1 | Computed but named/assigned differently; possible reference convention issue | Supported; unidentified reason character. Agent assigns a diffuse acceptor from CI/orbital populations, but principal-shell naming is not independently established. Proposals for Vu; no forced relabelling. |

The thioacetone r1/r2/r4 singlet jobs use CAS(4,6) with six equally weighted singlet
roots and exact integrals after failed RI preparations. Their printed NEVPT2 table
reorders the two excited A1 roots; the agent explicitly follows the CASSCF root ids.
The r3 run separates valence singlets (j0011, CAS(4,3)) and diffuse singlets (j0010,
CAS(4,6)), using RIJCOSX/TRAH, and still misses the identified valence slot by 0.489 eV.
This does not support a simple claim that switching off RI or separating groups fixes it.
All cited jobs already request the needed spin/irrep/root counts. Ketene r1/r2/r4
use CAS(6,7), r3 CAS(10,9), with the same two character failures and five accurate slots.

Guidance-fixable hypotheses: assign from the converged CI section with its matching
orbital order (#46, candidate 1); verify diffuse-orbital identity instead of importing
remembered labels; diagnose an unstable active space/state average using the agent's
own controlled calculations. Candidate 1 is evaluated before any memory edit.

## Stop evidence and interpretation

Candidate `S1-diazomethane-r1`: seven reported states, five identified and accurate,
zero false-success claims. Audit: five accepted `other_job` mutations out of 98
such variants. All other failure-counting variants accepted zero. `label_swap`
accepted 2 of 4 and is measured only, per the existing audit contract.
The evidence is local `candidate-1/S1-diazomethane-r1.audit.json`, its evaluator
record, and the immutable agent store. No audit or grader code was changed.

The launcher created `STOP`, terminated only its owned run process trees, wrote
the partial batch report/ledger, then exited. The final check found its PID gone
and no direct children. The three interrupted run JSON files are empty, not results.
No confirmation, memory candidate or held-out batch was launched after the stop.

## Final diff against @7

**Empty.** `experiments/questdb/memory/memory-project.md` is byte-for-byte the
base @7 file. The unaccepted #46 implementation is removed from the final branch
diff; its original candidate commit and detached `.pinned/oh1` worktree preserve
what was evaluated. Final changes are the report, ledger, frozen plans and launcher.

## Uptake

No accepted bullets, so accepted-bullet uptake is not applicable. No memory
candidate was written: the mandatory stop occurred during the required #46 test.
The stopped comparison supplies no causal estimate of #46's effect on accuracy.

## Held-out results

Not run. No candidate was accepted, so the held-out condition was never reached.
The held-out molecules were not used to revise guidance.

## Proposals for Vu

- Investigate the `other_job` audit accepts from candidate-1 diazomethane r1.
  Determine whether different cited jobs printed equivalent supported states or
  whether the audit reveals a substantive citation-boundary defect. Do not change
  grading based on this report alone; this task made no such change.
- #46 failed this experiment's required gate and was not promoted. This is a
  reason to close #46 as an unvalidated optimization, or defer it pending a
  separate audit-contract investigation. The incomplete batch does not show that
  the observation summaries reduced accuracy or caused the false accepts.
- Investigate supported-but-unidentified character conventions: two ketene and
  three thioacetone slots per prior Opus repeat, plus two diazomethane claims in
  seed-1 r1. These remain reference/normalizer questions; no forced rename was tried.
- Investigate the stock parallel runner's shared `ORCA_WORK` default. Its local
  scheduler has no cross-process lock. The first attempt here lost an initial
  allocation and corrupted an orbital file. Check earlier parallel-run provenance
  separately; this report does not declare earlier results invalid.
- Prior Opus transcripts recall supposed literature/QUESTDB values before computing.
  Consider how to measure model-prior contamination separately. Those assertions
  were not used as verified reference evidence or encoded in guidance.

## Launcher, recovery and validation

All live runs used pinned detached worktrees, under detached `caffeinate -i`, with
fresh short `WORK` directories. Seed 1: `/tmp/ohs3`; seed 2: `/tmp/ohs2`;
candidate 1: `/tmp/ohc1`. Each run gets its own `ORCA_WORK=<WORK>/j/<index>`.
Pi lockfiles matched the installed runtime before copying node_modules.

The invalid initial `/tmp/ohs1` attempt shared one scheduler directory among four
runs. Four initial requests produced only three initial job directories; ketene
r2's input digest had no separate directory. `job-00003/job.out` reported orbital
file corruption. The owned 50-process tree was stopped before any grade. The
launcher correction isolated scratch per run; no scheduler or sandbox code changed.
The invalid attempt remains charged to the total and preserved locally.

Seed-1 pooled reporting initially failed because `vte report --arm S1` is invalid:
the CLI takes exactly two arms or none. The launcher now infers the observed arm.
The eight saved graded/audited runs were reported without another paid run. The
reporting helper supports this offline recovery. Subsequent batches reported
without the operator delay; seed 2 and candidate 1 proceeded while this session
was not interacting with them.

The first valid run, seed-1 ketene r1, records
`pi_models_sha256=92824d2f826cc01649254bad8266b40a6c3cdbbc41eb6133a34791038b14359a`
in its run config and a nonzero Pi estimate of $0.9259122. All completed runs were
checked for the same digest and positive estimates. API credentials were never
printed, committed or included in report artifacts.

Validation actually run:
- Seed Mac sandbox preflight: **118 passed, 3 skipped**, 15.60 s.
- Rebased #46 sandbox and observation tests: **149 passed, 3 skipped**, 17.33 s.
- Rebased #46 full offline suite: **1009 passed, 8 skipped**, 204.25 s.
- Rebased #46 Ruff lint/format (237 files) and Pi TypeScript check passed.
- Launcher Ruff lint/format passed. Synthetic checks exercised acceptance boundaries,
  distinct run scratch paths and owned-process termination; actual isolated live
  directories and recovered offline reporting additionally verified those boundaries.
- Brief check passed; pin changed no refs. Doctor found no warnings and two existing
  draft-decision infos. The draft PR records the final review outcome.

Tighten removed an unused import and redundant cost calculation from the launcher.
The requested `codex/questdb-memory-hillclimb.md` was absent from the local checkouts;
the supplied conventions, Mac handoff and previous hill-climb report were followed.
No kypp tool was exposed. Unrelated `.mcp.json` remains untouched.
Raw stores and extracted diagnostic evidence remain local under this run directory;
credential-bearing Pi agent directories under `/tmp` are not committed.

## Spend and stop reason

Billed-key counter: **$113.866480116 before launch → $139.023041316 after stop**,
for **$25.156561200** total. The table's batch windows total $24.833232600;
$0.323328600 of inter-batch and late billing is also charged to the task. Pi's
estimates are verification only, not the source of this budget total.

The launcher polled billing every 20 seconds, refused new launches at $135 spent,
and had an in-flight stop at $145 to leave billing margin below the $150 ceiling.
Neither budget threshold was reached. All spend, including the invalid attempt
and interrupted runs, remains charged. Counter attribution assumes no unrelated
key use; completed seed-1 Pi estimates matched its billed delta to the cent.

**Stop reason: five audit false accepts during candidate 1.** One candidate batch
was started, none completed or accepted. Final incumbent: **@7, 28.5/46 mean**.
No further spending or scientific changes are authorized by the stopped protocol.

MAE was not used in any decision; the existing local reports retain it as a
secondary diagnostic.
