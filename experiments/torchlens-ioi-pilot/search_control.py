"""Local pilot controls. No model imports, persistent cache, or changed scoring budget."""

import copy
import json
import time


def parse(text):
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    values = json.loads(text)
    if not isinstance(values, list):
        raise ValueError("expected a JSON list")
    values = [int(v) if isinstance(v, str) and v.isascii() and v.isdigit() else v for v in values]
    if len(values) != 3 or not all(type(v) is int and 0 <= v < 144 for v in values):
        raise ValueError("expected three head IDs in 0..143")
    if len(set(values)) != 3:
        raise ValueError("duplicate head")
    return values


class ProposalGuard:
    """GEPA's empty mapping skips child evaluation; an independent cap prevents spinning."""

    def __init__(self, generate, max_attempts=30):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.generate = generate
        self.max_attempts = max_attempts
        self.records = []

    def stopped(self, _state):
        return len(self.records) >= self.max_attempts

    def __call__(self, candidate, reflective_dataset, components_to_update):
        if self.stopped(None):
            return {}
        old = parse(candidate["heads"])
        start = time.perf_counter()
        record = {"status": "invalid", "normalized": None}
        # Append before the call: provider errors count toward the attempt cap too.
        self.records.append(record)
        try:
            ids = parse(self.generate(candidate, reflective_dataset, components_to_update))
            record["normalized"] = ids
            record["status"] = "unchanged" if set(ids) == set(old) else "new"
        except (ValueError, TypeError, AttributeError):
            return {}
        except Exception:
            record["status"] = "generation_error"
            raise
        finally:
            record["seconds"] = time.perf_counter() - start
        return {"heads": json.dumps(ids)} if record["status"] == "new" else {}


class BatchCache:
    """Exact ordered batches only. One instance binds one immutable evaluator/data snapshot."""

    def __init__(self, evaluate):
        self.evaluate = evaluate
        self._values = {}
        self.records = []

    def __call__(self, batch, candidate, capture_traces=False):
        # Keep head order too: harmless permutations are rejected upstream, not cache-normalized.
        key = (tuple(parse(candidate["heads"])), tuple(tuple(x) for x in batch))
        start = time.perf_counter()
        hit = key in self._values
        if not hit:
            result = self.evaluate(batch, candidate, True)
            if len(result.scores) != len(batch) or len(result.outputs) != len(batch):
                raise ValueError("evaluation lost batch alignment")
            self._values[key] = copy.deepcopy(result)
        result = copy.deepcopy(self._values[key])
        if not capture_traces:
            result.trajectories = None
        # Leave GEPA's logical metric-call accounting unchanged on cache hits.
        self.records.append(
            {"hit": hit, "examples": len(batch), "seconds": time.perf_counter() - start}
        )
        return result
