---
id: terminal-bench
paths:
  - "src/onoda/adapters/tb/**"
  - "src/onoda/adapters/pi/contract.py"
  - "pi/extensions/onoda-recorder.ts"
  - "pi/extensions/lib/spool.ts"
entry:
  - "src/onoda/adapters/tb/harbor_agent.py"
  - "src/onoda/adapters/tb/proxy.py:SpendProxy"
  - "src/onoda/adapters/tb/slice.py"
tests:
  - "tests/test_tb_slice.py"
---

# Terminal-Bench slice and spend proxy

Run paired Pi arms against a Terminal-Bench task through Harbor's container interface and
measure their spooled mechanisms. A host spend proxy keeps the provider credential outside
the task container and enforces request and cost limits.

## Sub-features

- Container launch staging with arm-specific extensions and placeholder credentials.
- Local proxy with metered provider replies, request/total caps and fail-closed unknown pricing.
- Spool-derived mechanism metrics and paired slice orchestration.

## How to get to it (user POV)

This adapter is a Python/Harbor entry point, not an `onoda` CLI subcommand. Start from
`src/onoda/adapters/tb/slice.py` and the `harbor_agent.py` adapter when preparing a slice.
Use the offline tests to inspect staged files, proxy behavior and recorded metrics first.

## Driving it with pytest

| Action | Exact command | Observable result |
|---|---|---|
| Stage arms, send proxy requests and derive metrics | `uv run pytest tests/test_tb_slice.py` | Synthetic local upstream requests are capped/metered, real credential stays in the proxy and forged spools are rejected. |
| Inspect the orchestration entry point | `uv run python -m onoda.adapters.tb.slice --help` | Prints the slice arguments without launching Harbor or contacting a model. |

## Gotchas

- Offline tests use a local fake HTTP upstream: no Harbor, Docker, model provider or benchmark
  execution. There is no full live-container integration proof in this suite; that is a coverage gap.
- A container receives a placeholder credential. The host proxy owns authentication and
  accounting; unknown response prices fail closed unless explicit prices are supplied.
- Container staging uses its own isolation boundary rather than nesting the ordinary Pi
  command sandbox. See the A69 context in [agent sandbox](../decisions/agent-sandbox.md).
