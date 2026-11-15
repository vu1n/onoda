# Guarded local GEPA pilot

This fixes wasted work in this experiment only. It changes neither Onoda's production
optimizer nor its authoritative gates, and it does not rewrite the archived results.

- Malformed head proposals return an empty update to GEPA, not the parent as a fake
  child. Permutations of the same three heads are unchanged. Both statuses are counted.
- `GEPA_MAX_PROPOSALS` defaults to 30 and caps reflector attempts independently of
  logical scored-example budget. Errors count too. The guard also refuses extra calls
  after the cap. GEPA 0.1.4's empty-mapping contract skips child evaluation; integration
  tests exercise that contract rather than assuming it.
- Each seed gets a new cache bound to its fixed evaluator/model/data snapshot. Only
  identical head order and identical ordered `(split, index)` batches reuse a result.
  No cross-batch per-example reuse, persistent cache, or resumed search is supported.
  Returned results are copied; traces remain available even after a trace-free call.
  Logical GEPA metric accounting is unchanged. `evals` now records physical misses;
  `cache` records logical requests and hit/miss durations per seed.
- Mixed-split output ordering is restored to input order, including duplicates.
- Importing `pilot` still loads GPT-2 and establishes data/controls, but no longer runs
  its random baseline or overwrites `results.json`. GEPA refuses existing guarded
  run directories/results before loading models. Use a fresh working directory with
  existing local `model` and `reflector` assets. Outputs use `gepa-guarded-*` names.

## Validation on November 15, 2026

Tests use installed GEPA 0.1.4 with scripted local proposals. No provider, solver,
network, new model download, or production change was used.

`replay_cache.py` replays the 61 archived evaluation requests as score lookups:
128 logical examples, 25 cold misses, 36 cold hits, 56 physical lookup examples.
A second pass hits all 61. Scores and trace values match exactly. One local measured
pass took 0.593 ms cold and 0.388 ms warm. These are Python archived-score/cache
bookkeeping timings, **not GPT-2 inference timings or a model speedup**. Full model
A/B is unverified: the accessible Onoda environment lacks torch/transformers, and
local model directories were not found in the bounded pilot locations searched.

Run from this directory with an existing GEPA environment (no install necessary):

```sh
python -B -m unittest test_search_control test_pilot -q
python -B replay_cache.py
```

The prior 118.38-second experiment cannot be decomposed into actual saved latency
from this replay. A later model-backed measurement must separately report model and
fixture setup, generation, validation rejects, physical evaluation, and journal/cache
work. Keep cold/warm measurements separate, preserve logical budgets, and include
failed attempts. Cache reuse is safe only while the model, data, deterministic runtime
settings and scoring code bound to the instance remain unchanged.
