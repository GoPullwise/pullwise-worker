from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.pipeline.cache import PipelineCache
from codereview.pipeline.candidates import rank_candidates, select_candidates_for_verification
from codereview.pipeline.repo_profile import build_repo_profile
from codereview.simple_review import ReviewUnit, run_review


class AdaptiveReviewPipelineTests(unittest.TestCase):
    def test_simple_review_import_routes_run_review_but_keeps_legacy_helpers(self) -> None:
        self.assertTrue(callable(run_review))
        self.assertEqual(ReviewUnit("unit-0001", "src", ("src/a.py",), 1, 1).unit_id, "unit-0001")

    def test_repo_profile_scores_high_risk_paths(self) -> None:
        profile = build_repo_profile(
            Path("/tmp/repo"),
            [
                {"path": "pullwise_worker/_main_part_02_worker_checkout.py", "size_bytes": 1000},
                {"path": "docs/readme.md", "size_bytes": 10},
            ],
        )
        self.assertIn("python", profile.languages)
        self.assertGreater(profile.risk_for_path("pullwise_worker/_main_part_02_worker_checkout.py"), 0)
        self.assertEqual(profile.risk_for_path("docs/readme.md"), 0)

    def test_candidate_scoring_prioritizes_risk_and_contract_source(self) -> None:
        profile = build_repo_profile(
            Path("/tmp/repo"),
            [{"path": "src/auth_worker.py", "size_bytes": 1000}],
        )
        strong = {
            "candidate_id": "cand-strong",
            "severity": "medium",
            "expected_behavior_source": "documented API contract",
            "trigger_condition": "Call the worker with a cancelled job and pending upload.",
            "reproduction_idea": "Run a local harness.",
            "evidence": [{"file": "src/auth_worker.py"}],
        }
        weak = {"candidate_id": "cand-weak", "severity": "low", "evidence": []}
        ranked = rank_candidates([weak, strong], profile)
        self.assertEqual(ranked[0].candidate["candidate_id"], "cand-strong")
        selected, rejected, summary = select_candidates_for_verification(
            ranked,
            limit=1,
            min_score=20,
            always_repro_severities={"critical", "high"},
        )
        self.assertEqual([item["candidate_id"] for item in selected], ["cand-strong"])
        self.assertTrue(rejected)
        self.assertEqual(summary[0]["candidate_id"], "cand-strong")

    def test_pipeline_cache_key_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = PipelineCache(Path(tmp))
            first = cache.key(engine="engine", source_state={"manifest_hash": "abc"}, mode="fast", scan_mode="full-cached", config={"x": 1})
            second = cache.key(engine="engine", source_state={"manifest_hash": "abc"}, mode="fast", scan_mode="full-cached", config={"x": 1})
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
