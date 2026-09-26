# Implementation instructions

Read `README.md`, then `docs/BUILD_SPEC.md` and `docs/PORTABLE_RUN_SPEC.md`. Use `docs/ACCEPTANCE_TESTS.md` as the delivery contract. Respect any existing repository instructions and conventions.

## Priorities

1. Data portability, provenance, intermediate reproducibility, and safe forking are P0, not an export feature to retrofit.
2. Implement a small working vertical slice. Do not build a new general-purpose agent framework, hosted control plane, or UI.
3. Keep the scientific environment, evaluator, and artifact format independent of Prime, Jev, GEPA, and a hosted tracing service.
4. Preserve raw evidence. Derived observations, interpretations, evaluations, and memories must cite immutable inputs and versions.
5. Separate recording, offline replay, deterministic reanalysis, live reexecution, resume, and fork. Report exactly which capability was demonstrated.
6. A framework session, Python pickle, VM snapshot, or vector database is not the canonical portable record.
7. Never reuse a recorded response after its request changes. A fork may reuse valid ancestors, not the original future.
8. Keep evaluator answers and held-out feedback outside agent-visible storage, tools, and credentials. Prompt instructions alone are not isolation.
9. Default to offline fixtures and zero authorized external spend. Never infer authorization from the presence of an API key.
10. Do not invent SDK methods, model versions, chemistry thresholds, benchmark membership, or expert checks. Inspect sources, pin dependencies, and record unresolved assumptions.
11. Never describe synthetic output as ORCA output or a chemistry success. Label fixtures, mocks, and blocked capabilities visibly.
12. Do not obtain or redistribute proprietary binaries, credentials, private conversations, or third-party content without appropriate permission.
13. Capture observable requests, responses, tool actions, and concise evidence-linked explanations when available. Do not require unavailable private model reasoning.
14. Import is data-only. Never execute imported code, restore arbitrary Python objects, or contact providers automatically.
15. Keep individual milestones runnable and tested. Document actual commands and limitations, not aspirational functionality.

## Working style

Use explicit types and versioned schemas, small adapters, dependency locks, meaningful errors, and tests at trust boundaries. Prefer the existing repository's stack; otherwise use a Python core with a thin Prime integration. Keep upstream changes minimal. All example CLI commands in the handoff are desired interfaces, not existing APIs.

Before reporting completion: run the relevant tests, exercise an export/import cycle in a fresh directory, and list what is implemented, what was actually executed, and what remains blocked.
