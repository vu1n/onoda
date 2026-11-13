import json
import unittest
from pathlib import Path


class PilotTests(unittest.TestCase):
    def test_model(self):
        x = json.loads(Path("results.json").read_text())
        self.assertEqual(x["revision"], "607a30d783dfa663caf39e06633721c8d4cfcd7e")

    def test_groups(self):
        x = json.loads(Path("results.json").read_text())
        self.assertEqual(len(x["data"]), 72)
        for split in ["train", "val", "test"]:
            self.assertEqual(sum(r["split"] == split for r in x["data"]), 24)

    def test_controls(self):
        x = json.loads(Path("results.json").read_text())
        for c in x["controls"].values():
            self.assertLess(c["identity_max_error"], 1e-5)
            self.assertLess(c["final_residual_recovery_max_error"], 1e-5)

    def test_search_budget(self):
        x = json.loads(Path("results.json").read_text())
        self.assertEqual(len(x["search"]), 48)
        for seed in [11, 22, 33]:
            self.assertEqual(sum(r["seed"] == seed for r in x["search"]), 16)

    def test_selection(self):
        x = json.loads(Path("results.json").read_text())
        for seed in [11, 22, 33]:
            best = max([r for r in x["search"] if r["seed"] == seed], key=lambda r: r["fraction"])
            self.assertTrue(
                all(r["heads"] == best["heads"] for r in x["results"] if r.get("seed") == seed)
            )

    def test_torchlens(self):
        x = json.loads(Path("torchlens-check.json").read_text())
        self.assertEqual(x["no_op_max_abs_error"], 0)
        self.assertEqual(x["residual_patch_max_abs_error"], 0)
        self.assertEqual(x["head_patch"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
