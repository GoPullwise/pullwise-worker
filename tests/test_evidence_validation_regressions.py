from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
    fallback_semantic_artifact,
    intent_test_artifact_counts,
    inventory,
    qa_gate_payload,
    validate_phase_outputs,
    validate_reviewer_outputs,
    write_json,
)


def complete_reviewer_finding() -> dict:
    return {
        "id": "finding-1",
        "title": "Incorrect fallback state",
        "severity": "high",
        "confidence": 0.9,
        "failure_scenario": "A request with an empty token reaches the fallback branch.",
        "locations": [{"path": "app.py", "start_line": 1, "end_line": 1}],
        "evidence": ["The empty-token branch returns the stale cached value."],
        "impact": "The caller receives data for the wrong request.",
        "recommendation": "Reject the empty token before reading the cache.",
        "false_positive_risk": "Low; the branch is reachable from the public handler.",
        "next_agent_task": "Add an empty-token regression test and guard the branch.",
    }


def reviewer_payload(finding: dict) -> dict:
    return {
        "schema_version": "codex-reviewer-output/v1",
        "bundle_id": "p1-bundle-001",
        "reviewer": "correctness",
        "reviewed_paths": ["app.py"],
        "review_summary": "Reviewed the request fallback path and its callers.",
        "uncertainties": [],
        "findings": [finding],
    }


def analyzed_result(test_id: str, *, status: str = "failed", classification: str = "confirmed_bug") -> dict:
    return {
        "test_id": test_id,
        "status": status,
        "classification": classification,
        "confidence": 0.95,
        "evidence": ["The generated assertion failed."],
        "artifacts": [],
    }


class EvidenceValidationRegressionsTest(unittest.TestCase):
    def test_semantic_repair_does_not_synthesize_empty_cluster_or_validator_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run-1"
            write_json(
                run_dir / "verified-reviewers" / "correctness.json",
                {"schema_version": "codex-reviewer-output/v1", "findings": [complete_reviewer_finding()]},
            )

            fallback_semantic_artifact(run_dir, {"job_id": "job-1"}, "clustering_and_voting")

            self.assertFalse((run_dir / "clusters.json").exists())
            self.assertFalse((run_dir / "validation-input.json").exists())

            write_json(
                run_dir / "validation-input.json",
                {"schema_version": "validation-input/v1", "candidates": [{"candidate_id": "finding-1"}]},
            )
            fallback_semantic_artifact(run_dir, {"job_id": "job-1"}, "validator_disproof")

            self.assertFalse((run_dir / "validated-findings.json").exists())

    def test_reviewer_finding_with_only_locations_is_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run-1"
            write_json(
                run_dir / "raw-reviewers" / "correctness.json",
                reviewer_payload(
                    {
                        "locations": [
                            {"path": "app.py", "start_line": 1, "end_line": 1}
                        ]
                    }
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "title"):
                validate_reviewer_outputs(run_dir)

            self.assertFalse((run_dir / "verified-reviewers" / "correctness.json").exists())

    def test_reviewer_finding_requires_substantive_evidence_fields(self) -> None:
        required_fields = (
            "id",
            "title",
            "severity",
            "confidence",
            "failure_scenario",
            "evidence",
            "impact",
            "recommendation",
            "false_positive_risk",
            "next_agent_task",
        )
        for field in required_fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp_dir:
                run_dir = Path(tmp_dir) / "run-1"
                finding = complete_reviewer_finding()
                finding.pop(field)
                write_json(
                    run_dir / "raw-reviewers" / "correctness.json",
                    reviewer_payload(finding),
                )

                with self.assertRaisesRegex(RuntimeError, field):
                    validate_reviewer_outputs(run_dir)

    def test_qa_rejects_report_location_past_end_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run-1"
            repo.mkdir(parents=True)
            (repo / "app.py").write_text("print('one line')\n", encoding="utf-8")
            finding = complete_reviewer_finding()
            finding["locations"] = [{"path": "app.py", "start_line": 999, "end_line": 999}]
            write_json(
                run_dir / "report.agent.json",
                {
                    "schema_id": "codex-full-repo-review",
                    "schema_version": "v1",
                    "findings": [finding],
                },
            )
            (run_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            write_json(
                run_dir / "coverage.json",
                {
                    "schema_version": "coverage/v1",
                    "source_like_files_total": 1,
                    "deep_reviewed_files": 1,
                    "standard_reviewed_files": 0,
                    "light_reviewed_files": 0,
                    "inventory_only_files": 0,
                    "skipped_files": 0,
                },
            )
            write_json(run_dir / "token-budget.json", {"schema_version": "token-budget/v1"})
            write_json(run_dir / "inventory.json", inventory(repo))
            write_json(
                run_dir / "validated-findings.json",
                {
                    "schema_version": "validation-output/v1",
                    "validated_findings": [{"id": "finding-1", "status": "confirmed"}],
                    "weak_findings": [],
                    "disproven_findings": [],
                },
            )
            write_json(
                run_dir / "intent" / "intent-test-validation.json",
                {"schema_version": "intent-test-validation/v1", "enabled": False},
            )

            qa = qa_gate_payload(repo, run_dir)

        self.assertTrue(
            any("finding[0]" in error and "line" in error for error in qa["errors"]),
            qa,
        )

    def test_analyzed_intent_ids_must_match_raw_ids_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run-1"
            write_json(
                run_dir / "intent" / "intent-test-results.raw.json",
                {
                    "schema_version": "intent-test-run-results/v1",
                    "test_runs": [
                        {"test_id": "raw-1", "status": "failed"},
                        {"test_id": "raw-2", "status": "failed"},
                    ],
                },
            )
            write_json(
                run_dir / "intent" / "intent-test-results.json",
                {
                    "schema_version": "intent-test-result/v1",
                    "test_results": [analyzed_result("raw-1"), analyzed_result("fabricated")],
                },
            )

            with self.assertRaisesRegex(RuntimeError, "raw process evidence"):
                validate_phase_outputs(run_dir, "intent_test_failure_analysis")

    def test_analyzed_intent_requires_raw_process_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run-1"
            write_json(
                run_dir / "intent" / "intent-test-results.json",
                {
                    "schema_version": "intent-test-result/v1",
                    "test_results": [analyzed_result("fabricated")],
                },
            )

            with self.assertRaisesRegex(RuntimeError, "raw process evidence"):
                validate_phase_outputs(run_dir, "intent_test_failure_analysis")

    def test_passed_raw_intent_run_cannot_be_claimed_as_confirmed_bug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run-1"
            write_json(
                run_dir / "intent" / "intent-test-results.raw.json",
                {
                    "schema_version": "intent-test-run-results/v1",
                    "test_runs": [{"test_id": "raw-pass", "status": "passed"}],
                },
            )
            write_json(
                run_dir / "intent" / "intent-test-results.json",
                {
                    "schema_version": "intent-test-result/v1",
                    "test_results": [analyzed_result("raw-pass")],
                },
            )

            with self.assertRaisesRegex(RuntimeError, "passed raw process"):
                validate_phase_outputs(run_dir, "intent_test_failure_analysis")

    def test_unrelated_intent_ids_cannot_inflate_plan_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run-1"
            write_json(
                run_dir / "intent" / "intent-test-plan.json",
                {
                    "schema_version": "intent-test-plan/v1",
                    "test_targets": [{"test_id": "plan-1"}, {"test_id": "plan-2"}],
                },
            )
            write_json(
                run_dir / "intent" / "intent-test-source.json",
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [{"test_id": "written-x"}, {"test_id": "written-y"}],
                },
            )
            write_json(
                run_dir / "intent" / "intent-test-results.raw.json",
                {
                    "schema_version": "intent-test-run-results/v1",
                    "test_runs": [
                        {"test_id": "raw-x", "status": "failed", "command": "python test_x.py"},
                        {"test_id": "raw-y", "status": "failed", "command": "python test_y.py"},
                    ],
                },
            )
            write_json(
                run_dir / "intent" / "intent-test-results.json",
                {
                    "schema_version": "intent-test-result/v1",
                    "test_results": [analyzed_result("analyzed-x"), analyzed_result("analyzed-y")],
                },
            )

            counts = intent_test_artifact_counts(run_dir)

        self.assertEqual(counts["intent_tests_planned"], 2)
        self.assertEqual(counts["intent_tests_written"], 0)
        self.assertEqual(counts["intent_tests_attempted"], 0)
        self.assertEqual(counts["intent_tests_run"], 0)
        self.assertEqual(counts["intent_tests_asserted"], 0)
        self.assertEqual(counts["intent_tests_analyzed"], 0)


if __name__ == "__main__":
    unittest.main()
