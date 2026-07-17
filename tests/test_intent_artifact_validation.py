from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
    intent_test_plan_errors,
    intent_test_source_errors,
    read_json,
    repair_intent_test_source_artifact,
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
    write_json(
        run_dir / "intent" / "intent-test-plan.json",
        {
            "schema_version": "intent-test-plan/v1",
            "test_targets": [{"test_id": "ITP-001"}],
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

    def test_plan_execution_candidate_requires_command_and_cwd(self) -> None:
        cases = (
            (
                {"command": [], "cwd": "."},
                "intent-test-plan.json test_targets[0].execution_candidates[0].command is missing or empty",
            ),
            (
                {"command": ["python3", "-m", "unittest"], "cwd": ""},
                "intent-test-plan.json test_targets[0].execution_candidates[0].cwd is missing or empty",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            for candidate, expected_error in cases:
                with self.subTest(candidate=candidate):
                    errors = intent_test_plan_errors(
                        run_dir,
                        {
                            "schema_version": "intent-test-plan/v1",
                            "test_targets": [
                                _plan_target(execution_candidates=[candidate])
                            ],
                        },
                    )
                    self.assertIn(expected_error, errors)

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

    def test_generated_source_rejects_unknown_target_test_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            path = _write_generated_test(run_dir)
            errors = intent_test_source_errors(
                run_dir,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "path": path,
                            "command": ["python3", "-m", "unittest", path],
                            "target_test_ids": ["ITP-999"],
                        }
                    ],
                },
            )

        self.assertIn(
            "intent-test-source.json generated_tests[0].target_test_ids references unknown plan target ITP-999",
            errors,
        )

    def test_generated_source_rejects_target_when_plan_is_missing_or_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            path = _write_generated_test(run_dir)
            plan_path = run_dir / "intent" / "intent-test-plan.json"
            payload = {
                "schema_version": "intent-test-source/v1",
                "generated_tests": [
                    {
                        "test_id": "ITV-001",
                        "path": path,
                        "command": ["python3", "-m", "unittest", path],
                        "target_test_ids": ["ITP-001"],
                    }
                ],
            }
            cases = (
                None,
                {"schema_version": "intent-test-plan/v1", "test_targets": None},
            )
            for plan_payload in cases:
                with self.subTest(plan_payload=plan_payload):
                    if plan_payload is None:
                        plan_path.unlink(missing_ok=True)
                    else:
                        write_json(plan_path, plan_payload)
                    errors = intent_test_source_errors(run_dir, payload)
                    self.assertIn(
                        "intent-test-source.json generated_tests[0].target_test_ids references unknown plan target ITP-001",
                        errors,
                    )

    def test_source_repair_does_not_invent_target_link_without_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            (run_dir / "intent" / "intent-test-plan.json").unlink()
            path = _write_generated_test(run_dir)
            source_path = run_dir / "intent" / "intent-test-source.json"
            write_json(
                source_path,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "path": path,
                            "command": ["python3", "-m", "unittest", path],
                        }
                    ],
                },
            )

            repair_intent_test_source_artifact(source_path, run_dir)
            repaired = read_json(source_path, {})
            errors = intent_test_source_errors(run_dir, repaired)

        self.assertNotIn("target_test_ids", repaired["generated_tests"][0])
        self.assertIn(
            "intent-test-source.json generated_tests[0].target_test_ids is missing or empty",
            errors,
        )

    def test_generated_source_rejects_empty_normalized_command_and_target_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = _run_dir(Path(tmp_dir))
            path = _write_generated_test(run_dir)
            errors = intent_test_source_errors(
                run_dir,
                {
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "path": path,
                            "command": [{}],
                            "target_test_ids": ["", None, 123],
                        }
                    ],
                },
            )

        self.assertIn(
            "intent-test-source.json generated_tests[0].command is missing or empty",
            errors,
        )
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
