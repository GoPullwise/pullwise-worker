from __future__ import annotations

import json
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker.agentic_execution import build_execution_capabilities
from pullwise_worker.review_worker_v1 import (
    ReviewWorkerV1,
    intent_execution_repair_prompt,
    intent_runtime_repair_diagnostics,
    intent_test_command_policy,
    intent_test_source_preflight_payload,
    phase_prompt,
    run_intent_tests,
    write_json,
)


def _write_intent_run(
    root: Path,
    *,
    plan: dict,
    source: dict,
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
            "max_test_run_seconds_per_test": 20,
            "max_total_test_run_seconds": 60,
        },
    )
    write_json(run_dir / "intent" / "intent-test-plan.json", plan)
    write_json(run_dir / "intent" / "intent-test-source.json", source)
    return run_dir, validation_repo


class AgenticExecutionContractsTest(unittest.TestCase):
    def test_capability_snapshot_is_driven_by_agent_candidates_and_nested_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            (repo / "packages" / "web").mkdir(parents=True)
            (repo / "services" / "api").mkdir(parents=True)
            (repo / "packages" / "web" / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run"}}),
                encoding="utf-8",
            )
            (repo / "services" / "api" / "pyproject.toml").write_text(
                "[project]\nname='api'\n",
                encoding="utf-8",
            )
            plan = {
                "test_targets": [
                    {
                        "test_id": "ITP-001",
                        "execution_candidates": [
                            {
                                "command": ["node", "--test", "generated.test.mjs"],
                                "cwd": "packages/web",
                            },
                            {
                                "command": ["missing-flex-runner", "verify"],
                                "cwd": "services/api",
                            },
                        ],
                    }
                ]
            }

            payload = build_execution_capabilities(
                repo,
                proposal_sources=[plan],
                executable_resolver=lambda name: "/usr/bin/node" if name == "node" else None,
                sandbox_available=True,
            )

        self.assertEqual(payload["schema_version"], "agentic-execution-capabilities/v1")
        self.assertEqual(
            {workspace["root"] for workspace in payload["workspaces"]},
            {"packages/web", "services/api"},
        )
        candidates = payload["agent_candidates"]
        self.assertEqual(candidates[0]["test_id"], "ITP-001")
        self.assertTrue(candidates[0]["executable"]["available"])
        self.assertFalse(candidates[1]["executable"]["available"])
        self.assertEqual(candidates[1]["executable"]["name"], "missing-flex-runner")

    def test_preflight_returns_structured_missing_local_runner_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "generated.test.js",
                            "command": ["npm", "test", "--", "generated.test.js"],
                        }
                    ],
                },
            )
            (validation_repo / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run"}}),
                encoding="utf-8",
            )
            (validation_repo / "generated.test.js").write_text("export {};\n", encoding="utf-8")
            with patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                side_effect=lambda name: "/usr/bin/npm" if name == "npm" else None,
            ):
                payload = intent_test_source_preflight_payload(run_dir)

        diagnostic = payload["tests"][0]
        self.assertEqual(payload["summary"]["ready"], 0)
        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["reason_code"], "package_local_runner_missing")
        self.assertEqual(diagnostic["classification"], "dependency_missing")
        self.assertTrue(diagnostic["agent_repairable"])
        self.assertEqual(diagnostic["missing_capabilities"], ["node_modules/.bin/vitest"])

    def test_generic_contained_agent_runner_is_allowed_without_a_language_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            runner = validation_repo / "tools" / "bespoke-check"
            test_file = validation_repo / "checks" / "behavior.spec.custom"
            runner.parent.mkdir(parents=True)
            test_file.parent.mkdir(parents=True)
            runner.write_text("runner", encoding="utf-8")
            test_file.write_text("case", encoding="utf-8")

            allowed, reason = intent_test_command_policy(
                [str(runner), str(test_file)],
                validation_repo,
                validation_repo,
            )

        self.assertTrue(allowed, reason)
        self.assertIn("agent-proposed", reason)

    def test_generic_agent_runner_still_rejects_network_install_and_shell_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            cases = (
                ["curl", "https://example.invalid/test"],
                ["custom-test", "install"],
                ["custom-test", "&&", "other-test"],
            )
            for command in cases:
                with self.subTest(command=command):
                    allowed, _reason = intent_test_command_policy(
                        command,
                        validation_repo,
                        validation_repo,
                    )
                    self.assertFalse(allowed)

            nested = validation_repo / "packages" / "api"
            nested.mkdir(parents=True)
            allowed, reason = intent_test_command_policy(
                ["dotnet", "test", "../../../outside/intent-tests.dll"],
                nested,
                validation_repo,
            )

        self.assertFalse(allowed)
        self.assertIn("outside", reason)

    def test_source_preflight_marks_missing_command_as_agent_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "generated_test.custom",
                        }
                    ],
                },
            )
            (validation_repo / "generated_test.custom").write_text("generated test\n", encoding="utf-8")

            payload = intent_test_source_preflight_payload(run_dir)

        self.assertEqual(payload["tests"][0]["reason_code"], "command_missing")
        self.assertTrue(payload["tests"][0]["agent_repairable"])

    def test_validation_workspace_source_mutation_blocks_execution_without_agent_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "generated_test.py",
                            "command": [sys.executable, "-m", "unittest", "generated_test.py"],
                        }
                    ],
                },
            )
            source_repo = root / "repo"
            source_repo.mkdir(parents=True, exist_ok=True)
            original = b"def value(): return 7\n"
            (source_repo / "app.py").write_bytes(original)
            (validation_repo / "app.py").write_text("def value(): return 999\n", encoding="utf-8")
            (validation_repo / "generated_test.py").write_text(
                "from pathlib import Path\n"
                "Path('executed.marker').write_text('executed')\n",
                encoding="utf-8",
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [
                        {
                            "path": "app.py",
                            "sha256": hashlib.sha256(original).hexdigest(),
                        }
                    ],
                },
            )

            preflight = intent_test_source_preflight_payload(run_dir)
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)

        self.assertEqual(preflight["tests"][0]["reason_code"], "validation_workspace_modified")
        self.assertFalse(preflight["tests"][0]["agent_repairable"])
        self.assertEqual(result["test_runs"][0]["status"], "skipped")
        self.assertFalse((validation_repo / "executed.marker").exists())

    def test_runtime_diagnostics_repairs_harness_failure_but_not_product_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir)
            stderr_missing = run_dir / "missing.stderr.log"
            stderr_assertion = run_dir / "assertion.stderr.log"
            stderr_missing.write_text(
                "ModuleNotFoundError: No module named 'project_dependency'\n",
                encoding="utf-8",
            )
            stderr_assertion.write_text(
                "AssertionError: expected safe URL but received javascript:alert(1)\n",
                encoding="utf-8",
            )
            raw = {
                "schema_version": "intent-test-run-results/v1",
                "test_runs": [
                    {
                        "test_id": "ITV-harness",
                        "status": "failed",
                        "exit_code": 1,
                        "stderr_path": str(stderr_missing),
                    },
                    {
                        "test_id": "ITV-product",
                        "status": "failed",
                        "exit_code": 1,
                        "stderr_path": str(stderr_assertion),
                    },
                ],
            }

            payload = intent_runtime_repair_diagnostics(raw)

        self.assertEqual([item["test_id"] for item in payload["repair_candidates"]], ["ITV-harness"])
        self.assertEqual(payload["repair_candidates"][0]["reason_code"], "project_dependency_missing")
        self.assertEqual(payload["non_repairable"][0]["test_id"], "ITV-product")

    def test_runtime_diagnostics_do_not_treat_product_not_found_assertions_as_missing_dependencies(self) -> None:
        payload = intent_runtime_repair_diagnostics(
            {
                "test_runs": [
                    {
                        "test_id": "ITV-001",
                        "status": "failed",
                        "exit_code": 1,
                        "stderr": "AssertionError: expected the API to report resource not found",
                    }
                ]
            }
        )

        self.assertEqual(payload["summary"], {"repairable": 0, "non_repairable": 1})

    def test_writer_prompt_exposes_capabilities_without_forcing_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir)
            write_json(
                run_dir / "intent" / "execution-capabilities.json",
                {
                    "schema_version": "agentic-execution-capabilities/v1",
                    "runtimes": [{"name": "bespoke", "available": True}],
                    "agent_candidates": [],
                },
            )

            prompt = phase_prompt("intent_test_writing", run_dir)

        self.assertIn("intent/execution-capabilities.json", prompt)
        self.assertIn("agent-proposed", prompt)
        self.assertIn("faithful", prompt)
        self.assertNotIn("must select a fixed language template", prompt)

    def test_execution_repair_prompt_preserves_oracle_and_forbids_logic_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir)
            write_json(
                run_dir / "intent" / "intent-test-preflight.json",
                {
                    "schema_version": "intent-test-preflight/v1",
                    "tests": [{"test_id": "ITV-001", "reason_code": "command_missing"}],
                },
            )

            prompt = intent_execution_repair_prompt(run_dir, stage="preflight", attempt=1)

        self.assertIn("preserve the behavioral oracle", prompt)
        self.assertIn("Do not copy or reimplement application logic", prompt)
        self.assertIn("intent-test-preflight.json", prompt)
        self.assertIn("execution-capabilities.json", prompt)

    def test_worker_uses_agent_feedback_to_repair_preflight_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "generated.custom",
                        }
                    ],
                },
            )
            (validation_repo / "generated.custom").write_text("candidate\n", encoding="utf-8")
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})

            class RepairingCodex:
                def __init__(self) -> None:
                    self.prompts: list[str] = []

                def run_turn(self, **kwargs):
                    self.prompts.append(kwargs["prompt"])
                    (validation_repo / "generated_test.py").write_text(
                        "import unittest\n"
                        "class GeneratedTest(unittest.TestCase):\n"
                        "    def test_behavior(self): self.assertTrue(True)\n",
                        encoding="utf-8",
                    )
                    write_json(
                        run_dir / "intent" / "intent-test-source.json",
                        {
                            "schema_version": "intent-test-source/v1",
                            "generated_tests": [
                                {
                                    "test_id": "ITV-001",
                                    "target_test_ids": ["ITP-001"],
                                    "path": "generated_test.py",
                                    "command": [sys.executable, "-m", "unittest", "generated_test.py"],
                                }
                            ],
                        },
                    )
                    return SimpleNamespace(duration_ms=5)

            codex = RepairingCodex()
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            with patch("pullwise_worker.review_worker_v1.effort_for_phase", return_value="medium"), patch(
                "pullwise_worker.review_worker_v1.turn_timeout_for_job",
                return_value=30,
            ):
                payload = worker.repair_intent_test_preflight(
                    codex,
                    root / "repo",
                    run_dir,
                    {},
                    max_attempts=1,
                )

        self.assertEqual(len(codex.prompts), 1)
        self.assertIn("command_missing", codex.prompts[0])
        self.assertEqual(payload["summary"]["ready"], 1)
        self.assertEqual(payload["summary"]["blocked"], 0)

    def test_worker_repairs_real_runtime_harness_failure_and_preserves_attempt_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "generated_test.py",
                            "command": [sys.executable, "-m", "unittest", "generated_test.py"],
                        }
                    ],
                },
            )
            (validation_repo / "app.py").write_text("def value(): return 7\n", encoding="utf-8")
            generated_path = validation_repo / "generated_test.py"
            generated_path.write_text(
                "import unittest\n"
                "import unneeded_project_dependency\n"
                "from app import value\n"
                "class GeneratedTest(unittest.TestCase):\n"
                "    def test_behavior(self): self.assertEqual(value(), 7)\n",
                encoding="utf-8",
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                initial = run_intent_tests(run_dir)
            self.assertEqual(initial["test_runs"][0]["status"], "failed")
            write_json(run_dir / "intent" / "intent-test-results.raw.json", initial)

            class RepairingCodex:
                def __init__(self) -> None:
                    self.calls = 0

                def run_turn(self, **_kwargs):
                    self.calls += 1
                    generated_path.write_text(
                        "import unittest\n"
                        "from app import value\n"
                        "class GeneratedTest(unittest.TestCase):\n"
                        "    def test_behavior(self): self.assertEqual(value(), 7)\n",
                        encoding="utf-8",
                    )
                    return SimpleNamespace(duration_ms=5)

            codex = RepairingCodex()
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"), patch(
                "pullwise_worker.review_worker_v1.effort_for_phase",
                return_value="medium",
            ), patch("pullwise_worker.review_worker_v1.turn_timeout_for_job", return_value=30):
                repaired = worker.repair_intent_test_runtime(
                    codex,
                    root / "repo",
                    run_dir,
                    {},
                    max_attempts=1,
                )

            history = json.loads(
                (run_dir / "intent" / "intent-test-execution-history.json").read_text(encoding="utf-8")
            )

        self.assertEqual(codex.calls, 1)
        self.assertEqual(repaired["test_runs"][0]["status"], "passed")
        self.assertEqual(repaired["test_runs"][0]["attempt"], 2)
        self.assertEqual(history["attempts"][0]["test_runs"][0]["status"], "failed")

    def test_real_python_unittest_executes_through_agentic_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "generated_test.py",
                            "command": [sys.executable, "-m", "unittest", "generated_test.py"],
                        }
                    ],
                },
            )
            (validation_repo / "generated_test.py").write_text(
                "import unittest\n"
                "class GeneratedTest(unittest.TestCase):\n"
                "    def test_real_process(self):\n"
                "        self.assertEqual(sum([1, 2, 3]), 6)\n",
                encoding="utf-8",
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)

        self.assertEqual(result["test_runs"][0]["status"], "passed")
        self.assertEqual(result["test_runs"][0]["exit_code"], 0)
        self.assertEqual(result["test_runs"][0]["preflight"]["status"], "ready")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the real agentic runner test")
    def test_real_node_builtin_test_executes_in_nested_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            node = str(Path(shutil.which("node") or "node").resolve())
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "packages/web/generated.intent.test.mjs",
                            "cwd": "packages/web",
                            "command": [node, "--test", "generated.intent.test.mjs"],
                        }
                    ],
                },
            )
            workspace = validation_repo / "packages" / "web"
            workspace.mkdir(parents=True)
            (workspace / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            (workspace / "generated.intent.test.mjs").write_text(
                "import test from 'node:test';\n"
                "import assert from 'node:assert/strict';\n"
                "test('real node process', () => assert.equal(2 + 3, 5));\n",
                encoding="utf-8",
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)

        self.assertEqual(result["test_runs"][0]["status"], "passed")
        self.assertEqual(result["test_runs"][0]["exit_code"], 0)
        self.assertEqual(result["test_runs"][0]["cwd"], str(workspace))


if __name__ == "__main__":
    unittest.main()
