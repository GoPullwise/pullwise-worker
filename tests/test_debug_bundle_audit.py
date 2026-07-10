from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pullwise_worker.debug_bundle_audit import audit_bundle


def write_json(root: Path, relative: str, payload: object) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def issue_codes(result: dict) -> set[str]:
    return {str(issue.get("code") or "") for issue in result.get("issues", [])}


class DebugBundleAuditTest(unittest.TestCase):
    def write_good_bundle(self, root: Path) -> None:
        run = root / "worker" / "run"
        write_json(
            root,
            "worker/debug-summary.json",
            {"schema_version": "pullwise-debug-bundle/v1", "run_id": "run_1", "status": "completed", "error": ""},
        )
        write_json(
            root,
            "worker/run/codex-runtime.json",
            {
                "schema_version": "codex-runtime/v1",
                "mode": "managed_standalone",
                "python_sdk_version": "0.1.0b3",
                "sdk_bundled_cli_version": "0.137.0a4",
                "configured_cli_version": "codex-cli 0.144.1",
            },
        )
        write_json(
            root,
            "worker/run/progress.json",
            {
                "run_id": "run_1",
                "status": "completed",
                "counters": {
                    "source_like_files_total": 2,
                    "source_like_files_classified": 2,
                    "bundles_total": 1,
                    "bundles_packed": 1,
                    "intent_tests_total": 1,
                    "intent_tests_written": 1,
                    "intent_tests_run": 1,
                    "validator_candidates_total": 1,
                    "validator_candidates_completed": 1,
                },
            },
        )
        write_json(
            root,
            "worker/run/inventory.json",
            {
                "schema_version": "inventory/v1",
                "summary": {"source_like_files": 2},
                "files": [
                    {"path": "src/a.py", "is_source_like": True},
                    {"path": "src/b.py", "is_source_like": True},
                ],
            },
        )
        write_json(
            root,
            "worker/run/risk-routing.json",
            {
                "schema_version": "risk-routing/v1",
                "routes": [{"path": "src/a.py", "tier": "P0"}, {"path": "src/b.py", "tier": "P1"}],
            },
        )
        write_json(
            root,
            "worker/run/coverage.json",
            {
                "schema_version": "coverage/v1",
                "source_like_files_total": 2,
                "deep_reviewed_files": 1,
                "standard_reviewed_files": 1,
                "light_reviewed_files": 0,
                "inventory_only_files": 0,
                "skipped_files": 0,
            },
        )
        write_json(
            root,
            "worker/run/bundle-plan.json",
            {"schema_version": "bundle-plan/v1", "bundles": [{"bundle_id": "b1", "paths": ["src/a.py", "src/b.py"]}]},
        )
        (run / "bundles").mkdir(parents=True)
        (run / "bundles" / "b1.md").write_text("bundle", encoding="utf-8")
        write_json(
            root,
            "worker/run/report.agent.json",
            {
                "schema_id": "codex-full-repo-review",
                "schema_version": "v1",
                "findings": [
                    {
                        "id": "finding-1",
                        "title": "Example",
                        "severity": "medium",
                        "locations": [{"path": "src/a.py", "start_line": 1, "end_line": 2}],
                        "validation_sources": {"cluster": {"cluster_id": "candidate-1"}},
                    }
                ],
            },
        )
        write_json(
            root,
            "worker/run/location-verification.json",
            {
                "schema_version": "location-verification/v1",
                "summary": {"locations_total": 1, "valid_locations": 1},
                "locations": [{"finding_id": "finding-1", "path": "src/a.py", "start_line": 1, "end_line": 2, "valid": True}],
            },
        )
        write_json(
            root,
            "worker/run/validation-input.json",
            {"schema_version": "validation-input/v1", "candidates": [{"candidate_id": "candidate-1"}]},
        )
        write_json(
            root,
            "worker/run/validated-findings.json",
            {
                "schema_version": "validation-output/v1",
                "validated_findings": [{"candidate_id": "candidate-1", "status": "confirmed"}],
            },
        )
        write_json(
            root,
            "worker/run/intent/intent-test-plan.json",
            {"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "intent-1"}]},
        )
        write_json(
            root,
            "worker/run/intent/intent-test-source.json",
            {
                "schema_version": "intent-test-source/v1",
                "generated_tests": [
                    {
                        "test_id": "ITV-001",
                        "test_ids": ["intent-1"],
                        "path": "test_intent.py",
                        "test_framework": "unittest",
                        "command": ["python", "-m", "unittest", "test_intent.py"],
                    }
                ],
            },
        )
        write_json(
            root,
            "worker/run/intent/intent-test-results.raw.json",
            {
                "schema_version": "intent-test-run-results/v1",
                "test_runs": [
                    {
                        "test_id": "ITV-001",
                        "target_test_ids": ["intent-1"],
                        "status": "passed",
                        "command": "python -m unittest test_intent.py",
                    }
                ],
            },
        )
        write_json(root, "worker/run/qa.json", {"schema_version": "qa/v1", "status": "pass", "errors": [], "warnings": []})

    def test_valid_directory_and_zip_bundle_have_no_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            directory_result = audit_bundle(root)
            zip_path = Path(tmp_dir) / "bundle.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                for path in root.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
            zip_result = audit_bundle(zip_path)

        self.assertEqual(directory_result["summary"]["errors"], 0, directory_result["issues"])
        self.assertEqual(zip_result["summary"]["errors"], 0, zip_result["issues"])
        self.assertEqual(directory_result["facts"]["terminal_status"], "completed")
        self.assertEqual(directory_result["facts"]["findings"], 1)

    def test_live_failure_shapes_are_reported_with_actionable_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "broken"
            self.write_good_bundle(root)
            write_json(
                root,
                "worker/debug-summary.json",
                {
                    "schema_version": "pullwise-debug-bundle/v1",
                    "run_id": "run_1",
                    "status": "completed",
                    "error": "",
                },
            )
            progress = json.loads((root / "worker/run/progress.json").read_text(encoding="utf-8"))
            progress["status"] = "partial_completed"
            progress["counters"] = {key: 0 for key in progress["counters"]}
            write_json(root, "worker/run/progress.json", progress)
            report = json.loads((root / "worker/run/report.agent.json").read_text(encoding="utf-8"))
            report["findings"][0]["locations"] = [{"path": "src/a.py", "start_line": 20, "end_line": 3}]
            write_json(root, "worker/run/report.agent.json", report)
            write_json(
                root,
                "worker/run/location-verification.json",
                {"schema_version": "location-verification/v1", "summary": {"locations_total": 0}, "locations": []},
            )
            write_json(
                root,
                "worker/run/intent/intent-test-plan.json",
                {
                    "schema_version": "intent-test-plan/v1",
                    "test_targets": [{"test_id": "intent-a"}, {"test_id": "intent-b"}],
                },
            )
            write_json(
                root,
                "worker/run/intent/intent-test-source.json",
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "test_ids": ["intent-a", "intent-b"],
                            "path": ".codex-review/generated-tests/test_protocol.py",
                            "test_framework": "unittest",
                        }
                    ],
                    "test_commands": [
                        {"command": "python3 -m unittest .codex-review/generated-tests/test_protocol.py"}
                    ],
                },
            )
            write_json(
                root,
                "worker/run/intent/intent-test-results.raw.json",
                {
                    "schema_version": "intent-test-run-results/v1",
                    "test_runs": [
                        {"test_id": "intent-a", "status": "skipped", "skip_reason": "no generated test command was produced"},
                        {"test_id": "intent-b", "status": "skipped", "skip_reason": "no generated test command was produced"},
                        {
                            "test_id": "ITV-001",
                            "status": "skipped",
                            "classification": "dependency_missing",
                            "skip_reason": "pytest is not available",
                        },
                    ],
                },
            )
            (root / "worker/run/worker.log.jsonl").write_text(
                json.dumps(
                    {
                        "event": "job_failed",
                        "error": "The 'gpt-5.6-sol' model requires a newer version of Codex.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = audit_bundle(root)

        codes = issue_codes(result)
        self.assertEqual(result["facts"]["intent_tests"], {"total": 2, "written": 2, "run": 2})
        self.assertTrue(
            {
                "debug_status_mismatch",
                "report_location_reversed",
                "location_verification_missing",
                "intent_duplicate_plan_execution",
                "intent_unittest_routed_to_pytest",
                "progress_counter_mismatch",
                "codex_runtime_too_old",
            }.issubset(codes),
            result["issues"],
        )

    def test_cross_artifact_plausible_and_severity_mismatches_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            report = json.loads((root / "worker/run/report.agent.json").read_text(encoding="utf-8"))
            report["findings"][0].update(
                {
                    "severity": "P1",
                    "validator_status": "plausible",
                }
            )
            write_json(root, "worker/run/report.agent.json", report)
            write_json(
                root,
                "worker/run/validated-findings.json",
                {
                    "schema_version": "validation-output/v1",
                    "validated_findings": [
                        {
                            "id": "finding-1",
                            "title": "Example",
                            "status": "plausible",
                            "locations": [{"path": "src/a.py", "start_line": 1, "end_line": 2}],
                        }
                    ],
                },
            )
            write_json(
                root,
                "server/server-debug-evidence.json",
                {
                    "review_run": {
                        "summary_json": json.dumps(
                            {
                                "finding_counts": {
                                    "confirmed_critical": 0,
                                    "confirmed_high": 1,
                                    "confirmed_medium": 0,
                                    "confirmed_low": 0,
                                    "plausible": 0,
                                },
                                "top_findings": [
                                    {
                                        "id": "finding-1",
                                        "title": "Example",
                                        "severity": "P1",
                                        "validator_status": "plausible",
                                    }
                                ],
                            }
                        )
                    },
                    "scan": {
                        "issues": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
                        "humanReport": {"summaryMarkdown": "- Confirmed findings: 1 (P1 1)"},
                    },
                },
            )

            result = audit_bundle(root)

        codes = issue_codes(result)
        self.assertIn("noncanonical_finding_severity", codes)
        self.assertIn("result_validation_count_mismatch", codes)
        self.assertIn("server_issue_count_mismatch", codes)
        self.assertIn("human_report_validation_mismatch", codes)

    def test_unbacked_main_finding_is_not_defaulted_to_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            write_json(
                root,
                "worker/run/validated-findings.json",
                {
                    "schema_version": "validation-output/v1",
                    "validated_findings": [{"candidate_id": "different", "status": "rejected"}],
                },
            )

            result = audit_bundle(root)

        issues = {str(issue.get("code")): issue for issue in result["issues"]}
        self.assertIn("main_finding_validation_missing", issues)
        self.assertEqual(result["facts"]["confirmed_findings"], 0)

    def test_noncanonical_validation_collection_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            write_json(
                root,
                "worker/run/validated-findings.json",
                {
                    "schema_version": "validation-output/v1",
                    "validated": [{"candidate_id": "candidate-1", "validation_status": "confirmed"}],
                },
            )

            result = audit_bundle(root)

        self.assertIn("validation_findings_collection_noncanonical", issue_codes(result))

    def test_intent_classification_summary_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            write_json(
                root,
                "worker/run/intent/intent-test-results.json",
                {
                    "schema_version": "intent-test-result/v1",
                    "summary": {"classification_counts": {"test_harness_error": 1}},
                    "test_results": [
                        {"test_id": "ITV-001", "status": "failed", "classification": "dependency_missing"}
                    ],
                },
            )

            result = audit_bundle(root)

        self.assertIn("intent_classification_summary_mismatch", issue_codes(result))

    def test_reviewer_assignment_progress_and_post_failures_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            write_json(
                root,
                "worker/run/bundle-plan.json",
                {
                    "schema_version": "bundle-plan/v1",
                    "bundles": [
                        {"bundle_id": "b1", "paths": ["src/a.py", "src/b.py"], "reviewers": ["correctness", "test_gap"]}
                    ],
                },
            )
            write_json(
                root,
                "worker/run/raw-reviewers/correctness.json",
                {
                    "schema_version": "codex-reviewer-output/v1",
                    "reviewer": "correctness",
                    "bundles_reviewed": ["bundles/b1.md"],
                    "findings": [],
                },
            )
            progress = json.loads((root / "worker/run/progress.json").read_text(encoding="utf-8"))
            progress["counters"].update({"reviewer_runs_total": 1, "reviewer_runs_completed": 1})
            write_json(root, "worker/run/progress.json", progress)
            (root / "worker/run/worker.log.jsonl").write_text(
                json.dumps({"event": "progress_event_post_failed", "phase": "cleanup_active_job"}) + "\n",
                encoding="utf-8",
            )

            result = audit_bundle(root)

        codes = issue_codes(result)
        self.assertIn("reviewer_coverage_incomplete", codes)
        self.assertIn("progress_counter_mismatch", codes)
        self.assertIn("progress_event_post_failed", codes)

    def test_fully_degraded_intent_evidence_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "bundle"
            self.write_good_bundle(root)
            write_json(
                root,
                "worker/run/intent/intent-test-results.json",
                {
                    "schema_version": "intent-test-result/v1",
                    "test_results": [
                        {
                            "test_id": "intent-1",
                            "status": "skipped",
                            "classification": "dependency_missing",
                            "confidence": 0.0,
                        }
                    ],
                },
            )

            result = audit_bundle(root)

        self.assertIn("intent_evidence_fully_degraded", issue_codes(result))


if __name__ == "__main__":
    unittest.main()
