from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from pullwise_worker.review_worker_v1 import ReviewWorkerV1, write_json


ROOT = Path(__file__).resolve().parents[1]


def reviewer_job() -> dict:
    return {
        "model_profile": {
            "default_model": "gpt-5.5",
            "core_effort": "high",
            "non_core_effort": "medium",
        },
        "review_request": {
            "budget": {"max_wall_time_seconds": 14_400},
            "policy": {
                "allow_source_modification": False,
                "allow_dependency_install": False,
                "allow_network": False,
                "helper_scripts_standard_library_only": True,
                "turn_timeout_seconds": 1_800,
                "max_bundles": 24,
                "max_reviewer_assignments": 48,
                "reviewer_concurrency": 2,
            },
        },
        "repositoryLimits": {"maxFiles": 2_000, "maxBytes": 50 * 1024 * 1024},
    }


def reviewer_payload(*, with_location: bool) -> dict:
    finding = {
        "id": "finding-001",
        "title": "Concrete behavior issue",
        "severity": "medium",
        "confidence": 0.8,
        "failure_scenario": "A valid request reaches the incorrect fallback branch.",
        "evidence": ["The branch returns a value that violates the documented contract."],
        "impact": "The caller receives an incorrect result.",
        "recommendation": "Guard the fallback branch and add a regression test.",
        "false_positive_risk": "Low; the branch and contract are both present in the bundle.",
        "next_agent_task": "Fix the fallback guard and cover the request path.",
    }
    if with_location:
        finding["locations"] = [
            {"path": "src/service.py", "start_line": 12, "end_line": 18}
        ]
    return {
        "schema_version": "codex-reviewer-output/v1",
        "bundle_id": "p2-bundle-008",
        "reviewer": "correctness_lite",
        "reviewed_paths": ["src/service.py"],
        "review_summary": "Reviewed the assigned source path.",
        "uncertainties": [],
        "findings": [finding],
    }


class OutputContractCheckTest(unittest.TestCase):
    def test_reviewer_validation_allows_one_bounded_followup_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            raw_path = run_dir / "raw-reviewers" / "p2-bundle-008.correctness-lite.json"
            raw_path.parent.mkdir(parents=True)
            write_json(run_dir / "run-state.json", {"thread_id": "thread_1"})
            write_json(raw_path, reviewer_payload(with_location=False))

            class RepairingOnSecondTurn:
                def __init__(self) -> None:
                    self.calls = 0

                def run_turn(self, **_kwargs: object) -> SimpleNamespace:
                    self.calls += 1
                    write_json(
                        raw_path,
                        reviewer_payload(with_location=self.calls == 2),
                    )
                    return SimpleNamespace(duration_ms=5)

            codex = RepairingOnSecondTurn()
            worker = ReviewWorkerV1(
                SimpleNamespace(worker_id="wk_1", service_home=str(root)),
                client=object(),
            )

            worker.run_reviewer_json_validation_phase(
                codex,
                repo,
                run_dir,
                reviewer_job(),
            )

            validation = json.loads(
                (run_dir / "json-errors.json").read_text(encoding="utf-8")
            )

        self.assertEqual(codex.calls, 2)
        self.assertEqual(validation["errors"], [])

    def test_output_contract_fixture_tool_passes_the_checked_corpus(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check_output_contracts.py"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4 output contract cases passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
