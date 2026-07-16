from __future__ import annotations

import json
import hashlib
import os
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
    _intent_test_sandbox_command,
    _intent_test_env,
    ensure_immutable_inventory_baseline,
    intent_execution_repair_prompt,
    intent_runtime_repair_diagnostics,
    intent_test_command_policy,
    intent_test_source_preflight_payload,
    phase_prompt,
    prompt_template_for_name,
    refresh_agentic_execution_capabilities,
    run_intent_tests,
    write_json,
)


def _write_intent_run(
    root: Path,
    *,
    plan: dict,
    source: dict,
    defer_inventory_baseline: bool = False,
    per_test_timeout_seconds: int = 60,
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
            "max_test_run_seconds_per_test": per_test_timeout_seconds,
            "max_total_test_run_seconds": 60,
        },
    )
    write_json(run_dir / "intent" / "intent-test-plan.json", plan)
    write_json(run_dir / "intent" / "intent-test-source.json", source)
    if not defer_inventory_baseline:
        ensure_immutable_inventory_baseline(repo, run_dir)
    return run_dir, validation_repo


def _establish_immutable_validation_baseline(
    run_dir: Path,
    validation_repo: Path,
    relative_paths: list[str],
) -> None:
    repo = run_dir.parent.parent.parent
    for relative_path in relative_paths:
        validation_path = validation_repo / relative_path
        source_path = repo / relative_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(validation_path.read_bytes())
    ensure_immutable_inventory_baseline(repo, run_dir)


def _canonical_generated_test_path(relative_path: str) -> str:
    return (
        Path(".codex-review") / "generated-tests" / relative_path
    ).as_posix()


def _write_canonical_generated_test(
    run_dir: Path,
    validation_repo: Path,
    relative_path: str,
    content: str,
) -> tuple[Path, Path]:
    declared_path = _canonical_generated_test_path(relative_path)
    source_payload = json.loads(
        (run_dir / "intent" / "intent-test-source.json").read_text(
            encoding="utf-8"
        )
    )
    for generated in source_payload.get("generated_tests", []):
        if not isinstance(generated, dict):
            continue
        if str(generated.get("path") or "") != relative_path:
            continue
        generated["path"] = declared_path
        command = generated.get("command")
        if isinstance(command, list):
            generated["command"] = [
                declared_path if str(part) == relative_path else part
                for part in command
            ]
    write_json(run_dir / "intent" / "intent-test-source.json", source_payload)
    source_path = run_dir.parent.parent.parent / declared_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(content, encoding="utf-8")
    return source_path, validation_repo / declared_path


def _write_staged_generated_test(
    turn_kwargs: dict[str, object],
    relative_path: str,
    content: str,
) -> Path:
    turn_cwd = Path(str(turn_kwargs["turn_cwd"]))
    declared_path = (
        Path("intent") / "generated-tests" / relative_path
    ).as_posix()
    staged_source = turn_cwd / declared_path
    staged_source.parent.mkdir(parents=True, exist_ok=True)
    staged_source.write_text(content, encoding="utf-8")
    source_path = turn_cwd / "intent" / "intent-test-source.json"
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    for generated in source_payload.get("generated_tests", []):
        if not isinstance(generated, dict):
            continue
        old_path = str(generated.get("path") or "")
        if Path(old_path).name != Path(relative_path).name:
            continue
        generated["path"] = declared_path
        command = generated.get("command")
        if isinstance(command, list):
            generated["command"] = [
                declared_path if str(part) == old_path else part
                for part in command
            ]
    write_json(source_path, source_payload)
    return turn_cwd


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

    def test_candidate_required_paths_are_mechanically_preflighted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={
                    "schema_version": "intent-test-plan/v1",
                    "test_targets": [
                        {
                            "test_id": "ITP-001",
                            "execution_candidates": [
                                {
                                    "command": [sys.executable, "-m", "unittest", "generated_test.py"],
                                    "cwd": ".",
                                    "required_paths": ["fixtures/contract.json"],
                                },
                                {
                                    "command": [sys.executable, "-m", "unittest", "generated_test.py"],
                                    "cwd": ".",
                                    "required_paths": ["../outside-secret"],
                                },
                            ],
                        }
                    ],
                },
                source={"schema_version": "intent-test-source/v1", "generated_tests": []},
            )

            payload = refresh_agentic_execution_capabilities(validation_repo, run_dir)

        candidates = payload["agent_candidates"]
        self.assertEqual(candidates[0]["preflight"]["reason_code"], "required_path_missing")
        self.assertEqual(candidates[0]["preflight"]["missing_capabilities"], ["fixtures/contract.json"])
        self.assertEqual(candidates[1]["preflight"]["reason_code"], "required_path_escape")
        self.assertFalse(candidates[1]["preflight"]["agent_repairable"])

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
                defer_inventory_baseline=True,
            )
            (validation_repo / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run"}}),
                encoding="utf-8",
            )
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated.test.js",
                "export {};\n",
            )
            _establish_immutable_validation_baseline(
                run_dir,
                validation_repo,
                ["package.json"],
            )
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
            runner.chmod(0o755)
            test_file.write_text("case", encoding="utf-8")

            allowed, reason = intent_test_command_policy(
                [str(runner), str(test_file)],
                validation_repo,
                validation_repo,
            )

        self.assertTrue(allowed, reason)
        self.assertIn("agent-proposed", reason)

    def test_relative_contained_agent_runner_passes_executable_preflight(self) -> None:
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
                            "path": "checks/behavior.spec.custom",
                            "command": ["tools/bespoke-test", "checks/behavior.spec.custom"],
                        }
                    ],
                },
            )
            runner = validation_repo / "tools" / "bespoke-test"
            runner.parent.mkdir(parents=True)
            runner.write_text("contained runner", encoding="utf-8")
            runner.chmod(0o755)
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "checks/behavior.spec.custom",
                "case",
            )

            payload = intent_test_source_preflight_payload(run_dir)

        self.assertEqual(payload["tests"][0]["status"], "ready")

    def test_linux_sandbox_rewrites_absolute_contained_runner_to_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            runner = validation_repo / "tools" / "bespoke-test"
            runner.parent.mkdir(parents=True)
            runner.write_text("runner", encoding="utf-8")
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                side_effect=lambda name: "/usr/bin/bwrap" if name in {"bwrap", "bubblewrap"} else None,
            ):
                command, sandbox_cwd, skip_reason = _intent_test_sandbox_command(
                    [str(runner), "behavior.spec"],
                    validation_repo,
                    validation_repo,
                )

        self.assertEqual(skip_reason, "")
        self.assertEqual(sandbox_cwd, "/workspace")
        self.assertEqual(command[-2], "/workspace/tools/bespoke-test")

    def test_linux_sandbox_read_only_binds_a_trusted_external_runtime_bin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation"
            runtime_bin = root / "toolchain" / "bin"
            validation_repo.mkdir()
            runtime_bin.mkdir(parents=True)
            runner = runtime_bin / "custom-intent-test"
            runner.write_text("runtime", encoding="utf-8")
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                side_effect=lambda name: "/usr/bin/bwrap" if name in {"bwrap", "bubblewrap"} else None,
            ):
                command, _sandbox_cwd, skip_reason = _intent_test_sandbox_command(
                    [str(runner), "verify"],
                    validation_repo,
                    validation_repo,
                )

        self.assertEqual(skip_reason, "")
        self.assertTrue(
            any(
                command[index : index + 3] == ["--ro-bind", str(runtime_bin), str(runtime_bin)]
                for index in range(len(command) - 2)
            )
        )
        self.assertEqual(command[-2], str(runner))

    def test_linux_sandbox_resolves_and_binds_a_bare_external_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation"
            runtime_bin = root / "toolchain" / "bin"
            validation_repo.mkdir()
            runtime_bin.mkdir(parents=True)
            runner = runtime_bin / "custom-intent-test"
            runner.write_text("runtime", encoding="utf-8")

            def resolve_runtime(name: str) -> str | None:
                if name in {"bwrap", "bubblewrap"}:
                    return "/usr/bin/bwrap"
                if name == "custom-intent-test":
                    return str(runner)
                return None

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                side_effect=resolve_runtime,
            ):
                command, _sandbox_cwd, skip_reason = _intent_test_sandbox_command(
                    ["custom-intent-test", "verify"],
                    validation_repo,
                    validation_repo,
                )

        self.assertEqual(skip_reason, "")
        self.assertTrue(
            any(
                command[index : index + 3] == ["--ro-bind", str(runtime_bin), str(runtime_bin)]
                for index in range(len(command) - 2)
            )
        )
        self.assertEqual(command[-2], "custom-intent-test")

    def test_linux_sandbox_exposes_explicit_rust_toolchains_without_package_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation"
            rustup_home = root / "rustup-home"
            validation_repo.mkdir()
            rustup_home.mkdir()
            with patch.dict(os.environ, {"RUSTUP_HOME": str(rustup_home)}), patch(
                "pullwise_worker.review_worker_v1.sys.platform",
                "linux",
            ), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                side_effect=lambda name: "/usr/bin/bwrap" if name in {"bwrap", "bubblewrap"} else None,
            ):
                command, _sandbox_cwd, skip_reason = _intent_test_sandbox_command(
                    ["cargo", "test"],
                    validation_repo,
                    validation_repo,
                )

        self.assertEqual(skip_reason, "")
        self.assertTrue(
            any(
                command[index : index + 3] == ["--ro-bind", str(rustup_home), str(rustup_home)]
                for index in range(len(command) - 2)
            )
        )

    def test_rust_runtime_uses_default_host_rustup_home_when_env_is_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation"
            host_home = root / "host-home"
            rustup_home = host_home / ".rustup"
            validation_repo.mkdir()
            rustup_home.mkdir(parents=True)
            with patch.dict(os.environ, {"HOME": str(host_home)}, clear=True), patch(
                "pullwise_worker.review_worker_v1.sys.platform",
                "linux",
            ), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                side_effect=lambda name: "/usr/bin/bwrap" if name in {"bwrap", "bubblewrap"} else None,
            ):
                env = _intent_test_env(
                    validation_repo,
                    command=["cargo", "test", "--offline"],
                )
                command, _sandbox_cwd, skip_reason = _intent_test_sandbox_command(
                    ["cargo", "test", "--offline"],
                    validation_repo,
                    validation_repo,
                )
                non_rust_env = _intent_test_env(
                    validation_repo,
                    command=["python", "-m", "unittest"],
                )
                non_rust_command, _sandbox_cwd, non_rust_skip_reason = _intent_test_sandbox_command(
                    ["python", "-m", "unittest"],
                    validation_repo,
                    validation_repo,
                )

        self.assertEqual(env["RUSTUP_HOME"], str(rustup_home))
        self.assertNotIn("RUSTUP_HOME", non_rust_env)
        self.assertEqual(skip_reason, "")
        self.assertEqual(non_rust_skip_reason, "")
        self.assertTrue(
            any(
                command[index : index + 3] == ["--ro-bind", str(rustup_home), str(rustup_home)]
                for index in range(len(command) - 2)
            )
        )
        self.assertFalse(
            any(
                non_rust_command[index : index + 3]
                == ["--ro-bind", str(rustup_home), str(rustup_home)]
                for index in range(len(non_rust_command) - 2)
            )
        )

    def test_generic_agent_runner_still_rejects_network_install_and_shell_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            cases = (
                ["curl", "https://example.invalid/test"],
                ["custom-test", "install"],
                ["custom-test", "&&", "other-test"],
                ["npm", "test", "--", "https://example.invalid/intent.test.js"],
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

        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir)
            allowed, reason = intent_test_command_policy(
                ["npm", "test", "--", "../outside/intent.test.js"],
                validation_repo,
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
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated_test.custom",
                "generated test\n",
            )

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
                defer_inventory_baseline=True,
            )
            source_repo = root / "repo"
            source_repo.mkdir(parents=True, exist_ok=True)
            original = b"def value(): return 7\n"
            (source_repo / "app.py").write_bytes(original)
            ensure_immutable_inventory_baseline(source_repo, run_dir)
            (validation_repo / "app.py").write_text("def value(): return 999\n", encoding="utf-8")
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated_test.py",
                "from pathlib import Path\n"
                "Path('executed.marker').write_text('executed')\n",
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

    def test_passing_test_is_invalidated_if_it_mutates_repository_source_during_execution(self) -> None:
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
                defer_inventory_baseline=True,
            )
            source_repo = root / "repo"
            source_repo.mkdir(parents=True, exist_ok=True)
            original = b"def value(): return 7\n"
            (source_repo / "app.py").write_bytes(original)
            ensure_immutable_inventory_baseline(source_repo, run_dir)
            (validation_repo / "app.py").write_bytes(original)
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated_test.py",
                "import unittest\n"
                "from pathlib import Path\n"
                "class GeneratedTest(unittest.TestCase):\n"
                "    def test_mutating_pass(self):\n"
                "        Path('app.py').write_text('def value(): return 999\\n')\n"
                "        self.assertTrue(True)\n",
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [{"path": "app.py", "sha256": hashlib.sha256(original).hexdigest()}],
                },
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)

        self.assertEqual(result["test_runs"][0]["status"], "error")
        self.assertEqual(result["test_runs"][0]["preflight"]["reason_code"], "validation_workspace_modified")

    def test_generated_test_cannot_claim_an_existing_repository_file(self) -> None:
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
                            "path": "app.py",
                            "command": [sys.executable, "app.py"],
                        }
                    ],
                },
                defer_inventory_baseline=True,
            )
            source_repo = root / "repo"
            source_repo.mkdir(parents=True, exist_ok=True)
            (source_repo / "app.py").write_text("print('application')\n", encoding="utf-8")
            ensure_immutable_inventory_baseline(source_repo, run_dir)
            (validation_repo / "app.py").write_text("print('application')\n", encoding="utf-8")

            payload = intent_test_source_preflight_payload(run_dir)

        self.assertEqual(payload["tests"][0]["reason_code"], "generated_test_overwrites_repository_file")
        self.assertFalse(payload["tests"][0]["agent_repairable"])

    def test_agent_can_explicitly_reuse_an_immutable_existing_test(self) -> None:
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
                            "path": "tests/test_existing.py",
                            "reuse_existing": True,
                            "command": [sys.executable, "-m", "unittest", "tests/test_existing.py"],
                        }
                    ],
                },
                defer_inventory_baseline=True,
            )
            source_repo = root / "repo"
            source_test = source_repo / "tests" / "test_existing.py"
            validation_test = validation_repo / "tests" / "test_existing.py"
            source_test.parent.mkdir(parents=True, exist_ok=True)
            validation_test.parent.mkdir(parents=True, exist_ok=True)
            content = (
                "import unittest\n"
                "class ExistingTest(unittest.TestCase):\n"
                "    def test_behavior(self): self.assertTrue(True)\n"
            )
            source_test.write_text(content, encoding="utf-8")
            ensure_immutable_inventory_baseline(source_repo, run_dir)
            validation_test.write_text(content, encoding="utf-8")

            payload = intent_test_source_preflight_payload(run_dir)

        self.assertEqual(payload["tests"][0]["status"], "ready")

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

    def test_runtime_diagnostics_respect_explicit_nonrepairable_integrity_failures(self) -> None:
        payload = intent_runtime_repair_diagnostics(
            {
                "test_runs": [
                    {
                        "test_id": "ITV-001",
                        "status": "error",
                        "exit_code": 1,
                        "stderr": "ModuleNotFoundError: No module named 'helper'",
                        "preflight": {
                            "status": "blocked",
                            "reason_code": "validation_workspace_modified",
                            "classification": "environment_error",
                            "agent_repairable": False,
                        },
                    }
                ]
            }
        )

        self.assertEqual(payload["summary"], {"repairable": 0, "non_repairable": 1})
        self.assertEqual(payload["non_repairable"][0]["reason_code"], "validation_workspace_modified")

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

    def test_materialized_agent_prompts_preserve_the_agentic_execution_contract(self) -> None:
        planner = prompt_template_for_name("intent/05_intent_test_planner.md")
        writer = prompt_template_for_name("intent/06_intent_test_writer.md")

        self.assertIn("execution-capabilities.json", planner)
        self.assertIn("execution_candidates", planner)
        self.assertIn("agent-proposed", writer)
        self.assertIn("real repository code", writer)
        self.assertIn("Do not copy or reimplement", writer)

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
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated.custom",
                "candidate\n",
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})

            class RepairingCodex:
                def __init__(self) -> None:
                    self.prompts: list[str] = []

                def run_turn(self, **kwargs):
                    self.prompts.append(kwargs["prompt"])
                    declared_path = (
                        Path("intent") / "generated-tests" / "generated_test.py"
                    ).as_posix()
                    turn_cwd = Path(kwargs["turn_cwd"])
                    source_path = turn_cwd / declared_path
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    source_path.write_text(
                        "import unittest\n"
                        "class GeneratedTest(unittest.TestCase):\n"
                        "    def test_behavior(self): self.assertTrue(True)\n",
                        encoding="utf-8",
                    )
                    write_json(
                        turn_cwd / "intent" / "intent-test-source.json",
                        {
                            "schema_version": "intent-test-source/v1",
                            "generated_tests": [
                                {
                                    "test_id": "ITV-001",
                                    "target_test_ids": ["ITP-001"],
                                    "path": declared_path,
                                    "command": [
                                        sys.executable,
                                        "-m",
                                        "unittest",
                                        declared_path,
                                    ],
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

    def test_preflight_agent_repair_attempts_are_strictly_bounded(self) -> None:
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
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated.custom",
                "candidate\n",
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})

            class NonRepairingCodex:
                def __init__(self) -> None:
                    self.calls = 0

                def run_turn(self, **_kwargs):
                    self.calls += 1
                    return SimpleNamespace(duration_ms=1)

            codex = NonRepairingCodex()
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
                    max_attempts=2,
                )

        self.assertEqual(codex.calls, 2)
        self.assertEqual(payload["tests"][0]["reason_code"], "command_missing")

    def test_failed_optional_repair_turn_degrades_to_structured_blocked_evidence(self) -> None:
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
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated.custom",
                "candidate\n",
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})

            class FailingCodex:
                def run_turn(self, **_kwargs):
                    raise TimeoutError("repair turn timed out")

            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            with patch("pullwise_worker.review_worker_v1.effort_for_phase", return_value="medium"), patch(
                "pullwise_worker.review_worker_v1.turn_timeout_for_job",
                return_value=30,
            ):
                payload = worker.repair_intent_test_preflight(
                    FailingCodex(),
                    root / "repo",
                    run_dir,
                    {},
                    max_attempts=2,
                )
            log = (run_dir / "worker.log.jsonl").read_text(encoding="utf-8")

        self.assertEqual(payload["tests"][0]["reason_code"], "command_missing")
        self.assertIn("intent_test_execution_repair_failed", log)

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
                defer_inventory_baseline=True,
            )
            (validation_repo / "app.py").write_text("def value(): return 7\n", encoding="utf-8")
            _establish_immutable_validation_baseline(
                run_dir,
                validation_repo,
                ["app.py"],
            )
            generated_source, generated_path = _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated_test.py",
                "import unittest\n"
                "import unneeded_project_dependency\n"
                "from app import value\n"
                "class GeneratedTest(unittest.TestCase):\n"
                "    def test_behavior(self): self.assertEqual(value(), 7)\n",
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                initial = run_intent_tests(run_dir)
            self.assertEqual(initial["test_runs"][0]["status"], "failed")
            write_json(run_dir / "intent" / "intent-test-results.raw.json", initial)

            class RepairingCodex:
                def __init__(self) -> None:
                    self.calls = 0

                def run_turn(self, **kwargs):
                    self.calls += 1
                    repaired_source = (
                        "import unittest\n"
                        "from app import value\n"
                        "class GeneratedTest(unittest.TestCase):\n"
                        "    def test_behavior(self): self.assertEqual(value(), 7)\n"
                    )
                    _write_staged_generated_test(
                        kwargs,
                        "generated_test.py",
                        repaired_source,
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

    def test_runtime_repair_reruns_only_repairable_tests_and_preserves_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={
                    "schema_version": "intent-test-plan/v1",
                    "test_targets": [{"test_id": "ITP-pass"}, {"test_id": "ITP-repair"}],
                },
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-pass",
                            "target_test_ids": ["ITP-pass"],
                            "path": "test_pass.py",
                            "command": [sys.executable, "-m", "unittest", "test_pass.py"],
                        },
                        {
                            "test_id": "ITV-repair",
                            "target_test_ids": ["ITP-repair"],
                            "path": "test_repair.py",
                            "command": [sys.executable, "-m", "unittest", "test_repair.py"],
                        },
                    ],
                },
            )
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "test_pass.py",
                "import unittest\n"
                "class PassTest(unittest.TestCase):\n"
                "    def test_behavior(self): self.assertEqual(3 * 3, 9)\n",
            )
            repair_source, repair_path = _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "test_repair.py",
                "import unittest\n"
                "import unavailable_harness_helper\n"
                "class RepairTest(unittest.TestCase):\n"
                "    def test_behavior(self): self.assertTrue(True)\n",
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                initial = run_intent_tests(run_dir)
            write_json(run_dir / "intent" / "intent-test-results.raw.json", initial)

            class SelectiveRepairCodex:
                def run_turn(self, **kwargs):
                    repaired_source = (
                        "import unittest\n"
                        "class RepairTest(unittest.TestCase):\n"
                        "    def test_behavior(self): self.assertTrue(True)\n"
                    )
                    _write_staged_generated_test(
                        kwargs,
                        "test_repair.py",
                        repaired_source,
                    )
                    return SimpleNamespace(duration_ms=5)

            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"), patch(
                "pullwise_worker.review_worker_v1.effort_for_phase",
                return_value="medium",
            ), patch("pullwise_worker.review_worker_v1.turn_timeout_for_job", return_value=30):
                repaired = worker.repair_intent_test_runtime(
                    SelectiveRepairCodex(),
                    root / "repo",
                    run_dir,
                    {},
                    max_attempts=1,
                )
            history = json.loads(
                (run_dir / "intent" / "intent-test-execution-history.json").read_text(encoding="utf-8")
            )

        by_id = {item["test_id"]: item for item in repaired["test_runs"]}
        self.assertEqual(by_id["ITV-pass"]["status"], "passed")
        self.assertEqual(by_id["ITV-pass"]["attempt"], 1)
        self.assertEqual(by_id["ITV-repair"]["status"], "passed")
        self.assertEqual(by_id["ITV-repair"]["attempt"], 2)
        self.assertEqual([item["test_id"] for item in history["attempts"][1]["test_runs"]], ["ITV-repair"])

    def test_runtime_repair_is_rejected_when_agent_modifies_repository_source(self) -> None:
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
                defer_inventory_baseline=True,
            )
            source_repo = root / "repo"
            source_repo.mkdir(parents=True, exist_ok=True)
            original = b"def value(): return 7\n"
            (source_repo / "app.py").write_bytes(original)
            ensure_immutable_inventory_baseline(source_repo, run_dir)
            validation_app = validation_repo / "app.py"
            validation_app.write_bytes(original)
            generated_source, generated_path = _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated_test.py",
                "import unittest\n"
                "import unneeded_project_dependency\n"
                "from app import value\n"
                "class GeneratedTest(unittest.TestCase):\n"
                "    def test_behavior(self): self.assertEqual(value(), 7)\n",
            )
            write_json(
                run_dir / "inventory.json",
                {
                    "schema_version": "inventory/v1",
                    "files": [{"path": "app.py", "sha256": hashlib.sha256(original).hexdigest()}],
                },
            )
            write_json(run_dir / "run-state.json", {"thread_id": "thread-1"})
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                initial = run_intent_tests(run_dir)
            write_json(run_dir / "intent" / "intent-test-results.raw.json", initial)

            class SourceMutatingCodex:
                def run_turn(self, **kwargs):
                    validation_app.write_text("def value(): return 7  # agent changed source\n", encoding="utf-8")
                    repaired_source = (
                        "import unittest\n"
                        "from app import value\n"
                        "class GeneratedTest(unittest.TestCase):\n"
                        "    def test_behavior(self): self.assertEqual(value(), 7)\n"
                    )
                    _write_staged_generated_test(
                        kwargs,
                        "generated_test.py",
                        repaired_source,
                    )
                    return SimpleNamespace(duration_ms=5)

            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"), patch(
                "pullwise_worker.review_worker_v1.effort_for_phase",
                return_value="medium",
            ), patch("pullwise_worker.review_worker_v1.turn_timeout_for_job", return_value=30):
                repaired = worker.repair_intent_test_runtime(
                    SourceMutatingCodex(),
                    source_repo,
                    run_dir,
                    {},
                    max_attempts=1,
                )
            history = json.loads(
                (run_dir / "intent" / "intent-test-execution-history.json").read_text(encoding="utf-8")
            )

        self.assertEqual(repaired["test_runs"][0]["status"], "failed")
        self.assertEqual(repaired["test_runs"][0]["attempt"], 1)
        self.assertEqual(history["repair_rejections"][0]["reason_code"], "validation_workspace_modified")

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
            _write_canonical_generated_test(
                run_dir,
                validation_repo,
                "generated_test.py",
                "import unittest\n"
                "class GeneratedTest(unittest.TestCase):\n"
                "    def test_real_process(self):\n"
                "        self.assertEqual(sum([1, 2, 3]), 6)\n",
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)
            stderr = Path(result["test_runs"][0].get("stderr_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )
            stdout = Path(result["test_runs"][0].get("stdout_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(result["test_runs"][0]["status"], "passed", stdout + "\n" + stderr)
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
                            "reuse_existing": True,
                            "cwd": "packages/web",
                            "command": [node, "--test", "generated.intent.test.mjs"],
                        }
                    ],
                },
                defer_inventory_baseline=True,
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
            _establish_immutable_validation_baseline(
                run_dir,
                validation_repo,
                [
                    "packages/web/package.json",
                    "packages/web/generated.intent.test.mjs",
                ],
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)
            stderr = Path(result["test_runs"][0].get("stderr_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(result["test_runs"][0]["status"], "passed", stderr)
        self.assertEqual(result["test_runs"][0]["exit_code"], 0)
        self.assertEqual(result["test_runs"][0]["cwd"], str(workspace))

    @unittest.skipUnless(shutil.which("go"), "Go is required for the real agentic runner test")
    def test_real_go_test_executes_in_nested_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            go = str(Path(shutil.which("go") or "go").resolve())
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "services/math/value_test.go",
                            "reuse_existing": True,
                            "cwd": "services/math",
                            "command": [go, "test", "./..."],
                        }
                    ],
                },
                defer_inventory_baseline=True,
                per_test_timeout_seconds=60,
            )
            workspace = validation_repo / "services" / "math"
            workspace.mkdir(parents=True)
            (workspace / "go.mod").write_text("module example.com/intentmath\n\ngo 1.20\n", encoding="utf-8")
            (workspace / "value.go").write_text(
                "package intentmath\n\nfunc Value() int { return 7 }\n",
                encoding="utf-8",
            )
            (workspace / "value_test.go").write_text(
                "package intentmath\n\n"
                'import "testing"\n\n'
                "func TestValue(t *testing.T) {\n"
                "    if Value() != 7 { t.Fatalf(\"unexpected value: %d\", Value()) }\n"
                "}\n",
                encoding="utf-8",
            )
            _establish_immutable_validation_baseline(
                run_dir,
                validation_repo,
                [
                    "services/math/go.mod",
                    "services/math/value.go",
                    "services/math/value_test.go",
                ],
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)
            stderr = Path(result["test_runs"][0].get("stderr_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(result["test_runs"][0]["status"], "passed", stderr)
        self.assertEqual(result["test_runs"][0]["exit_code"], 0)

    @unittest.skipUnless(shutil.which("dotnet"), ".NET SDK is required for the generic real runner test")
    def test_real_dotnet_agent_proposal_executes_without_a_framework_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dotnet = str(Path(shutil.which("dotnet") or "dotnet").resolve())
            build_root = (
                root
                / "validation-repo"
                / ".codex-review"
                / "build"
                / "dotnet"
            )
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "services/dotnet/Program.cs",
                            "reuse_existing": True,
                            "cwd": "services/dotnet",
                            "command": [
                                dotnet,
                                "run",
                                "--project",
                                "IntentTest.csproj",
                                "--configuration",
                                "Release",
                                (
                                    "--property:BaseOutputPath="
                                    + str(build_root / "bin")
                                    + os.sep
                                ),
                                (
                                    "--property:BaseIntermediateOutputPath="
                                    + str(build_root / "obj")
                                    + os.sep
                                ),
                                (
                                    "--property:MSBuildProjectExtensionsPath="
                                    + str(build_root / "obj")
                                    + os.sep
                                ),
                            ],
                        }
                    ],
                },
                defer_inventory_baseline=True,
            )
            workspace = validation_repo / "services" / "dotnet"
            workspace.mkdir(parents=True)
            (workspace / "IntentTest.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk">\n'
                "  <PropertyGroup>\n"
                "    <OutputType>Exe</OutputType>\n"
                "    <TargetFramework>net8.0</TargetFramework>\n"
                "    <ImplicitUsings>disable</ImplicitUsings>\n"
                "    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>\n"
                "    <GenerateTargetFrameworkAttribute>false</GenerateTargetFrameworkAttribute>\n"
                "  </PropertyGroup>\n"
                "</Project>\n",
                encoding="utf-8",
            )
            (workspace / "Program.cs").write_text(
                "if (2 + 3 != 5) System.Environment.Exit(1);\n"
                'System.Console.WriteLine("agentic intent check passed");\n',
                encoding="utf-8",
            )
            _establish_immutable_validation_baseline(
                run_dir,
                validation_repo,
                [
                    "services/dotnet/IntentTest.csproj",
                    "services/dotnet/Program.cs",
                ],
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)
            stderr = Path(result["test_runs"][0].get("stderr_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )
            stdout = Path(result["test_runs"][0].get("stdout_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(
            result["test_runs"][0]["status"],
            "passed",
            stdout
            + "\n"
            + stderr
            + "\n"
            + json.dumps(
                result["test_runs"][0].get("workspace_integrity", {}),
                sort_keys=True,
            ),
        )
        self.assertEqual(result["test_runs"][0]["exit_code"], 0)

    @unittest.skipIf(
        sys.platform == "win32",
        "Rust leaves transient toolchain handles on Windows; the worker production target is Linux",
    )
    @unittest.skipUnless(
        shutil.which("cargo") and shutil.which("rustc"),
        "Rust is required for the real agentic runner test",
    )
    def test_real_rust_test_executes_with_isolated_runtime_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            # Preserve the cargo shim basename; resolving it can turn a rustup
            # multicall proxy into an explicit rustup invocation.
            cargo = str(Path(shutil.which("cargo") or "cargo").absolute())
            run_dir, validation_repo = _write_intent_run(
                root,
                plan={"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITP-001"}]},
                source={
                    "schema_version": "intent-test-source/v1",
                    "generated_tests": [
                        {
                            "test_id": "ITV-001",
                            "target_test_ids": ["ITP-001"],
                            "path": "crates/math/src/lib.rs",
                            "reuse_existing": True,
                            "cwd": "crates/math",
                            "command": [cargo, "test", "--offline"],
                        }
                    ],
                },
                defer_inventory_baseline=True,
            )
            workspace = validation_repo / "crates" / "math"
            (workspace / "src").mkdir(parents=True)
            (workspace / "Cargo.toml").write_text(
                "[package]\nname = \"intent_math\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
                encoding="utf-8",
            )
            (workspace / "src" / "lib.rs").write_text(
                "pub fn value() -> i32 { 7 }\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    use super::*;\n"
                "    #[test]\n"
                "    fn behavior() { assert_eq!(value(), 7); }\n"
                "}\n",
                encoding="utf-8",
            )
            _establish_immutable_validation_baseline(
                run_dir,
                validation_repo,
                ["crates/math/Cargo.toml", "crates/math/src/lib.rs"],
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)
            stderr = Path(result["test_runs"][0].get("stderr_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )
            stdout = Path(result["test_runs"][0].get("stdout_path") or "").read_text(
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(result["test_runs"][0]["status"], "passed", stdout + "\n" + stderr)
        self.assertEqual(result["test_runs"][0]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
