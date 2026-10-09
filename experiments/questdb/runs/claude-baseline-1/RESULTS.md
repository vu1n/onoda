# Claude baseline 1: stopped before paid runs

Both requested models are available on OpenRouter, but the pinned Pi runtime
has no model-specific price entry for either. Its unknown-model fallback
borrows Kimi K2.6's prices. The required pricing preflight therefore failed:
`MAX_COST_USD=5` would use unrelated, substantially lower input/output rates.
**No pilot or batch was launched; total task model spend is $0.**

This is an unmeasured baseline, not a negative model result. No model was
substituted, and no adoption decision was made.

## Scope and pin

Remote `main` was fetched on 2026-10-09 and resolved to
`79d1f775c01987238783dcfd33b8809abb7dcd9b`. The existing clean detached worktree
at `.pinned/79d1f77` was verified and reused. It excludes #46, which remains a
draft. The report branch is `codex/questdb-claude-baseline-1`.

The planned experiment was S1 only, ketene and thioacetone: Opus pilot with two
repeats per molecule, then two more if its gate passed; Sonnet four per molecule
if at least $10 remained, with the same gate after its first four runs. Caps:
200 requests, $5 Pi estimate, 7200 seconds, report reminder on, four concurrent
runs, and $40 actual OpenRouter spend for this task. Provider errors permit one
fresh-work replacement; nothing else is repeated. No run or replacement occurred.

Code, `memory-project@7`, parser, grader v3, target inputs and ladder were not
edited. Memory SHA-256: `a47b1f737c613ad8ac0ecfac02c31e116caa164e0675a96fe5817e3011a3e1e1`.

## Model and price checks

Exactly one free request to `https://openrouter.ai/api/v1/models` returned
469 models at 2026-10-09T10:26:46.086628Z, including these exact non-batch IDs:

| Model | OpenRouter ID | Input / output per million tokens | Pi model entry |
|---|---|---:|---|
| Opus 5.5 | `anthropic/claude-opus-5.5` | $4 / $20 | absent |
| Sonnet 5.5 | `anthropic/claude-sonnet-5.5` | $2 / $10 | absent |

The runtime actually used by the pinned coding agent is Pi / pi-ai **0.85.1**.
Its nested pi-ai dependency matters: the top-level pi-ai copy is a different
version and is not the coding agent's catalog. Both the built-in lookup and a
fresh `ModelRuntime` reported neither requested ID as known. Network was
forbidden during the fresh-runtime reproduction.

`resolveCliModel` nevertheless accepts each exact ID and warns that it is using
a custom model. `buildFallbackModel` copies the default OpenRouter model,
`moonshotai/kimi-k2.6`, including its prices: input **$0.95**, output **$4**,
cache read **$0.16**, cache write **$0** per million tokens. This is not an
all-zero estimate; it fails the separate prerequisite that Pi know the requested
model's prices. Nonzero fallback prices do not satisfy that prerequisite.

Reproduction uses installed Pi's `core/model-resolver.js` with
`resolveCliModel({cliProvider: "openrouter", cliModel: id + ":high", modelRuntime})`.
The relevant installed-source boundaries are `buildFallbackModel`,
`createAgentSessionServices`, and `ModelRuntime.create`. The harness creates a
fresh agent directory; print/JSON mode does not perform the interactive/RPC
catalog refresh. No custom price configuration or dependency upgrade was made.

The CLI resolver accepts the `:high` suffix and returns `thinkingLevel: high`
for both IDs. No provider request was sent, so **effort actually sent is null**;
wire-level effort remains unverified. Local `launch.json` records this explicitly.
The free OpenRouter response advertises default effort `high` for both models,
which differs from the task's Opus-medium assumption. No default was relied on.

## Results

| Model | Runs completed | Found | Accurate within 0.3 eV | Cost per run | Stop reason |
|---|---:|---:|---:|---:|---|
| DeepSeek reference | three prior 8-run batches, per task | not re-aggregated | mean 22/56; range 21–23 | not re-aggregated | reference only |
| Opus 5.5 | 0 | not measured | not measured | N/A | model-specific price entry absent |
| Sonnet 5.5 | 0 | not measured | not measured | N/A | model-specific price entry absent |

There are no per-run rows: no runs exist. Experiment inference tokens and jobs
are zero; Mac sandbox test jobs are not experiment jobs. No claim, false-success
or `vte audit` measurements exist, so these metrics are **not measured**, not
zero. MAE, common-target MAE and cost per accurate state are also N/A. No
comparison of Claude tokens/jobs/cost with DeepSeek is possible from this task.

For later interpretation only: compare each completed 56-slot model batch with
DeepSeek's 22/56, and Opus with Sonnet. A gap of at least 5/56 is outside the
measured noise; a smaller gap is a tie. Report found and accurate separately.
When scores tie, cost per accurate state decides adoption. No such comparison
or adoption action was performed here. S0 and Fable remain out of scope.

## Validation, billing and preservation

The HANDOFF Mac preflight passed **118 tests, 3 skipped in 29.39 seconds**:
`test_pi_sandbox_seatbelt.py`, `test_orca_sandbox.py`, `test_orca_broker.py`, and
`test_pi_sandbox.py`, using the pinned runtime, ORCA 6.1.1, the configured
OpenMPI tree and short `.pt/t` basetemp. Main and the detached runtime remain
unchanged; the Pi lockfile matches the main checkout's installed runtime lock.

A free billed-key snapshot was recorded before any potential launch and
retained locally. No launch occurred, so no per-batch counter delta is reported. **Task model spend is $0 because no paid requests were issued**, not
because a counter difference was rounded to zero. Key exclusivity was not
established and was not needed for this stopped preflight. The $40 task budget
is unspent; earlier DeepSeek experiments are outside this task's budget.

The raw catalog response, built-in/fresh-runtime checks, resolution evidence,
Mac preflight log, billed snapshot and not-launched metadata remain in the local
`claude-baseline-1/` archive. Only this report and `ledger.jsonl` are committed.
No credentials, molecular reference answers or raw prior-run sessions are added.

Resumption needs an explicit scope decision to provide verified model-specific
pricing through supported configuration or an updated pinned runtime, followed
by a fresh pricing preflight. This report does not authorize that change.
This is exploratory development work, not a chemistry result.
