---
id: agent-sandbox
project: onoda
type: decision
status: active
title: "Agent commands run sandboxed: bwrap two layers on Linux, one Seatbelt layer on macOS, refused elsewhere"
related_code:
  - "src/onoda/adapters/pi/sandbox.py"
  - "src/onoda/adapters/pi/runtime.py"
  - "pi/extensions/onoda-recorder.ts"
  - "src/onoda/adapters/tb/**"
  - "src/onoda/ablation/run.py"
  - "src/onoda/optimize/pi_runner.py"
carries: [A38, A50, A60, A69, A72]  # rows of the former docs/ASSUMPTIONS.md register
---

<!-- brief:anchor sandbox-or-refuse -->
## Run every live agent command inside the platform sandbox, or refuse the run

A live Pi run needs a sandbox and never degrades without one. Linux uses bubblewrap with two layers (outer: the Pi process tree; inner: Pi's `bash`, Action Fusion's `then_run` and the RLM kernel, with no network and a cleared environment). macOS uses one Seatbelt layer for the inner commands only, because macOS 27 refuses nested Seatbelt; Pi's node process runs unconfined there. Other platforms are refused. Pi's file tools are confined by the gateway to the workspace (and the bundle for `read`). One agent `bash` command is capped at 600 s. Terminal-Bench runs use the task container as the isolation instead, with no credential inside it.

**Why.** Agent-written commands run with the operator's privileges unless confined; a run that silently fell back to no sandbox could read the evaluator store or forge its own spool.

### Invariant
- `detect_sandbox`: Linux to bwrap, darwin to Seatbelt, anything else refused; ingest refuses a run whose recorded sandbox disagrees.
- Live ablations and optimizer trials require a sandbox.
- The `then_run` token filter in `gatewayDecision` is a check, not a boundary.
- `MAX_BASH_SECONDS = 600`; the recorder lowers a missing or longer `timeout` before spooling the call.
- TB runs: the provider base URL points at a host-side `SpendProxy` that adds the key and enforces per-run and whole-slice caps.

### Known gaps (not yet enforced; owner to decide)
- A60 macOS behaviour was checked in code only; it cannot run on the Linux host this was verified on.

### Carried forward from the assumptions register

Verbatim from `docs/ASSUMPTIONS.md` as of 83df418 (2026-10-04). Where a row and the invariants above disagree, the invariants are current.

Drift found when this was verified against code (2026-10-04):

- A69 says the TB spend cap uses the cost the provider reports; `SpendProxy` also accepts an optional `Prices` table for responses without a cost (counted in `cost_estimated`). Only an unpriceable response closes the run.
- A72 says `run.sh` lowers its per-run limit to 3600 s; 3600 s is already the `run-vte --timeout` default, so `run.sh` passes the default explicitly.
- docs/ADAPTER_SEAM.md §2 still says "Pi's `bash` runs unsandboxed (A38)"; A50/A60 sandboxes now exist.

#### A38: What bounds a Pi tool call?

Register status: decided (A50). Owner milestone: M3.

Before A50: Pi's `bash` tool and Action Fusion's `then_run` are unsandboxed. The gateway limits which tools run and the request/cost budgets, not what a shell command can reach; its `then_run` token filter is not a boundary. A sandbox (no solver on PATH, no scheduler, no network) is required before any live run. A live ablation also needs each run's filesystem isolated from the other arms' work directories and from the evaluator store, since arms run one after another under one work directory (M3b). The same holds for M4: the RLM kernel and child Pi processes run with the agent's privileges, so the agent can read a child's spool key while the child runs (and so forge that child's spool), make its own run impossible to ingest, or write where an optimizer search keeps its scratch state. The sandbox must cover the kernel, children, and the optimizer's run dir.

#### A50: How are Pi runs sandboxed?

Register status: decided. Owner milestone: live optimizer.

Bubblewrap (`bwrap`), Linux only; anything else refuses a sandboxed or live run, never degrades. Two layers of allowlisted bind mounts. Outer (the whole Pi process tree, RLM children included): system dirs, the pinned runtime, node, the Python environment, the RLM kernel dir and CA files read-only; the run's Pi work dir read-write; the task bundle read-only; private `/tmp`, `/proc`, pid/ipc/uts namespaces; the network stays on because Pi must reach its provider. The agent store, evaluator store, optimizer run dir, other runs' work dirs and the repository are not mounted. Inner (Pi's `bash` tool, so Action Fusion's `then_run`, and the RLM kernel): no network, cleared environment (no credential), the workspace read-write, the bundle read-only, an empty `/proc`; wired through Pi's `settings.json` `shellPath` and the kernel's interpreter variable, with `defaultProjectTrust: never` so a workspace `.pi/settings.json` cannot undo it. Pi's own `read`/`edit`/`write` run outside the inner layer, so the recorder's gateway blocks any whose path (symlinks resolved) leaves the workspace, or for `read` the bundle. Known limits: the file-tool check has a time-of-check gap (a symlink swapped after the check); the outer layer's network is open, so an agent that got code running in the Pi process itself could reach any host; the inner layer has no `/proc`; replay stays unsandboxed (it runs no model and no agent command reaches a live tool). A run records `sandbox: bwrap` in its start line and config, and ingest refuses a mismatch.

#### A60: How is a Pi run sandboxed on macOS?

Register status: decided (owner confirmed the single layer by merging PR #20, 2026-10-03). Owner milestone: QUESTDB slice.

ORCA runs only on the owner's Mac, so live runs need a sandbox there. `SeatbeltSandbox` (`sandbox-exec` profiles) has one layer, not bwrap's two (A50): macOS 27 refuses to apply a second Seatbelt sandbox inside a deny-default one (`sandbox_apply: Operation not permitted`; repro: `sandbox-exec -p '(version 1)(deny default)(allow process-exec process-fork)(allow file-read*)' sandbox-exec -p '(version 1)(allow default)' /bin/echo x`), and Pi must start the inner wrappers. So the Pi process runs with only a private TMPDIR/HOME and no Seatbelt, and the inner layer keeps A50's rules where macOS can: Pi's `bash` (so `then_run`) and the RLM kernel run deny-by-default with no network, a cleared environment and write access to the workspace (and a private tmp) only. Pi's file tools stay inside the gateway's roots (workspace, plus the bundle for `read`), exactly as with bwrap, so they cannot rewrite the read-only wrappers and profiles under `sandbox/`. Given up on macOS: Pi's node process itself (SoL-Pi, the extensions, RLM child Pi) is not confined; it can reach any host and the host filesystem as the user. That code is pinned, and the agent reaches files only through the gateway-checked tools. What the inner layer cannot give (no PID namespace, no `/proc`, `sandbox-exec` is deprecated though functional) is stated in its docstring. Linux keeps bwrap's two layers; any other platform is refused.

#### A69: How does a Pi run on a benchmark task that brings its own container (Terminal-Bench via Harbor)?

Register status: assumed: exploratory only. Owner milestone: TB4 slice.

`onoda.adapters.tb` runs the whole pinned Pi process inside the task container, so Pi's `bash` tool (and Action Fusion's `then_run`) only ever runs there; the container is the isolation and A50's bubblewrap sandbox is not used (it is Linux-only, and the task image is the workspace). The run keeps the variant table's mechanisms and `sol-pi.json`, the gateway policy with `max_requests` and `max_cost_usd`, the extension order (SoL-Pi, recorder last) and the keyed spool, and drops what is ORCA-specific: no task extension, so no task tools, no ladder and no raw tool-result tap; an arm id names its mechanism set only (S1 = Pi, S3 = full SoL-Pi stack). No credential enters the container: the provider's base URL points at a host-side proxy (`tb/proxy.py`) that adds the key and enforces a per-run and a whole-slice spend cap from the cost the provider reports; a response it cannot price closes the run. Runs are not ingested into a store (no `TaskManifest`, container session paths): the numbers are counts and sums over the HMAC-verified spool. Solved/unsolved is Harbor's verifier verdict, not an onoda evaluation. Results are a paired slice over operator-picked tasks, one attempt each, and may not be described as a mechanism verdict or a reproduction of the SoL-Pi paper's Terminal-Bench table.

#### A72: How long may one agent `bash` command run, and how does the agent get back to an earlier job's output?

Register status: decided (default, 8-molecule live run). Owner milestone: bash timeout.

600 seconds (`MAX_BASH_SECONDS`, policy field `max_bash_seconds`). The recorder sets a `bash` call's `timeout` to the cap when the agent gives none or a longer one, and the spooled input is the one that ran; Pi then ends the command with a timeout error the agent sees. ORCA runs in the host broker, not in bash, so solver jobs are not affected. Why: in the first 8-molecule run (2026-10-04) an agent's `find / -name ...` in its sandboxed shell never returned and held the run for 76 minutes until the 2-hour run limit. The number is a convenience bound, not a tuned value. The search happened because job files live only on the host and the agent sees each output once, in the `orca_submit` reply; so VTE runs also get `orca_output`, which re-reads an earlier job of the same run (whole, or the lines containing some text) from the bytes that reply verified, costs no job, and is a read in the record. Each job's input and output are also copied, read-only, into the workspace as `jobs/<job_id>/input.inp` and `output.out` after the bytes are spooled, so the agent's own shell tools (ls, grep) find them; the copies are agent-visible only, and ingest and the grader never read them (the grader reads the broker's journal bytes), so editing or deleting a copy changes nothing graded. `orca_submit`'s description and each reply name that folder. Each `orca_submit` reply says how many calls are left. Pi's stderr and the tail of its stdout are kept next to the spool after Pi exits (diagnostics only, never shown to the agent), since the hang above left no log. `run.sh` lowers its per-run limit to 3600 s, logs one progress line per run (an incomplete run is flagged as a harness failure, not a result), skips a run whose `run-vte` fails, and resumes past runs already graded.
