# SoL-Pi delta to the scientific-harness handoff

**Prepared:** September 26, 2026 • **Applies to:** handoff v0.1 (Sept 22, 2026)
**Status:** Design delta. Nothing here has been installed or executed. Treat SoL-Pi claims as the authors' reported results on coding benchmarks, not evidence about chemistry.

## 0. Harness decision (revised Sept 26)

Prime Agent is built on Pi (its README: "Our agent and TUI is built on top of pi"); both are MIT. That makes two routes possible:

| Route | What we build | Keeps | Costs |
|---|---|---|---|
| **A. Pi + SoL-Pi + our RLM extension (default)** | A small Pi extension giving the model a persistent `ipython` tool and a recursive `rlm()` child call, designed around our semantic checkpoints from day one. | SoL-Pi unmodified on the harness it was measured on, so the Pi and Pi+SoL-Pi runs are a validated control. RLM becomes one more ablatable mechanism the hill-climbing loop can accept or reject. | We rebuild the RLM pieces we need. Prime's background sessions, refinement and global memory are left out, which the handoff wanted disabled for controlled runs anyway. |
| **B. PrimeAgent-SoL** | Port SoL-Pi's mechanisms into Prime. | Prime's mature RLM runtime. | Prime exposes one `ipython` tool, so Action Fusion (edit + bash) has nothing to fuse and ObservationPack / the reducer must be re-targeted at kernel output. The paper's evidence no longer applies, and we inherit Prime's kernel state and upstream drift from Pi (Prime's Pi version is not confirmed; SoL-Pi pins 0.85.1). |

Route A is the working default. Route B remains a legitimate later variant (P-SoL) once the loop exists to measure it. Where this document or BUILD_SPEC says "Prime adapter first", read "Pi adapter first; Prime is an optional comparison arm".

## 1. What SoL-Pi is (source S13)

- Paper: Liu et al., *SoL-Pi: Recursively Scaling Auto-Research Loops for Efficient Agent Harness*, arXiv:2609.20519 (Sept 17, 2026). Site: https://nvlabs.github.io/SoL-Pi/
- Code: https://github.com/NVlabs/SoL-Pi (MIT). A set of **extensions for the Pi coding agent** (`@earendil-works/pi-coding-agent@0.85.1`), installed with `pi install git:github.com/NVlabs/SoL-Pi`. It uses public Pi APIs and does not vendor Pi. The repo ships the mechanisms, not the auto-research loop or benchmarks.
- Reported result: on 51-task EdgeBench (40 held out), ~94% of Pi's score with 44.7–49.0% less recorded token traffic and about one third lower API cost, across GPT-5.6 Sol and Opus 5. On Terminal-Bench 4 it solved 15 vs Pi's 18 at $211 vs $286, so the capability cost is real.

It matters to this handoff in two separate ways:

1. **As a harness.** Four mechanisms aimed at exactly the regime our chemistry runs live in: multi-hour trajectories with huge tool outputs (ORCA logs).
2. **As a method.** An auto-research loop that searches over *harness mechanisms* (code), with fixed acceptance gates held outside the optimizer and a one-way held-out split. That is a broader and more rigorous version of what the handoff currently assigns to GEPA.

The handoff currently has no second harness, treats token efficiency only as a cost line item, and limits optimization to text surfaces. Those are the gaps.

## 2. The four mechanisms, and what each needs from our core

| Mechanism | What it does (per paper) | Conflict with handoff P0 rules | Required change |
|---|---|---|---|
| **ObservationPack** | Outputs > 10 KiB are archived; sent in full for the next two requests, then replaced by a stable handle, size, and head/tail excerpt; agent pages exact slices on demand. | Pi's local archive is a second store outside our artifact store; handles would break on export/import. Pack state (which request count each output is at) changes rendered context. | Back handles with `core/artifacts` content addresses. Paged recall is a gateway-recorded read. Pack state is part of the semantic checkpoint (PORTABLE_RUN_SPEC §checkpoint). |
| **Evidence-Preserving Reducer** | Build/test logs ≥ 4 KiB go to a cheaper model that writes a compact receipt; a deterministic verifier checks schema, source hash, exit status, exact quotes, size; falls back to the raw log on failure. | Quote verification proves *fidelity*, not *completeness*. A reducer that drops a convergence warning, root flip, or CI-weight line passes verification. A second model is a new provider call, a new cost, and a new leakage surface. | Receipts are **derived interpretations**, never parser output. Required scientific diagnostics always come from the deterministic `chemistry` parser, and the evaluator never reads receipts. Reducer calls are full invocation receipts (model id, cost, replay key). Log fallback events are recorded. Reducer runs inside the agent-visible boundary only. |
| **Online Context Compact** | Evaluates compaction at plan-step completion, estimating remaining requests and capping by context growth rate. | Compaction is lossy and its trigger depends on observed request counts, so it is not reproducible unless recorded. A fork taken after compaction sees only the summary. | Record every trigger decision and its inputs as an event. Align compaction points with safe checkpoints (after an observation, before a decision) so forks land on a declared boundary. Fork metadata states whether the branch sees raw history or a compaction summary. |
| **Action Fusion** | An edit/write and its follow-up validation command run in one tool call. | Fusing "write input + submit job" would bypass commit-before-visible, reviewed submission, and idempotency keys. | Fusion allowed only for idempotent local checks (e.g. write an ORCA input + run `validate_input`). A fused call is decomposed into separate gateway records. `submit`, `cancel`, and anything with budget impact are never fusable. |

## 3. Changes to BUILD_SPEC

**Architecture (§2).** Pi is the first agent adapter (see §0). The RLM extension lives in `adapters/agents/pi/rlm` and must externalize kernel state: anything a later step depends on is committed through the gateway, so a checkpoint never relies on live kernel memory. Prime stays an optional adapter; the `adapters/agents` contract should still fit it, as the harness-neutrality test.

**Modules.** Add `adapters/agents/pi` with SoL-Pi mechanisms as individually toggleable, recorded treatments. Add a `context` responsibility (or extend `core/records`) covering observation handles, compaction events, reducer receipts, and fusion decomposition.

**New §4 extension, "SoL-Pi."** Pin Pi and SoL-Pi revisions in M0. Integrate with each mechanism off by default, then enable them one at a time. Each mechanism logs its activation count per run, because the paper reports lower activation on Opus 5 than on the model it was tuned on.

**GEPA section (§4) becomes "Harness optimization."** Keep GEPA for text surfaces. Add an optional SoL-Pi-style mechanism search that may change declared harness code (context, observation handling, tool shapes, delegation) but never the evaluator, parsers, gateway, budget authority, or protected data. Borrow its structure:
- Oracle analysis over *our* recorded portable runs to find concrete overhead (repeated actions, context growth, large observations, sparse diagnostics). Each proposal must cite the trajectories that motivate it.
- Each candidate search runs as a **disposable loop instance**: fresh orchestration per candidate, keep the candidate diff and its evidence as artifacts, discard the orchestration code. No shared mutable orchestrator.
- Independent review of the implementation before evaluation; failed reviews trigger revision, not acceptance.
- Gates are fixed before search and live outside the optimizer's writable environment (this already matches our protected-evaluator rule).

**Milestones (§6).** M0–M2 unchanged. Revised:

| Milestone | Change |
|---|---|
| M0 | Also pin Pi and SoL-Pi; add S13 to sources; add assumptions below. |
| M1 | Add fixture tests for handle resolution after export/import, compaction-event replay, reducer fallback, and fused-call decomposition. Still offline, no Pi install required. |
| M3 | **Pi baseline** plus Pi + RLM extension on the same frozen dev set, tools, information, and evaluator. Prime baseline optional. |
| **M3b (new)** | SoL-Pi add-one ablation on Pi: baseline, each mechanism alone, full stack. |
| M6 | GEPA **and** optional mechanism search, both under the two-gate promotion rule. |

## 4. Changes to PORTABLE_RUN_SPEC

- **Checkpoint contents** add: observation-pack state (handle → artifact digest, per-output request age), compaction events with trigger inputs, and active mechanism configuration.
- **Replay.** A Pi run with SoL-Pi enabled produces different request bodies from plain Pi. Recorded calls from one never replay under the other; the existing `replay_mismatch` rule already covers this. Switching mechanisms at a fork is a declared treatment and needs a live suffix.
- **Cost accounting** splits cached tokens into **cache read** and **cache write** (SoL-Pi prices them differently via `cacheWriteReadRatio`, default 12.5). Add per-mechanism attribution: reducer model spend, tokens saved by handles (estimated, labelled as estimated), and paged-recall calls. Record total token traffic as a first-class metric, not only dollars.

## 5. Changes to EXPERIMENT_PLAN

**New variants.**

| ID | Variant | Isolated question |
|---|---|---|
| S0 | Pi + allowed documentation, no ladder | Pi baseline; compare to P0 for harness effect. |
| S1 | S0 + advisory ladder | Ladder effect on the base harness (replaces P1 as primary). |
| S1r | S1 + RLM extension | Does a persistent kernel and recursive calls help chemistry? |
| S2a–d | S1 + one SoL-Pi mechanism each | Which mechanism changes cost, and does it cost capability? |
| S3 | S1 + full SoL-Pi stack | Do the reported efficiency gains transfer to chemistry? |

Single-mechanism runs do not isolate interactions (the paper says so too). Report S3 separately from the sum of S2a–d.

**Promotion criteria (§9) adopt SoL-Pi's two gates**, declared before any search:
1. **Capability gate:** every capability metric in the outcome vector (identified-state coverage, scientific-check pass/unresolved counts, false-success claims, common-target reference error) stays within a predeclared tolerance of the baseline.
2. **Efficiency gate:** at least one declared efficiency metric (token traffic, inference cost, solver submissions, elapsed time) improves.

Among candidates passing both, keep the non-dominated set. This fits the handoff's rule against invented accuracy thresholds only if the tolerances are chosen and justified in the assumptions register before comparison. For false-success claims the tolerance should be zero increase. SoL-Pi's own TB4 drop (18 → 15) is the reason: a cost win that loses a scientific diagnostic is not a win here.

**Splits.** Mirror SoL-Pi's one-way held-out: a small acceptance slice that candidates may be gated on, plus an untouched final slice. Search results never read the final slice. Mechanism search may run on cheaper environments (synthetic fixtures, coding tasks) but acceptance must be on chemistry dev cases.

**Long-horizon relevance.** SoL-Pi chose EdgeBench because savings only show on 2–12 hour runs. Our multi-attempt CASSCF/NEVPT2 runs are in that regime, so efficiency results on short smoke tests should not be used to accept or reject a mechanism.

## 6. Additions to the assumptions register

| Question | Status | Required behavior |
|---|---|---|
| Pi / SoL-Pi revisions and extension API stability | Not pinned. | Pin in M0; SoL-Pi targets pi-coding-agent 0.85.1. |
| Whether ObservationPack thresholds (10 KiB, 2 requests) suit ORCA outputs | Unknown; tuned on coding. | Keep as configurable, recorded parameters; don't retune on test data. |
| Reducer completeness on scientific logs | Unverified. | Parser remains authoritative; measure diagnostics lost in receipts on dev cases. |
| Mechanism transfer across models | Paper reports lower activation on Opus 5. | Log activation rates per model; no transfer claim without data. |
| Prime's base Pi version and extension compatibility | Not verified. | Check before attempting route B. |
| Capability-gate tolerances | Not chosen. | Predeclare and justify before any comparison. |
| Auto-research loop code | Not released in the repo. | Reconstruct from the paper; label as reconstruction. |

## 7. What does not change

The P0 contract stands: portable records, immutable evidence, fork invalidation, protected evaluator, no-spend defaults, and M0–M1 as a harness-neutral offline slice. SoL-Pi strengthens the case for it, because every mechanism above mutates what the model sees, and the only way to compare Pi, Pi+SoL-Pi, Pi+RLM, and Prime fairly is a run record that none of them owns.
