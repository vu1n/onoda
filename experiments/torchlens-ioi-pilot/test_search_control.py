import ast
import json
import tempfile
import unittest
from pathlib import Path

import gepa
from gepa.core.adapter import EvaluationBatch
from search_control import BatchCache, ProposalGuard, parse


class ControlsTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse('["1", 2, 3]'), [1, 2, 3])
        for text in ["{}", "[true,2,3]", "[1,1,3]", "[1,2,144]", "[1,2]", "oops"]:
            with self.assertRaises((ValueError, TypeError)):
                parse(text)

    def test_invalid_is_not_parent(self):
        guard = ProposalGuard(lambda *args: "oops", 3)
        for _ in range(10):
            self.assertEqual(guard({"heads": "[1,2,3]"}, {}, []), {})
        self.assertEqual(len(guard.records), 3)
        self.assertTrue(guard.stopped(None))
        self.assertTrue(all(r["status"] == "invalid" for r in guard.records))

    def test_unchanged_permutation(self):
        guard = ProposalGuard(lambda *args: '[3,"1",2]')
        self.assertEqual(guard({"heads": "[1,2,3]"}, {}, []), {})
        self.assertEqual(guard.records[0]["status"], "unchanged")

    def test_new(self):
        guard = ProposalGuard(lambda *args: "[3,4,5]")
        self.assertEqual(json.loads(guard({"heads": "[1,2,3]"}, {}, [])["heads"]), [3, 4, 5])

    def test_generation_failure_counts(self):
        def fail(*args):
            raise RuntimeError("local test failure")

        guard = ProposalGuard(fail, 1)
        with self.assertRaises(RuntimeError):
            guard({"heads": "[1,2,3]"}, {}, [])
        self.assertTrue(guard.stopped(None))
        self.assertEqual(guard.records[0]["status"], "generation_error")

    def test_cache_order_scope_and_traces(self):
        calls = []

        def evaluator(batch, candidate, capture):
            calls.append((batch, candidate))
            rows = [{"case": x, "value": i} for i, x in enumerate(batch)]
            return EvaluationBatch(rows, list(range(len(batch))), rows)

        cache = BatchCache(evaluator)
        batch = [("val", 2), ("train", 1), ("val", 2)]
        c = {"heads": "[1,2,3]"}
        a = cache(batch, c)
        self.assertIsNone(a.trajectories)
        a.outputs[0]["value"] = 99
        b = cache(batch, c, True)
        self.assertEqual(b.outputs[0]["value"], 0)
        self.assertEqual([r["case"] for r in b.trajectories], batch)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(b.num_metric_calls)
        cache(list(reversed(batch[:2])), c)
        cache(batch, {"heads": "[1,2,4]"})
        BatchCache(evaluator)(batch, c)
        self.assertEqual(len(calls), 4)

    def test_archive_replay_exact(self):
        from replay_cache import replay

        report = replay(Path(__file__).with_name("gepa-completed-seed11.json"))
        self.assertEqual(report["requests"], 61)
        self.assertEqual(report["logical_examples"], 128)
        self.assertEqual(report["cold_cache_hits"], 36)
        self.assertEqual(report["cold_physical_lookup_examples"], 56)
        self.assertEqual(report["warm_cache_hits"], 61)

    def test_baseline_export_only_in_main(self):
        tree = ast.parse(Path(__file__).with_name("pilot.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        exports = [
            n
            for n in ast.walk(main)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "dump"
        ]
        self.assertEqual(len(exports), 1)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.If)):
                continue
            self.assertFalse(
                any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "dump"
                    for n in ast.walk(node)
                )
            )

    def test_bad_evaluation_not_cached(self):
        c = BatchCache(lambda *args: EvaluationBatch([], []))
        with self.assertRaises(ValueError):
            c([("train", 0)], {"heads": "[1,2,3]"})
        self.assertEqual(c._values, {})

    def test_real_gepa_invalid_unchanged_and_new(self):
        for text, status in [("oops", "invalid"), ("[3,2,1]", "unchanged"), ("[4,5,6]", "new")]:
            seen = []

            class Adapter:
                propose_new_texts = None

                def evaluate(self, batch, candidate, capture_traces=False, seen=seen):
                    ids = parse(candidate["heads"])
                    seen.append(ids)
                    scores = [sum(ids) / 1000] * len(batch)
                    return EvaluationBatch(scores, scores, scores if capture_traces else None)

                def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
                    return {"heads": [{"feedback": "test fixture"}]}

            guard = ProposalGuard(lambda *args, text=text: text, 3)
            with tempfile.TemporaryDirectory() as run:
                result = gepa.optimize(
                    seed_candidate={"heads": "[1,2,3]"},
                    trainset=[("train", i) for i in range(4)],
                    valset=[("val", i) for i in range(4)],
                    adapter=Adapter(),
                    custom_candidate_proposer=guard,
                    stop_callbacks=guard.stopped,
                    max_metric_calls=100,
                    reflection_minibatch_size=2,
                    skip_perfect_score=False,
                    run_dir=run,
                    display_progress_bar=False,
                    seed=11,
                )
            self.assertEqual(len(guard.records), 3)
            self.assertEqual(guard.records[0]["status"], status)
            self.assertEqual(
                parse(result.best_candidate["heads"]), [4, 5, 6] if status == "new" else [1, 2, 3]
            )
            if status != "new":
                self.assertTrue(all(ids == [1, 2, 3] for ids in seen))
                # Only initial validation and 3 parent minibatches, never fake children.
                self.assertEqual(len(seen), 4)


if __name__ == "__main__":
    unittest.main()
