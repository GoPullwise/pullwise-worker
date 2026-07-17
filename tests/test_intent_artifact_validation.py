from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
    intent_test_plan_errors,
    intent_test_source_errors,
    write_json,
)


def _run_dir(root: Path) -> Path:
    run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
    write_json(
        run_dir / "clusters.json",
        {
            "schema_version": "cluster-output/v1",
            "clusters": [{"cluster_id": "CL-001"}],
        },
    )
    return run_dir


def _plan_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "test_id": "ITP-001",
        "title": "Exercise the selected contract",
        "expected_result_before_fix": "fail",
        "linked_finding_ids": ["CL-001"],
    }
    target.update(overrides)
    return target


def _write_generated_test(run_dir: Path, name: str = "test_intent.py") -> str:
    relative = f"intent/generated-tests/{name}"
    path = run_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# generated intent test\n", encoding="utf-8")
    return relative


class IntentArtifactValidationTest(unittest.TestCase):
    def test_plan_target_requires_execution_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            payload = {
                "schema_version": "intent-test-plan/v1",
                "test_targets": [_plan_target()],
            }

            errors = intent_test_plan_errors(run_dir, payload)

        self.assertIn(
            "intent-test-plan.json test_targets[0].execution_candidates is missing or empty",
            errors,
        )

    def test_generated_source_requires_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            path = _write_generated_test(run_dir)
            payload = {
                "schema_version": "intent-test-source/v1",
                "generated_tests": [
                    {
                        "test_id": "ITV-001",
                        "target_test_ids": ["ITP-001"],
                        "path": path,
                    }
                ],
            }

            errors = intent_test_source_errors(run_dir, payload)

        self.assertIn(
            "intent-test-source.json generated_tests[0].command is missing or empty",
            errors,
        )

    def test_generated_source_requires_target_test_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            path = _write_generated_test(run_dir)
            payload = {
                "schema_version": "intent-test-source/v1",
                "generated_tests": [
                    {
                        "test_id": "ITV-001",
                        "path": path,
                        "command": ["python3", "-m", "unittest", path],
                    }
                ],
            }

            errors = intent_test_source_errors(run_dir, payload)

        self.assertIn(
            "intent-test-source.json generated_tests[0].target_test_ids is missing or empty",
            errors,
        )

    def test_plan_accepts_canonical_alias_skipped_and_empty_targets(self) -> None:
        valid_targets = (
            _plan_target(
                execution_candidates=[
                    {
                        "command": ["python3", "-m", "unittest", "test_intent.py"],
                        "cwd": ".",
                    }
                ]
            ),
            _plan_target(
                executionCandidates=[
                    {
                        "testCommand": "python3 -m unittest test_intent.py",
                        "working_directory": ".",
                    }
                ]
            ),
            _plan_target(skipReason="no faithful runnable strategy is available"),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            for target in valid_targets:
                with self.subTest(target=target):
                    errors = intent_test_plan_errors(
                        run_dir,
                        {
                            "schema_version": "intent-test-plan/v1",
                            "test_targets": [target],
                        },
                    )
                    self.assertEqual(errors, [])
            empty_errors = intent_test_plan_errors(
                run_dir,
                {
                    "schema_version": "intent-test-plan/v1",
                    "test_targets": [],
                    "skip_reason": "no P0/P1 target was selected",
                },
            )

        self.assertEqual(empty_errors, [])

    def test_source_accepts_known_aliases_explicit_skip_and_empty_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            path = _write_generated_test(run_dir)
            valid_records = (
                {
                    "test_id": "ITV-001",
                    "path": path,
                    "command": ["python3", "-m", "unittest", path],
                    "target_test_ids": ["ITP-001"],
                },
                {
                    "test_id": "ITV-002",
                    "path": path,
                    "runCommand": f"python3 -m unittest {path}",
                    "targetIds": ["ITP-001"],
                },
                {
                    "test_id": "ITV-003",
                    "path": path,
                    "targetTestIds": ["ITP-001"],
                    "skippedReason": "no faithful runnable command is available",
                },
            )
            for record in valid_records:
                with self.subTest(record=record):
                    errors = intent_test_source_errors(
                        run_dir,
                        {
                            "schema_version": "intent-test-source/v1",
                            "generated_tests": [record],
                        },
                    )
                    self.assertEqual(errors, [])
            empty_errors = intent_test_source_errors(
                run_dir,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [],
                    "skip_reason": "no intent test was selected",
                },
            )

        self.assertEqual(empty_errors, [])


if __name__ == "__main__":
    unittest.main()
