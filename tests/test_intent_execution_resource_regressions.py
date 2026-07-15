from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.bundle_planning_fixtures import materialize_test_bundle_plan
from pullwise_worker.review_worker_v1 import (
    MAX_BUNDLE_ESTIMATED_TOKENS,
    ReviewWorkerV1,
    ensure_immutable_inventory_baseline,
    intent_command_is_runnable_for_repo,
    intent_execution_preflight,
    intent_runtime_repair_diagnostics,
    intent_test_source_preflight_payload,
    pack_bundles,
    run_intent_tests,
    write_json,
)


def _write_intent_run(
    root: Path,
    *,
    source: dict[str, object],
    plan: dict[str, object] | None = None,
    total_seconds: int = 60,
) -> tuple[Path, Path]:
    repo = root / "repo"
    run_dir = repo / ".codex-review" / "runs" / "run_1"
    validation_repo = root / "validation-repo"
    validation_repo.mkdir(parents=True)
    write_json(
        run_dir / "intent" / "validation-workspace.json",
        {
            "schema_version": "validation-workspace/v1",
            "validation_repo_root": str(validation_repo),
            "source_repo_root": str(repo),
        },
    )
    write_json(
        run_dir / "intent" / "intent-test-validation.json",
        {
            "schema_version": "intent-test-validation/v1",
            "enabled": True,
            "max_tests_per_run": 20,
            "max_test_run_seconds_per_test": total_seconds,
            "max_total_test_run_seconds": total_seconds,
        },
    )
    write_json(
        run_dir / "intent" / "intent-test-plan.json",
        plan
        or {
            "schema_version": "intent-test-plan/v1",
            "test_targets": [{"test_id": "ITP-001"}],
        },
    )
    write_json(run_dir / "intent" / "intent-test-source.json", source)
    ensure_immutable_inventory_baseline(repo, run_dir)
    return run_dir, validation_repo


def _write_canonical_generated_test(
    run_dir: Path,
    relative_path: str,
    content: str,
) -> tuple[str, Path]:
    repo = run_dir.parent.parent.parent
    declared_path = (Path(".codex-review") / "generated-tests" / relative_path).as_posix()
    source_path = repo / declared_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(content, encoding="utf-8")
    validation_path = repo.parent / "validation-repo" / declared_path
    return declared_path, validation_path


def _generated_python_source(
    *,
    path: str,
    command: list[str],
    cwd: str = ".",
    required_paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "intent-test-source/v1",
        "generated_tests": [
            {
                "test_id": "ITV-001",
                "target_test_ids": ["ITP-001"],
                "path": path,
                "command": command,
                "cwd": cwd,
                "required_paths": list(required_paths or []),
            }
        ],
    }


class BundleResourceLimitRegressionTest(unittest.TestCase):
    def test_packed_bundle_keeps_unranged_files_alongside_a_ranged_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
            (repo_dir / "src").mkdir(parents=True)
            (repo_dir / "src" / "large.py").write_text(
                "first line\nsecond line\n",
                encoding="utf-8",
            )
            (repo_dir / "src" / "small.py").write_text(
                "SMALL_FILE_SENTINEL = True\n",
                encoding="utf-8",
            )
            write_json(
                run_dir / "bundle-plan.json",
                {
                    "schema_version": "bundle-plan/v1",
                    "run_id": run_dir.name,
                    "bundles": [
                        {
                            "bundle_id": "p1-bundle-001",
                            "tier": "P1",
                            "title": "mixed ranged and unranged",
                            "estimated_tokens": 1000,
                            "paths": ["src/large.py", "src/small.py"],
                            "file_ranges": [
                                {
                                    "path": "src/large.py",
                                    "start_line": 2,
                                    "end_line": 2,
                                }
                            ],
                            "reviewers": ["correctness"],
                            "intent_test_eligible": True,
                        }
                    ],
                },
            )

            pack_bundles(repo_dir, run_dir)
            packed = (run_dir / "bundles" / "p1-bundle-001.md").read_text(
                encoding="utf-8"
            )

        self.assertIn("2 | second line", packed)
        self.assertIn("### src/small.py", packed)
        self.assertIn("SMALL_FILE_SENTINEL = True", packed)

    def test_oversized_single_line_packed_bundle_stays_below_hard_token_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
            source_path = repo_dir / "src" / "single_line.py"
            source_path.parent.mkdir(parents=True)
            target_length = MAX_BUNDLE_ESTIMATED_TOKENS * 8
            source_text = "".join(
                hashlib.sha256(f"high-density-{index}".encode("ascii")).hexdigest()
                + f":{index:08x};"
                for index in range((target_length // 74) + 2)
            )[:target_length]
            source_path.write_text(source_text, encoding="utf-8")
            run_dir.mkdir(parents=True)
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [
                        {
                            "path": "src/single_line.py",
                            "is_source_like": True,
                            "is_binary": False,
                            "is_generated_candidate": False,
                            "risk_hints": [],
                            "estimated_tokens": MAX_BUNDLE_ESTIMATED_TOKENS * 2,
                            "line_count": 1,
                        }
                    ],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": "src/single_line.py", "tier": "P0"}],
                },
            )

            plan = materialize_test_bundle_plan(run_dir)
            write_json(run_dir / "bundle-plan.json", plan)
            pack_bundles(repo_dir, run_dir)
            packed_payloads = [
                (
                    bundle,
                    (run_dir / "bundles" / f"{bundle['bundle_id']}.md").read_text(encoding="utf-8"),
                )
                for bundle in plan["bundles"]
            ]

        self.assertGreater(len(packed_payloads), 1)
        self.assertTrue(
            all(
                len(payload.encode("utf-8")) <= MAX_BUNDLE_ESTIMATED_TOKENS
                and len(payload) <= MAX_BUNDLE_ESTIMATED_TOKENS
                for _bundle, payload in packed_payloads
            ),
            [
                (len(payload.encode("utf-8")), len(payload))
                for _bundle, payload in packed_payloads
            ],
        )
        self.assertTrue(
            all(
                int(bundle["estimated_tokens"])
                >= max(len(payload.encode("utf-8")), len(payload))
                for bundle, payload in packed_payloads
            ),
            [
                (
                    bundle["estimated_tokens"],
                    len(payload.encode("utf-8")),
                    len(payload),
                )
                for bundle, payload in packed_payloads
            ],
        )

    def test_multiline_file_with_one_oversized_line_is_split_by_rendered_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
            source_path = repo_dir / "src" / "mixed_lines.py"
            source_path.parent.mkdir(parents=True)
            oversized_line = "x" * (MAX_BUNDLE_ESTIMATED_TOKENS * 2)
            source_path.write_text(
                f"prefix = True\n{oversized_line}\nsuffix = True\n",
                encoding="utf-8",
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [
                        {
                            "path": "src/mixed_lines.py",
                            "is_source_like": True,
                            "is_binary": False,
                            "is_generated_candidate": False,
                            "risk_hints": [],
                            "estimated_tokens": (
                                MAX_BUNDLE_ESTIMATED_TOKENS * 4 + 1
                            ),
                            "line_count": 3,
                        }
                    ],
                },
            )
            write_json(
                run_dir / "risk-routing.json",
                {
                    "schema_version": "risk-routing/v1",
                    "routes": [{"path": "src/mixed_lines.py", "tier": "P0"}],
                },
            )

            plan = materialize_test_bundle_plan(run_dir)
            write_json(run_dir / "bundle-plan.json", plan)
            pack_bundles(repo_dir, run_dir)
            packed_payloads = [
                (
                    bundle,
                    (
                        run_dir
                        / "bundles"
                        / f"{bundle['bundle_id']}.md"
                    ).read_text(encoding="utf-8"),
                )
                for bundle in plan["bundles"]
            ]

        self.assertTrue(
            all(
                max(len(payload), len(payload.encode("utf-8")))
                <= MAX_BUNDLE_ESTIMATED_TOKENS
                for _bundle, payload in packed_payloads
            )
        )
        ranges = [
            item
            for bundle, _payload in packed_payloads
            for item in bundle.get("file_ranges", [])
        ]
        self.assertTrue(
            any(item["start_line"] == item["end_line"] == 1 for item in ranges)
        )
        self.assertTrue(
            any(item["start_line"] == item["end_line"] == 3 for item in ranges)
        )
        line_two_ranges = sorted(
            (
                int(item["start_char"]),
                int(item["end_char"]),
            )
            for item in ranges
            if item["start_line"] == item["end_line"] == 2
            and "start_char" in item
            and "end_char" in item
        )
        self.assertGreater(len(line_two_ranges), 1)
        self.assertEqual(line_two_ranges[0][0], 0)
        self.assertEqual(line_two_ranges[-1][1], len(oversized_line))
        self.assertTrue(
            all(
                left[1] == right[0]
                for left, right in zip(line_two_ranges, line_two_ranges[1:])
            )
        )


class IntentSubprocessResourceRegressionTest(unittest.TestCase):
    def test_large_intent_output_is_streamed_without_parent_memory_growth(self) -> None:
        output_bytes = 12 * 1024 * 1024
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            declared_path = ".codex-review/generated-tests/test_large_output.py"
            run_dir, validation_repo = _write_intent_run(
                root,
                source=_generated_python_source(
                    path=declared_path,
                    command=[sys.executable, declared_path],
                ),
            )
            _write_canonical_generated_test(
                run_dir,
                "test_large_output.py",
                "import sys\n"
                f"sys.stdout.write('x' * {output_bytes})\n",
            )

            tracemalloc.start()
            try:
                with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                    result = run_intent_tests(run_dir)
                _current, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            raw_run = result["test_runs"][0]
            stdout_path = Path(raw_run["stdout_path"])
            stdout_size = stdout_path.stat().st_size

        self.assertEqual(raw_run["status"], "passed")
        self.assertGreater(stdout_size, 0)
        self.assertLess(
            peak_bytes,
            output_bytes // 2,
            f"intent output consumed {peak_bytes} bytes in the worker process",
        )


class IntentRuntimeRepairRegressionTest(unittest.TestCase):
    def test_runtime_repair_does_not_treat_assertion_text_as_missing_module(self) -> None:
        diagnostics = intent_runtime_repair_diagnostics(
            {
                "test_runs": [
                    {
                        "test_id": "ITV-001",
                        "status": "failed",
                        "exit_code": 1,
                        "stderr": (
                            "AssertionError: expected message to contain "
                            "ModuleNotFoundError: No module named 'optional_plugin'"
                        ),
                    }
                ]
            }
        )

        self.assertEqual(diagnostics["repair_candidates"], [])
        self.assertEqual(
            diagnostics["non_repairable"][0]["reason_code"],
            "product_signal_or_non_repairable_environment",
        )

    def test_runtime_repair_recognizes_real_missing_module_traceback(self) -> None:
        diagnostics = intent_runtime_repair_diagnostics(
            {
                "test_runs": [
                    {
                        "test_id": "ITV-001",
                        "status": "failed",
                        "exit_code": 1,
                        "stderr": (
                            "Traceback (most recent call last):\n"
                            "  File 'test_generated.py', line 1, in <module>\n"
                            "ModuleNotFoundError: No module named 'project_dependency'\n"
                        ),
                    }
                ]
            }
        )

        self.assertEqual(
            diagnostics["repair_candidates"][0]["reason_code"],
            "project_dependency_missing",
        )

    def test_runtime_repair_forwards_one_global_deadline_to_turn_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, _validation_repo = _write_intent_run(
                root,
                source={"schema_version": "intent-test-source/v1", "generated_tests": []},
            )
            write_json(
                run_dir / "intent" / "intent-test-results.raw.json",
                {
                    "schema_version": "intent-test-run-results/v1",
                    "run_id": run_dir.name,
                    "test_runs": [
                        {
                            "test_id": "ITV-001",
                            "status": "skipped",
                            "attempt": 1,
                            "preflight": {
                                "status": "blocked",
                                "agent_repairable": True,
                                "reason_code": "command_missing",
                                "classification": "test_harness_error",
                            },
                        }
                    ],
                },
            )
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            deadline = time.monotonic() + 30
            retry_payload = {
                "schema_version": "intent-test-run-results/v1",
                "run_id": run_dir.name,
                "test_runs": [{"test_id": "ITV-001", "status": "passed", "attempt": 2}],
            }
            with patch.object(
                worker,
                "_run_intent_execution_repair_turn",
                return_value=True,
            ) as repair_turn, patch(
                "pullwise_worker.review_worker_v1.repair_intent_test_source_artifact"
            ), patch(
                "pullwise_worker.review_worker_v1.refresh_agentic_execution_capabilities",
                return_value={},
            ), patch(
                "pullwise_worker.review_worker_v1.intent_test_source_preflight_payload",
                return_value={"workspace_integrity": {"status": "ok"}},
            ), patch(
                "pullwise_worker.review_worker_v1.run_intent_tests",
                return_value=retry_payload,
            ) as retry:
                worker.repair_intent_test_runtime(
                    None,
                    root / "repo",
                    run_dir,
                    {},
                    max_attempts=1,
                    deadline_monotonic=deadline,
                )

        self.assertEqual(repair_turn.call_args.kwargs["deadline_monotonic"], deadline)
        self.assertEqual(retry.call_args.kwargs["deadline_monotonic"], deadline)

    def test_runtime_repair_turn_timeout_is_clamped_to_global_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})

            class RecordingCodex:
                def __init__(self) -> None:
                    self.timeout_seconds: int | float | None = None

                def run_turn(self, **kwargs: object) -> SimpleNamespace:
                    self.timeout_seconds = kwargs.get("timeout_seconds")  # type: ignore[assignment]
                    return SimpleNamespace(duration_ms=1)

            codex = RecordingCodex()
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            with patch(
                "pullwise_worker.review_worker_v1.time.monotonic",
                return_value=100.0,
            ), patch(
                "pullwise_worker.review_worker_v1.turn_timeout_for_job",
                return_value=30,
            ), patch(
                "pullwise_worker.review_worker_v1.effort_for_phase",
                return_value="medium",
            ):
                completed = worker._run_intent_execution_repair_turn(
                    codex,
                    root / "repo",
                    run_dir,
                    {},
                    stage="runtime",
                    attempt=1,
                    diagnostics={},
                    deadline_monotonic=103.0,
                )

        self.assertTrue(completed)
        self.assertIsNotNone(codex.timeout_seconds)
        self.assertLessEqual(float(codex.timeout_seconds or 0), 3.0)


class IntentExecutablePreflightRegressionTest(unittest.TestCase):
    def test_absolute_directory_is_not_accepted_as_an_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            diagnostic = intent_execution_preflight(
                [str(validation_repo), "test_behavior"],
                validation_repo,
                validation_repo,
            )

        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["classification"], "dependency_missing")

    @unittest.skipIf(os.name == "nt", "POSIX executable mode is not meaningful on Windows")
    def test_non_executable_regular_file_is_not_accepted_as_a_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            runner = validation_repo / "bespoke-test"
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(stat.S_IRUSR | stat.S_IWUSR)

            diagnostic = intent_execution_preflight(
                [str(runner), "behavior.spec"],
                validation_repo,
                validation_repo,
            )

        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["classification"], "dependency_missing")

    def test_pytest_probe_uses_the_selected_python_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            selected_python = validation_repo / "python3.99"
            selected_python.write_text("selected interpreter", encoding="utf-8")
            selected_python.chmod(0o755)
            probe_calls: list[list[str]] = []

            def fail_selected_probe(command: list[str], **_kwargs: object) -> SimpleNamespace:
                probe_calls.append([str(part) for part in command])
                return SimpleNamespace(returncode=1, stdout="", stderr="No module named pytest")

            with patch.object(importlib.util, "find_spec", return_value=object()), patch(
                "pullwise_worker.review_worker_v1.subprocess.run",
                side_effect=fail_selected_probe,
            ):
                runnable, reason = intent_command_is_runnable_for_repo(
                    [str(selected_python), "-m", "pytest", "test_behavior.py"],
                    validation_repo,
                    validation_repo,
                    {},
                )

        self.assertFalse(runnable)
        self.assertIn("pytest", reason.lower())
        self.assertTrue(probe_calls)
        self.assertEqual(Path(probe_calls[0][0]), selected_python)


class IntentPreflightBindingRegressionTest(unittest.TestCase):
    def test_source_preflight_records_and_rechecks_canonical_required_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation-repo"
            declared_path = ".codex-review/generated-tests/test_approved.py"
            script = validation_repo / declared_path
            run_dir, validation_repo = _write_intent_run(
                root,
                source=_generated_python_source(
                    path=declared_path,
                    command=[sys.executable, str(script)],
                    required_paths=["fixture.dat"],
                ),
            )
            _write_canonical_generated_test(
                run_dir,
                "test_approved.py",
                "raise SystemExit(0)\n",
            )
            fixture = validation_repo / "fixture.dat"
            fixture.write_text("fixture", encoding="utf-8")

            ready = intent_test_source_preflight_payload(run_dir)
            fixture.unlink()
            missing = intent_test_source_preflight_payload(run_dir)

        self.assertEqual(ready["tests"][0]["status"], "ready")
        self.assertEqual(
            ready["tests"][0]["required_paths"],
            [str(fixture.resolve(strict=False))],
        )
        self.assertEqual(missing["tests"][0]["status"], "blocked")
        self.assertEqual(missing["tests"][0]["reason_code"], "required_path_missing")

    def test_source_preflight_rejects_required_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation-repo"
            declared_path = ".codex-review/generated-tests/test_approved.py"
            script = validation_repo / declared_path
            run_dir, validation_repo = _write_intent_run(
                root,
                source=_generated_python_source(
                    path=declared_path,
                    command=[sys.executable, str(script)],
                    required_paths=["../outside.dat"],
                ),
            )
            _write_canonical_generated_test(
                run_dir,
                "test_approved.py",
                "raise SystemExit(0)\n",
            )
            (root / "outside.dat").write_text("outside", encoding="utf-8")

            preflight = intent_test_source_preflight_payload(run_dir)

        self.assertEqual(preflight["tests"][0]["status"], "blocked")
        self.assertEqual(preflight["tests"][0]["reason_code"], "required_path_escape")
        self.assertFalse(preflight["tests"][0]["agent_repairable"])

    def test_execution_rejects_command_cwd_and_required_path_drift_after_preflight(self) -> None:
        drift_cases = ("command", "cwd", "required_paths")
        outcomes: dict[str, tuple[bool, dict[str, object]]] = {}
        for drift_case in drift_cases:
            with self.subTest(drift=drift_case), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                validation_repo = root / "validation-repo"
                declared_path = ".codex-review/generated-tests/test_approved.py"
                script = validation_repo / declared_path
                marker = root / f"{drift_case}.executed"
                approved_command = [sys.executable, str(script), "--approved"]
                approved_required_paths = [str(validation_repo / "fixture-a.dat")]
                source = _generated_python_source(
                    path=declared_path,
                    command=approved_command,
                    required_paths=approved_required_paths,
                )
                run_dir, validation_repo = _write_intent_run(root, source=source)
                (validation_repo / "fixture-a.dat").write_text("a", encoding="utf-8")
                (validation_repo / "fixture-b.dat").write_text("b", encoding="utf-8")
                (validation_repo / "nested").mkdir()
                _write_canonical_generated_test(
                    run_dir,
                    "test_approved.py",
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                )

                preflight = intent_test_source_preflight_payload(run_dir)
                preflight["tests"][0]["required_paths"] = approved_required_paths
                write_json(run_dir / "intent" / "intent-test-preflight.json", preflight)

                mutated_source = json.loads(
                    (run_dir / "intent" / "intent-test-source.json").read_text(encoding="utf-8")
                )
                generated = mutated_source["generated_tests"][0]
                if drift_case == "command":
                    generated["command"] = [sys.executable, str(script), "--changed"]
                elif drift_case == "cwd":
                    generated["cwd"] = "nested"
                else:
                    generated["required_paths"] = [str(validation_repo / "fixture-b.dat")]
                write_json(run_dir / "intent" / "intent-test-source.json", mutated_source)

                with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                    result = run_intent_tests(run_dir)

                raw_run = result["test_runs"][0]
                marker_existed = marker.exists()

            outcomes[drift_case] = (marker_existed, raw_run)
        for drift_case, (marker_existed, raw_run) in outcomes.items():
            with self.subTest(drift=drift_case):
                self.assertFalse(marker_existed, drift_case)
                self.assertNotEqual(raw_run["status"], "passed", drift_case)
                self.assertEqual(
                    raw_run.get("preflight", {}).get("reason_code"),
                    "preflight_candidate_mismatch",
                    drift_case,
                )

    def test_execution_rejects_symlinked_preflight_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            declared_path = ".codex-review/generated-tests/test_approved.py"
            marker = root / "symlinked-preflight.executed"
            validation_script = root / "validation-repo" / declared_path
            run_dir, validation_repo = _write_intent_run(
                root,
                source=_generated_python_source(
                    path=declared_path,
                    command=[sys.executable, str(validation_script)],
                ),
            )
            _write_canonical_generated_test(
                run_dir,
                "test_approved.py",
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
            )
            approved = intent_test_source_preflight_payload(run_dir)
            approved_target = root / "approved-preflight.json"
            write_json(approved_target, approved)
            approved_path = run_dir / "intent" / "intent-test-preflight.json"
            try:
                approved_path.symlink_to(approved_target)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)

        self.assertFalse(marker.exists())
        self.assertEqual(result["test_runs"][0]["status"], "skipped")
        self.assertEqual(
            result["test_runs"][0]["preflight"]["reason_code"],
            "preflight_candidate_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
