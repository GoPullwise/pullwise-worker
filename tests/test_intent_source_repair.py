from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
    fallback_semantic_artifact,
    read_json,
    repair_intent_test_source_artifact,
    validate_phase_outputs,
    write_json,
)


def _run_dir(root: Path, *target_ids: str) -> Path:
    run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
    write_json(
        run_dir / "intent" / "intent-test-plan.json",
        {
            "schema_version": "intent-test-plan/v1",
            "test_targets": [
                {
                    "test_id": target_id,
                    "title": f"Target {target_id}",
                    "expected_result_before_fix": "fail",
                    "linked_finding_ids": [],
                    "skip_reason": "source-repair fixture",
                }
                for target_id in target_ids
            ],
        },
    )
    return run_dir


class IntentSourceRepairTest(unittest.TestCase):
    def test_fallback_repairs_string_generated_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir), "ITP-001", "ITP-002")
            first_path = "intent/generated-tests/intent-agent-fix-api-base.test.jsx"
            second_path = "intent/generated-tests/intent-review-artifact-url.test.jsx"
            (run_dir / first_path).parent.mkdir(parents=True, exist_ok=True)
            (run_dir / first_path).write_text("test('first', () => {})\n", encoding="utf-8")
            (run_dir / second_path).write_text("test('second', () => {})\n", encoding="utf-8")
            source_path = run_dir / "intent" / "intent-test-source.json"
            write_json(
                source_path,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [first_path, second_path],
                    "tests": [
                        {
                            "test_id": "ITP-001",
                            "path": first_path,
                            "command": ["npm", "test", "--", first_path],
                            "targetIds": ["ITP-001"],
                        },
                        {
                            "test_id": "ITP-002",
                            "path": second_path,
                            "command": ["npm", "test", "--", second_path],
                            "targetIds": ["ITP-002"],
                        },
                    ],
                },
            )

            with self.assertRaisesRegex(RuntimeError, r"generated_tests\[0\] must be an object"):
                validate_phase_outputs(run_dir, "intent_test_writing")

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_test_writing")
            validate_phase_outputs(run_dir, "intent_test_writing")
            payload = read_json(source_path, {})

        self.assertEqual(payload["generated_tests"][0]["test_id"], "ITP-001")
        self.assertEqual(payload["generated_tests"][0]["path"], first_path)
        self.assertEqual(
            payload["generated_tests"][0]["artifact_refs"],
            ["art_intent_test_source"],
        )
        self.assertEqual(
            payload["generated_tests"][1]["command"],
            ["npm", "test", "--", second_path],
        )

    def test_repair_fills_missing_path_from_supporting_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir), "ITP-001")
            test_path = "intent/generated-tests/intent-review-artifact-url.test.jsx"
            (run_dir / test_path).parent.mkdir(parents=True, exist_ok=True)
            (run_dir / test_path).write_text("test('artifact url', () => {})\n", encoding="utf-8")
            source_path = run_dir / "intent" / "intent-test-source.json"
            write_json(
                source_path,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITP-001",
                            "command": ["npm", "test", "--", test_path],
                            "target_test_ids": ["ITP-001"],
                        }
                    ],
                    "tests": [
                        {
                            "test_id": "ITP-001",
                            "test_file": test_path,
                            "framework": "vitest",
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(RuntimeError, r"generated_tests\[0\].path is missing"):
                validate_phase_outputs(run_dir, "intent_test_writing")

            repair_intent_test_source_artifact(source_path, run_dir)
            validate_phase_outputs(run_dir, "intent_test_writing")
            payload = read_json(source_path, {})

        self.assertEqual(payload["generated_tests"][0]["path"], test_path)
        self.assertEqual(payload["generated_tests"][0]["framework"], "vitest")

    def test_repair_infers_single_materialized_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir), "ITP-001")
            test_path = "intent/generated-tests/intent-generated.test.py"
            (run_dir / test_path).parent.mkdir(parents=True, exist_ok=True)
            (run_dir / test_path).write_text("def test_generated():\n    assert True\n", encoding="utf-8")
            source_path = run_dir / "intent" / "intent-test-source.json"
            write_json(
                source_path,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "target_test_ids": ["ITP-001"],
                            "command": ["python", "-m", "pytest", test_path],
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(RuntimeError, r"generated_tests\[0\].path is missing"):
                validate_phase_outputs(run_dir, "intent_test_writing")

            repair_intent_test_source_artifact(source_path, run_dir)
            validate_phase_outputs(run_dir, "intent_test_writing")
            payload = read_json(source_path, {})

        self.assertEqual(payload["generated_tests"][0]["path"], test_path)


if __name__ == "__main__":
    unittest.main()
