# Paste into Codex

Implement the first working slice of the scientific-agent harness described in this handoff.

Read `README.md`, `AGENTS.md`, `docs/BUILD_SPEC.md`, `docs/PORTABLE_RUN_SPEC.md`, `docs/ACCEPTANCE_TESTS.md`, and `docs/SOL_PI_DELTA.md` (which overrides conflicting text). Inspect the current repository before changing anything. Follow its existing instructions and preserve unrelated work.

The priorities are **data portability, reproducibility at intermediate steps, and forkable experiment histories**. Prime Agent is the first intended agent adapter, not the owner of the data model. Jev, shared memory, and GEPA are later independently evaluated components.

Start with **M0 and M1**:

- Establish source/dependency pins and an explicit assumptions register. Do not wait for the paper authors' code or invent missing scientific details.
- Implement a domain-neutral local artifact store, immutable typed execution records, an append-only journal, portable checkpoints, export/import, offline replay, deterministic reanalysis, and forks with dependency invalidation.
- Add a completely synthetic, deterministic fixture workflow. It must need no ORCA installation, provider credentials, network, or paid compute.
- Demonstrate the full sequence in a clean temporary workspace: execute fixture → checkpoint → export → import → replay → reanalyze → fork before a decision → change the decision policy → execute the new suffix → compare lineage and costs.
- Include tests for checksum corruption, missing objects, changed-request replay rejection, multiple invocations of the same specification, crash-safe job submission, no inherited future evidence, no duplicate side effects, and protected evaluator data.
- Inspect Prime's current extension model and sketch the smallest adapter seam, but do not block the portable core on a real provider connection.

Keep this small. No frontend, distributed scheduler, vector database service, model training, live benchmark, or deep Prime fork. A local filesystem and a rebuildable index are sufficient. Reuse libraries for well-defined standards; avoid inventing a canonical JSON algorithm.

The sample YAML files are design templates, not an implemented schema. Replace them with validated configurations as the implementation becomes concrete. Explicitly reject unresolved live-run settings.

After implementing and running tests, report the files changed, exact tested commands, acceptance tests passed, the demonstrated reproducibility capabilities, and any limitations. Do not claim the chemistry experiment has been reproduced. Propose M2 only after the offline portability slice passes.
