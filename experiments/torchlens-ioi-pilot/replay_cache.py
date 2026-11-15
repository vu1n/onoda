"""Audit exact-batch reuse on archived scores, NOT a model benchmark."""

import json
import time
from pathlib import Path

from gepa.core.adapter import EvaluationBatch
from search_control import BatchCache


def replay(path):
    archive = json.loads(path.read_text())
    rows = archive["evals"]
    values = {}
    for row in rows:
        key = (tuple(row["candidate"]), tuple(tuple(x) for x in row["batch"]))
        if key in values:
            assert values[key] == row["scores"], "non-deterministic archived exact batch"
        values[key] = row["scores"]
    calls = []

    def lookup(batch, candidate, capture):
        key = (tuple(json.loads(candidate["heads"])), tuple(tuple(x) for x in batch))
        calls.append(len(batch))
        scores = values[key]
        return EvaluationBatch(list(scores), list(scores), list(scores))

    cache = BatchCache(lookup)
    timings = []
    for _ in range(2):
        start = time.perf_counter()
        for row in rows:
            result = cache(row["batch"], {"heads": json.dumps(row["candidate"])}, True)
            assert result.scores == row["scores"]
            assert result.trajectories == row["scores"]
        timings.append(time.perf_counter() - start)
    cold = cache.records[: len(rows)]
    return {
        "scope": "archived-score lookup only; no model loaded, no speedup claim",
        "requests": len(rows),
        "logical_examples": sum(len(x["batch"]) for x in rows),
        "cold_cache_hits": sum(x["hit"] for x in cold),
        "cold_physical_lookup_examples": sum(calls),
        "cold_seconds": timings[0],
        "warm_seconds": timings[1],
        "warm_cache_hits": sum(x["hit"] for x in cache.records[len(rows) :]),
        "scores_and_trace_values_exact": True,
    }


if __name__ == "__main__":
    print(json.dumps(replay(Path(__file__).with_name("gepa-completed-seed11.json")), indent=2))
