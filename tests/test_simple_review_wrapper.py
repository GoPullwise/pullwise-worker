from __future__ import annotations

import unittest

import codereview.simple_review as simple_review


class SimpleReviewWrapperTests(unittest.TestCase):
    def test_legacy_helpers_remain_available(self) -> None:
        unit = simple_review.ReviewUnit("unit-0001", "src", ("src/a.py",), 10, 2)
        self.assertEqual(unit.unit_id, "unit-0001")
        self.assertTrue(callable(simple_review.run_review))

    def test_risk_ordering_prioritizes_worker_paths(self) -> None:
        risky = simple_review.ReviewUnit("unit-0001", "src", ("src/auth_worker.py",), 10, 2)
        plain = simple_review.ReviewUnit("unit-0002", "src", ("src/view.py",), 10, 2)
        risky_batch = simple_review.DiscoveryBatch("discovery-002", (risky,), ((risky.unit_id,),))
        plain_batch = simple_review.DiscoveryBatch("discovery-001", (plain,), ((plain.unit_id,),))
        ordered = sorted([plain_batch, risky_batch], key=simple_review._batch_sort_key)
        self.assertEqual(ordered[0].batch_id, "discovery-002")

    def test_candidate_scoring_prioritizes_concrete_reproducible_findings(self) -> None:
        strong = {
            "candidate_id": "cand-strong",
            "severity": "medium",
            "expected_behavior_source": "documented API behavior",
            "reproduction_idea": "run a local harness",
            "evidence": [{"file": "src/auth_worker.py"}],
        }
        weak = {"candidate_id": "cand-weak", "severity": "low", "evidence": []}
        selected, rejected = simple_review._select_candidates([weak, strong], 1)
        self.assertEqual(selected[0]["candidate_id"], "cand-strong")
        self.assertEqual(rejected[0]["candidate_id"], "cand-weak")


if __name__ == "__main__":
    unittest.main()
