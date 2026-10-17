# QUESTDB Opus memory hill climb 1

Exploratory development work, not a chemistry result. Status: detached seed and candidate-1 sequence running.

## Frozen protocol

Base/current main: `ce0e9744e0b7f6b483724d51c6a0fc8ffa6adffe` (verified against GitHub before spend).
Scoring: ketene, thioacetone, acetaldehyde, diazomethane, two repeats each, S1 only.
Held out: formamide, acetone, cyclopropenone, methylenecyclopropene, untouched until the final check.
Settings: OpenRouter `anthropic/claude-opus-5.5:high`, 200 requests, harness cost cap 100,
7200 seconds, report reminder on, four workers, pinned `pi-models-claude-5.5.json`.
Score is grader-v3 identified slots within 0.3 eV. Accept at incumbent +3, no molecule
losing more than one summed slot, all eight runs settled with claims, zero false successes
and audit false accepts. Confirm at incumbent mean +2 with the same safety/claim guards.
A seed repeat difference above one on any molecule triggers a second seed batch and mean.
Stop at eight candidate batches (including confirmations), three consecutive rejected
candidates, less than $15 remaining of the $150 billed-counter ceiling, or any safety failure.
Held-out batches are conditional on an accepted candidate and remain subject to the ceiling.

## Step 0: diagnosis before edits or spend

Source: local `claude-baseline-1/opus/out/runs/<run>` agent stores and evaluator-v3
claim verdicts. Agent claims, spooled provider responses, original ORCA inputs embedded
in outputs, converged CI blocks and NEVPT2 transition tables were inspected. `diagnosis/`
contains local extracted agent-visible evidence. No `references.json` was opened.

Leakage note: the requested older wrong-state report and the first raw evaluator record
exposed reference labels embedded in prose/keys. Subsequent evaluation reads whitelist
claim verdicts and numeric aggregates; reference labels are not used in guidance. Agent
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

## Proposals for Vu

- Investigate systematic supported-but-unidentified character claims (two ketene slots,
  three thioacetone slots per repeat) as a reference/normalizer convention question.
  No grader, normalizer, references or task input change is authorized or made here.
- Prior transcripts invoke remembered QUESTDB/literature values before inspecting outputs.
  Consider how to measure this model-prior contamination separately; no reference lookup
  is used to tune this search.

## Preflight and provenance

Pinned seed `.pinned/oh0` at the base SHA; `uv sync`, matching Pi lockfile and copied
installed node_modules. Mac-only sandbox suite: **118 passed, 3 skipped, 15.60 s**.
Rebased #46: `d9f94f5` (original implementation `4e8469d`); sandbox plus observation
tests: **149 passed, 3 skipped, 17.33 s**. Launcher lint/format and synthetic
accept/reject boundary checks passed. `brief check` passed; `brief pin` changed no refs;
`brief doctor` found zero warnings and two pre-existing draft-decision infos.

The requested `codex/questdb-memory-hillclimb.md` was not found in this checkout or the
prior local workspace; the previous hill-climb report and supplied conventions were read.
No kypp tool is exposed. Unrelated `.mcp.json` is untouched.

## Batch ledger

The detached launcher appends each completed batch below and to `ledger.jsonl`.
It runs each completed run through grading and audit before refilling its four-worker pool.
Billing is polled every 20 seconds; launches stop at $135 spent, with an emergency
in-flight stop at $145 to leave billing margin under $150. Counter failure stops the
owned process trees. No live setting or agent sandbox was changed.

## Final diff against @7

Pending; no guidance edited.

## Uptake

Pending; no accepted bullet.

## Held-out results

Not yet run; conditional on acceptance.

## Spend and stop reason

Billed-key start: $113.866480116, recorded before the first launch. Total spend and stop reason pending.
