# Adapter seam: Pi-first, harness-neutral

Status: design sketch for M0 (BUILD_SPEC §4, SOL_PI_DELTA §0/§3/§7). Nothing
in this document has been installed or executed. Pi is pinned at
`@earendil-works/pi-coding-agent@0.85.1` (provenance/sources.json, S14);
SoL-Pi is pinned at commit `1559b5cb12c72da4a485bc50fe326586b216fb19` (S13).
Every specific extension point named below is marked **unverified** and must
be checked against that pinned Pi version before any real adapter is built
against it (AGENTS.md rule 10: do not invent SDK methods).

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
or daemon.** Concretely, the seam (`src/labrun/adapters/agents.py`,
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
   request id. Field names mirror `labrun.records.InvocationReceipt` /
   `Usage` so a real gateway can commit a `CallRecord` directly as evidence.
   It never contains unobservable model-internal reasoning (AGENTS.md
   rule 13) and never claims a provider-resolved model version that was not
   actually exposed (assumptions register A9).

`AgentAdapter` is a `typing.Protocol` (`runtime_checkable`), not a base
class: Pi, Prime, and the test mock all satisfy it structurally, which is
the harness-neutrality test SOL_PI_DELTA §3 asks for ("the `adapters/agents`
contract should still fit [Prime], as the harness-neutrality test").

## 2. Where Pi's real extension points would attach — unverified list

Everything below is **to verify against pinned Pi 0.85.1** before
implementation. None of it is assumed to exist with this shape; it is
where SOL_PI_DELTA §3 says the RLM extension and the gateway boundary must
sit, phrased against Pi's documented extension surface in general terms.

| Seam method | Where it would attach in Pi (unverified) | What must be checked |
|---|---|---|
| `render_context` | Pi's context-construction step, before a request is sent to the provider (the same point SoL-Pi's ObservationPack and Online Context Compact operate on, per SOL_PI_DELTA §2). | Whether Pi exposes a stable hook at this point in 0.85.1, or whether it must be observed by wrapping the outbound request instead. |
| `propose_action` | Pi's tool-call boundary, before a tool actually executes (the point Action Fusion collapses two calls into one, per SOL_PI_DELTA §2). | The exact shape of a Pi tool-call event and whether it can be intercepted before execution vs. only logged after. |
| `capture` | Pi's provider-call boundary (request/response, token usage, request id). | Which fields Pi's SDK actually surfaces for `@earendil-works/pi-coding-agent@0.85.1`; do not assume parity with any other version, including npm `latest` (currently `0.87.1`). |
| RLM kernel externalization | `adapters/agents/pi/rlm`, per SOL_PI_DELTA §3: "must externalize kernel state: anything a later step depends on is committed through the gateway, so a checkpoint never relies on live kernel memory." | Prime's RLM docs (S4, S5) describe the *programming model* we are reconstructing for Pi, not an API Pi itself exposes. Pi has no persistent kernel today; ours is new code, not a Pi feature. |
| SoL-Pi mechanism toggles | Four mechanisms (ObservationPack, Evidence-Preserving Reducer, Online Context Compact, Action Fusion) installed via SoL-Pi on top of Pi 0.85.1. | SoL-Pi "uses public Pi APIs and does not vendor Pi" (SOL_PI_DELTA §1) — which public APIs, exactly, is unverified until the pinned commit is inspected. |

## 3. How RLM kernel state is externalized through the gateway

BUILD_SPEC §4 (as revised by SOL_PI_DELTA §3): a persistent REPL is a
working convenience, never the record. The design rule for the RLM
extension is:

- Every value a later step depends on — a variable the recursive `rlm()`
  call produced, a partial computation the persistent `ipython` tool holds
  — is committed as an artifact or checkpoint field through the gateway
  *before* any subsequent step may reference it. "Committed" means: written
  to `core/artifacts` (content-addressed), or included in the checkpoint's
  `context` / `visible` fields (`labrun.records.Checkpoint`).
- A checkpoint's `capabilities` field (already in `labrun.records.Checkpoint`)
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
  `labrun.records.BOUNDARIES` already defines, so a fork always lands on a
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

## 6. Explicit "unverified" list

Everything in this list is a named gap, not a decision:

- Pi's actual hook/callback names for context construction, tool-call
  interception, and provider-call observation at `0.85.1` (§2 table).
- Whether SoL-Pi's public-API usage (§1, SOL_PI_DELTA §1) is stable between
  Pi `0.85.1` (pinned) and Pi `0.87.1` (current npm `latest`).
- ObservationPack's thresholds (10 KiB archive, 2-request resend window)
  applied to ORCA-sized logs — tuned on coding benchmarks, not chemistry
  (assumptions register A14).
- Reducer completeness on scientific logs (assumptions register A15).
- Mechanism activation-rate transfer across models (assumptions register
  A16).
- Prime's own base Pi version and its compatibility with the SoL-Pi
  extension API, needed only if Route B is ever attempted (assumptions
  register A17).
- The auto-research loop that produced SoL-Pi's four mechanisms: the paper
  describes it, the repository does not ship it (assumptions register A19).

None of these block `AgentAdapter` or `DeterministicMockAdapter` — both are
harness-independent and fully offline — but they block writing a real `Pi`
or `SoL-Pi` adapter class, which is out of scope for M0.
