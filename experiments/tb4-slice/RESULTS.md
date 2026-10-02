# Slice 1 results (2026-10-01), exploratory

Ten Terminal-Bench 4.0.0 tasks (`tasks.txt`), one attempt per arm, OpenRouter
`deepseek/deepseek-v4-flash` for agent and reducer. Per run: 120 requests,
$0.20 (Pi's estimate), 20 minutes wall clock. Task images are amd64 and ran
under emulation on an arm64 Mac. All 20 spools verified against their HMAC keys.

Not a reproduction of the SoL-Pi paper's Table 3 (63 tasks, GPT-5.6 Sol, 8-hour
agent budget) and not a mechanism verdict (A54).

| | S1 (Pi) | S3 (SoL-Pi) | S3 vs S1 | Paper, SoL-Pi vs Pi |
|---|---|---|---|---|
| Solved | 0 / 10 | 0 / 10 | same | 15 vs 18 of 63 |
| Agent requests | 397 | 451 | +13.6% | |
| Reducer / summary requests | 0 / 0 | 0 / 2 | | |
| Tokens (input + cache read + output) | 23,153,116 | 21,509,019 | -7.1% | |
| Cost, Pi's estimate | $0.5243 | $0.4969 | -5.2% | |
| Cost, billed through the proxy | $0.9552 | $0.8956 | -6.2% | -26.3% |
| Reducer rewrote a result | 0 | 0 | | |
| Compactions completed | 0 | 2 | | |
| Fused `then_run` calls | 0 | 0 | | |
| `update_plan` / `obs_recall` calls | 0 / 0 | 15 / 0 | | |

How the runs ended: 11 of 20 were killed by the 20-minute limit (5 in S1, 6 in
S3), one S3 run hit the 120-request cap, eight settled on their own. One S3
verifier run timed out (vf2-speedup-networkx) and is counted as unsolved.

## What the records show

- **Reducer: never a candidate.** SoL-Pi only reduces the output of a
  recognized build or test command (`make`, `pytest`, `npm test`, ...) of at
  least 4 KB. In 20 runs, 24 such commands ran and none produced 4 KB: the
  model pipes its own output through `tail`/`head`. The few outputs over 4 KB
  (three runs, up to 51 KB) came from commands the rule does not recognize.
- **Compaction: fired twice**, both in S3 at a plan boundary (session-window-debug
  after 10 agent requests, batched-eval-parity after 16). Both runs kept
  working afterwards and were later stopped by the time limit.
- **Action Fusion: never used.** The fused `edit`/`write` tools were offered
  in every S3 run; the model never passed `then_run`.
- **Cost difference is within what the truncation alone could produce.** Per
  task, S3 was cheaper on 5 and dearer on 5 (billed). With most runs cut at 20
  minutes, an arm's cost is mostly how much it got through in that time.

## Spend

- Billed through the proxy: $1.920 ($1.851 for the 20 reported runs, $0.069
  for one round interrupted by a driver restart and re-run).
- OpenRouter's own usage figure for the key rose by $1.989 over the slice.
  The $0.069 gap matches requests in flight when a run was killed: the proxy
  stops reading when the client disconnects, so it never sees their usage.
- The billed cost is about 1.8 times Pi's estimate in both arms, so Pi's
  price table understates this model on OpenRouter and the recorder's
  `max_cost_usd` is looser than it reads. The cause was not investigated.
