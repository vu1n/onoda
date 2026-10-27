# Onoda feature map

Onoda records portable scientific-agent experiments: inspect and replay exact evidence,
branch at semantic checkpoints, run pinned Pi harnesses, and grade claims separately from
agent-visible data. Start with the feature below that matches the user action, then read
its entry points, functional tests and Gotchas. Maps describe current implementation;
they do not approve a scientific claim or a proposed design change.

## Preconditions and driving conventions

Run commands from the repository root with Python 3.11+, `uv`, Node and npm available.
`uv sync --locked` installs the Python environment. `scripts/pi-setup.sh` installs pinned
Pi/SoL-Pi from the lockfile (network once, lifecycle scripts disabled); Pi suites otherwise
skip. `cd pi && npx tsc -p .` checks the installed extension interfaces. The demo also needs
`jq` and a POSIX shell. Use a fresh temporary store/work directory, with a short path for
VTE Unix sockets; the tests create these automatically.

`uv run pytest` is offline with respect to external services: scripted providers, synthetic
claims, labelled solver stand-ins, and local fake transports. It needs no credentials or
ORCA installation. Linux bubblewrap and macOS Seatbelt tests probe platform support and
can skip; inspect the skip reasons. A green result with skipped runtime/sandbox tests does
not establish those behaviors. Live experiments and paid-provider/solver runs are outside
these harness commands. Pending-review cases and synthetic trajectories are not validated
scientific results.

Each feature pairs a user action with an exact command and observable assertions. Run its
commands after changing behavior; when shared files such as `cli.py` change, several maps
may apply. `scripts/demo.sh` drives the cross-feature CLI journey in disposable stores.

## Features

| Feature | User or caller outcome |
|---|---|
| [Records and forks](records-and-forks.md) | Commit durable records, resume fixture work, fork checkpoints and compare cost/lineage. |
| [Portability and replay](portability-and-replay.md) | Move bundles, inspect capabilities, replay exact decisions and reanalyze evidence. |
| [Scientific cases](scientific-cases.md) | Ingest pinned ORCA output, parse/evaluate states and check source evidence packets. |
| [Pi runs](pi-runs.md) | Run, grade, replay and fork pinned Pi variants with recorded context. |
| [ORCA broker](orca-broker.md) | Submit confined jobs, cache/replay them and audit opt-in fault injection. |
| [VTE grading](vte-grading.md) | Match cited claims by identity and report accuracy and process metrics. |
| [RLM](rlm.md) | Execute Python cells, record recursive child runs and commit portable state. |
| [Optimization](optimization.md) | Compare ablations, search candidates and enforce promotion gates. |
| [Decision advice](decision-advice.md) | Record shadow judgments, tune question wording and compare frozen memory tiers. |
| [Terminal-Bench](terminal-bench.md) | Stage container task arms and meter model calls through a host proxy. |

## Maintaining and checking the map

```sh
python3 scripts/featuremap.py features src/onoda/science/orca_broker.py
python3 scripts/featuremap.py features --base origin/main
python3 scripts/featuremap.py check
```

Update maps with the code they describe. `paths` are implementation globs; `entry` points
are where to start reading; `tests` are behavioral examples. The checker uses tracked
files, so stage newly added implementation/tests before checking new pointers. It checks
only pointer existence, symbols, nonempty paths and duplicate ids, not behavioral truth.
Review descriptions and run the listed harnesses as well. Uncovered files in `features`
output are a review prompt, not a passing assertion of completeness.

The stdlib-only helper is vendored from
[vu1n/dev's feature-map skill](https://github.com/vu1n/dev/blob/main/skills/feature-map/SKILL.md)
(source blob `e5afead7eba037759119d570f33f7f679454b04b`, adapted to repository formatting).
The `Feature map` workflow runs its pointer check on PRs, including drafts and docs-only
changes. Existing product CI retains its draft/local-verified and prose-skip policy.

[Decision context](../decisions/README.md) preserves all 22 prior current documents,
their status labels, historical versions and A-row references. The
[optimizer validation-slice proposal](../proposals/optimizer-validation-slice.md) remains
unratified. No runtime serialized provenance identifiers are changed by this migration.
