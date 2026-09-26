# Adapter seam: Pi-first, harness-neutral

Status: M0 design sketch, verified in M3 against the installed pins. Pi is
pinned at `@earendil-works/pi-coding-agent@0.85.1` (provenance/sources.json,
S14) and SoL-Pi at commit `1559b5cb12c72da4a485bc50fe326586b216fb19` (S13),
both locked in `pi/package-lock.json` and installed by `scripts/pi-setup.sh`.
§2 records which extension points were verified by running that Pi process
(offline, under the scripted provider) and how onoda's extensions use them.
Nothing here has run against a real model provider.

## 0. Why Pi is first (Route A)

SOL_PI_DELTA §0 revises the handoff: Prime Agent is itself built on Pi
("Our agent and TUI is built on top of pi", MIT). Two routes are possible;
**Route A — Pi + SoL-Pi + our own RLM extension — is the working default**:

- SoL-Pi runs unmodified on the harness it was measured on, so a Pi run and
  a Pi+SoL-Pi run are a validated control pair.
- Our RLM extension (persistent `ipython` tool, recursive `rlm()` child
  call) is designed around semantic checkpoints from day one, instead of
  inheriting Prime's kernel lifecycle.
- Prime's background sessions, refinement, and global memory — which the
  handoff wanted disabled for controlled runs anyway — are simply absent.

Route B (port SoL-Pi's mechanisms into Prime) remains a legitimate later
variant once a loop exists to measure it (assumptions register A17). It is
not attempted here.

## 1. What the seam has to externalize

The single hard requirement (PORTABLE_RUN_SPEC, AGENTS.md rule 6): **critical
data cannot exist only in a Python variable, retained subagent, host cache,
or daemon.** Concretely, the seam (`src/onoda/adapters/agents.py`,
`AgentAdapter`) externalizes exactly three things, each independent of which
harness implements it:

1. **`render_context(checkpoint_state) -> RenderedContext`** — what the
   model is about to be shown. This is the recorded artifact a cross-harness
   semantic fork actually compares: Pi and Prime construct their prompts
   completely differently, but both must produce a `RenderedContext` whose
   digest and message list are what a checkpoint stores and what a fork
   inherits or replaces. Nothing about Pi's internal `ipython` kernel state
   is assumed here — only its rendered *output*.
2. **`propose_action(rendered, tools) -> ProposedAction`** — a proposal, not
   an effect. Every side effect (writing an ORCA input, submitting a job)
   is decomposed into a gateway call after this point (BUILD_SPEC §4); the
   adapter itself never executes anything directly. This is also where
   SoL-Pi's Action Fusion would have to be decomposed back into separate
   gateway records (SOL_PI_DELTA §2) before commit.
3. **`capture(call) -> CallRecord`** — the observable record of the
   underlying provider call: request body, response, model id, usage,
   request id. Field names mirror `onoda.records.InvocationReceipt` /
   `Usage` so a real gateway can commit a `CallRecord` directly as evidence.
   It never contains unobservable model-internal reasoning (AGENTS.md
   rule 13) and never claims a provider-resolved model version that was not
   actually exposed (assumptions register A9).

`AgentAdapter` is a `typing.Protocol` (`runtime_checkable`), not a base
class: Pi, Prime, and the test mock all satisfy it structurally, which is
the harness-neutrality test SOL_PI_DELTA §3 asks for ("the `adapters/agents`
contract should still fit [Prime], as the harness-neutrality test").

## 2. Where Pi's extension points attach — verified against 0.85.1 (M3)

Verified by launching the pinned `pi` binary with onoda's extensions
(`pi/extensions/`) and asserting on what they spooled
(`tests/test_pi_extensions.py`, `tests/test_pi_end_to_end.py`). The spool
format is fixed in `src/onoda/adapters/pi/contract.py`.

| Seam method | Pi 0.85.1 extension point (verified) | How onoda uses it |
|---|---|---|
| `render_context` | `context` event (messages after every extension's transform) and `before_provider_request` (the provider payload). A custom provider's `streamSimple(model, context)` receives `{systemPrompt, messages, tools}` directly. | The recorder loads last, so it spools exactly what the model is shown. The scripted provider spools its own request with a replay key (timestamps dropped, absolute paths aliased; A34). |
| `propose_action` | `tool_call` event, fired before the tool executes; returning `{block: true, reason}` stops it and `event.input` is the call's arguments. | The recorder spools every call (allowed or blocked) before it runs and enforces the gateway policy there: tool allowlist, request and acquisition budgets, and no fused `then_run` that could reach a solver. |
| `capture` | Assistant `message_end` carries the final message with `usage` (input, output, cacheRead, cacheWrite, totalTokens, cost). No request id is surfaced to extensions. | Spooled as `provider.response`; tokens as integers, Pi's cost estimate as decimal text. |
| RLM kernel externalization | Not a Pi feature. | Not built in M3; variant S1r refuses to run. |
| SoL-Pi mechanism toggles | SoL-Pi reads `$PI_CODING_AGENT_DIR/sol-pi.json` at `session_start` and registers only enabled mechanisms. It uses `context`, `before_provider_request`, `tool_result`, `turn_end`, `agent_settled`, `session_before_tree`, the compaction events, `registerTool`, and `ExtensionContext.compact()` (per its `docs/compatibility.md`, and exercised here). | The runtime writes one `sol-pi.json` per variant into an isolated agent dir. ObservationPack archives under the session dir; ingest imports each archived object into the store and maps `obs_id` to its digest. |

Findings that differ from the M0 sketch:

- `before_provider_request` fires for a custom provider only if it calls
  `options.onPayload`, which Pi's provider guide requires; the scripted
  provider does.
- Pi exits 0 even when a run ends because the script ran out or a replay key
  differed; the outcome is read from `spool.end`, never the exit code.
- SoL-Pi's reducer calls its model through `modelRegistry.complete()`, which
  fires no hooks. The scripted provider spools reducer calls itself; a real
  reducer provider's calls would not be spooled (open before any live S2b/S3
  run).
- Pi's compaction summaries call the provider directly, without
  `options.onPayload`, so they fire no `before_provider_request` and no
  `message_end`. The scripted provider recognizes them by that missing
  `onPayload`, serves them from `Script.summary_turns`, and spools them with
  `purpose: "summary"` (ingest: `compaction-summary` receipts). A live
  provider's compaction-summary calls are not spooled (the same gap as the
  reducer's; open before any live S2c/S3 run).
- Pi rejects some tool calls before `tool_call` fires (unknown or disallowed
  tool name, arguments failing the schema, a message truncated at the token
  limit) and still shows the model an error result. The recorder spools a
  blocked `tool.call` for each such result ("rejected by Pi before
  tool_call") from the `tool_execution_start` arguments.
- Spool integrity: each launch hands the Pi process a random HMAC key through
  a 0600 file the spool writer reads and unlinks at load; every spool line
  carries a chained `mac` that ingest verifies with the key the runtime kept
  in memory. The recorder then deletes every `ONODA_PI_*` variable from the
  process environment, and the child environment itself is an allowlist.
  This is tamper evidence against the agent's tools rewriting the spool, not
  a sandbox (A38): Pi's `bash` runs unsandboxed.
- SoL-Pi's reducer only rewrites output of commands it recognizes as build or
  test logs, so on the task tools (`case_input`, `orca_run`) S2b is inert.

## 3. How RLM kernel state is externalized through the gateway

BUILD_SPEC §4 (as revised by SOL_PI_DELTA §3): a persistent REPL is a
working convenience, never the record. The design rule for the RLM
extension is:

- Every value a later step depends on — a variable the recursive `rlm()`
  call produced, a partial computation the persistent `ipython` tool holds
  — is committed as an artifact or checkpoint field through the gateway
  *before* any subsequent step may reference it. "Committed" means: written
  to `core/artifacts` (content-addressed), or included in the checkpoint's
  `context` / `visible` fields (`onoda.records.Checkpoint`).
- A checkpoint's `capabilities` field (already in `onoda.records.Checkpoint`)
  states `{"semantic_restart": "supported", "native_resume": "unsupported"}`
  for any Pi/RLM-backed run: resuming means replaying committed state into
  a fresh kernel, never reattaching to the original process (assumptions
  register A10, A24).
- Recursive child calls (`rlm()`) are themselves `ProposedAction` /
  `CallRecord` pairs with `parents` set (mirroring
  `InvocationReceipt.parents`), so a subagent tree is visible in the run
  record instead of living only inside the parent's kernel memory.

## 4. Where SoL-Pi mechanisms attach as toggleable, recorded treatments

Per SOL_PI_DELTA §2 and §3 ("Add `adapters/agents/pi` with SoL-Pi mechanisms
as individually toggleable, recorded treatments"): each mechanism is off by
default and enabled one at a time, and each logs its own activation count
per run (needed because the paper reports lower activation on Opus 5 than
its tuning model — assumptions register A16). None of the four mechanisms
change the seam's method signatures; they change what happens *inside*
`render_context` / `propose_action` / `capture` for a Pi adapter instance,
and what gets recorded alongside:

- **ObservationPack** — large outputs backed by `core/artifacts` content
  addresses instead of Pi's local archive, so a handle survives export/import
  (SOL_PI_DELTA §2). Pack state (which request count each output is at) is
  part of `RenderedContext`/checkpoint context, not adapter-local memory.
- **Evidence-Preserving Reducer** — runs inside the agent-visible boundary
  only; its receipts are `CallRecord`s in their own right (a second model
  call, its own cost and replay key), never a substitute for the
  deterministic `chemistry` parser output the evaluator reads (BUILD_SPEC §5,
  assumptions register A15).
- **Online Context Compact** — every trigger decision and its inputs is an
  event; compaction points are aligned with the same checkpoint boundaries
  `onoda.records.BOUNDARIES` already defines, so a fork always lands on a
  declared boundary and its metadata states whether it sees raw history or
  a summary (SOL_PI_DELTA §2).
- **Action Fusion** — allowed only for idempotent local checks; a fused call
  is decomposed into separate `ProposedAction`/gateway records before
  commit. Anything with budget impact (`submit`, `cancel`) is never fusable
  (SOL_PI_DELTA §2).

## 5. Why Prime fits the same Protocol

Prime is architecturally a TypeScript host plus a persistent Python/RLM
kernel on top of Pi (S3, S4). A `PrimeAdapter` would satisfy the same
`AgentAdapter` Protocol by:

- `render_context` reading Prime's materialized permitted state (BUILD_SPEC
  §4, "Prime baseline": "Provide functions that access the typed gateway and
  materialize the permitted current state") instead of Pi's context
  construction.
- `propose_action` treating Prime's single `ipython` tool call the same way
  Pi's tool calls are treated — as a proposal the gateway records before
  execution, not as a Prime-native persistence event.
- `capture` recording the same fields regardless of whether the call went
  through Prime's host or Pi's.

This is exactly SOL_PI_DELTA §3's stated test: "the `adapters/agents`
contract should still fit [Prime], as the harness-neutrality test." Building
`AgentAdapter` against Pi first and checking it against Prime's documented
shape (without implementing a `PrimeAdapter` yet) is how M0 keeps that test
live without installing either harness.

## 6. Still unverified

- Any real provider: live runs are built but no credentials are authorized
  (A11, A35), so provider-specific payloads, request ids, and cost fields are
  unobserved.
- Online Context Compact triggering a compaction inside a Pi run: the
  recorder spools compaction events, but no test has made SoL-Pi compact.
- Stability of SoL-Pi's extension use between Pi 0.85.1 and later releases
  (A13).
- ObservationPack thresholds on ORCA-sized logs (A14), reducer completeness on
  scientific logs (A15), and mechanism activation across models (A16).
- Prime's base Pi version and SoL-Pi compatibility (A17), needed only for
  Route B.
- The auto-research loop behind SoL-Pi (A19).
