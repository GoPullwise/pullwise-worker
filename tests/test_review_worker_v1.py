from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from pullwise_worker import __version__
from pullwise_worker._main_part_01_bootstrap import (
    PULLWISE_WORKER_USER_AGENT,
    PullwiseClient,
    PullwiseResponse,
    WorkerConfig,
    worker_registration_payload,
)
from pullwise_worker._main_part_07_readiness_doctor import run_doctor, subscription_plan_agent_configs_validation_error, writable_path_check
from pullwise_worker.review_worker_v1 import (
    INTENT_TEST_CLASSIFICATIONS,
    PIPELINE_PHASES,
    REQUIRED_COMPLETED_ARTIFACTS,
    REQUIRED_PROMPT_FILES,
    REQUIRED_SCHEMA_FILES,
    REQUIRED_TOOL_FILES,
    SEMANTIC_PHASES,
    SEMANTIC_PHASE_PROMPT_SPECS,
    ActiveJob,
    approval_response_for_request,
    CodexQuotaMonitor,
    JobCancelled,
    JsonRpcAppServer,
    ReviewWorkerV1,
    WorkerState,
    artifact_manifest_items,
    codex_error_code,
    codex_quota_payload_from_rate_limits,
    quota_refresh_error_is_exhaustion,
    decide_approval,
    default_agent_report,
    effort_for_phase,
    fallback_semantic_artifact,
    model_for_job,
    review_worker_policy_for_job,
    turn_timeout_for_job,
    result_payload,
    bundle_plan_payload,
    phase_completion_data,
    phase_progress_data,
    phase_prompt,
    progress_final_payload,
    inventory,
    materialize_artifacts,
    materialize_terminal_artifacts,
    pack_bundles,
    prepare_validation_workspace,
    qa_gate_payload,
    run_intent_tests,
    scoped_codex_command,
    upload_artifacts,
    upload_log_artifacts,
    validate_job_policy,
    validate_phase_outputs,
    validate_reviewer_outputs,
    write_json,
)


def write_completed_artifact_inputs(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text("# Codex Full Repository Review Report\n", encoding="utf-8")
    (run_dir / "report.agent.json").write_text(
        json.dumps({"schema_id": "codex-full-repo-review", "schema_version": "v1", "findings": []}),
        encoding="utf-8",
    )
    (run_dir / "coverage.json").write_text(
        json.dumps(
            {
                "schema_version": "coverage/v1",
                "source_like_files_total": 0,
                "deep_reviewed_files": 0,
                "standard_reviewed_files": 0,
                "light_reviewed_files": 0,
                "inventory_only_files": 0,
                "skipped_files": 0,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "token-budget.json").write_text(json.dumps({"schema_version": "token-budget/v1"}), encoding="utf-8")
    (run_dir / "qa.json").write_text(
        json.dumps({"schema_version": "qa/v1", "status": "pass", "errors": [], "warnings": []}),
        encoding="utf-8",
    )


class ReviewWorkerV1ContractsTest(unittest.TestCase):
    def test_worker_state_allows_lease_only_when_idle_without_active_job(self) -> None:
        state = WorkerState()
        state.state = "idle"
        self.assertEqual(state.local_queue_depth, 0)
        self.assertEqual(state.available_job_slots, 1)
        self.assertTrue(state.can_lease())

        state.set_active(ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk-1"))
        self.assertFalse(state.can_lease())
        self.assertEqual(state.available_job_slots, 0)
        with self.assertRaisesRegex(RuntimeError, "active job"):
            state.set_active(ActiveJob(job_id="job_2", run_id="run_2", lease_id="lease_2", attempt_id="wk-1"))

        state.clear_active("completed")
        self.assertTrue(state.can_lease())

    def test_progress_events_and_heartbeats_include_worker_reported_steps(self) -> None:
        events = []

        class Client:
            def event(self, _run_id: str, event: dict) -> dict:
                events.append(event)
                return {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir)
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=tmp_dir), client=Client())

            event = worker.emit_event(
                active,
                run_dir,
                "progress_updated",
                "repo_map",
                progress=33,
                current_phase_percent=40,
                message="Mapping repository",
            )
            snapshot = active.progress_snapshot()

        self.assertEqual(len(event["progress"]["steps"]), len(PIPELINE_PHASES))
        self.assertEqual(len(snapshot["steps"]), len(PIPELINE_PHASES))
        repo_step = next(step for step in event["progress"]["steps"] if step["id"] == "repo_map")
        self.assertEqual(repo_step["status"], "running")
        self.assertEqual(repo_step["percent"], 40)
        self.assertEqual(events[0]["progress"]["steps"], event["progress"]["steps"])

    def test_codex_turn_interrupts_when_cancel_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            calls = []

            class AppServer(JsonRpcAppServer):
                def __init__(self) -> None:
                    super().__init__("codex", {}, workspace, workspace / "events.jsonl")

                def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                    calls.append((method, params or {}))
                    if method == "turn/start":
                        return {"turnId": "turn_1"}
                    return {}

            server = AppServer()

            with self.assertRaises(JobCancelled):
                server.run_turn(
                    thread_id="thread_1",
                    repo_dir=workspace,
                    prompt="review",
                    effort="medium",
                    read_only=True,
                    timeout_seconds=2,
                    cancel_requested=lambda: True,
                )

        self.assertIn(("turn/interrupt", {"threadId": "thread_1", "turnId": "turn_1"}), calls)

    def test_codex_app_server_uses_current_sandbox_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            requests = []

            class AppServer(JsonRpcAppServer):
                def __init__(self) -> None:
                    super().__init__("codex", {}, workspace, workspace / "events.jsonl")

                def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                    requests.append((method, params or {}))
                    if method == "thread/start":
                        return {"threadId": "thread_1"}
                    if method == "turn/start":
                        return {"turnId": ""}
                    return {}

            server = AppServer()
            thread_id = server.start_thread(workspace, "gpt-5.5")
            server.run_turn(
                thread_id=thread_id,
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=2,
            )
            server.run_turn(
                thread_id=thread_id,
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=False,
                timeout_seconds=2,
            )

        thread_start = requests[0][1]
        read_only_turn = requests[1][1]
        write_turn = requests[2][1]
        self.assertEqual(thread_start["sandbox"], "workspace-write")
        self.assertNotIn("personality", thread_start)
        self.assertEqual(read_only_turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(write_turn["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertNotIn('read-only', json.dumps(requests))
        self.assertNotIn('danger-full-access', json.dumps(requests))
        self.assertNotIn("precise", json.dumps(requests))

    def test_codex_app_server_caches_supported_thread_sandbox_mode_after_variant_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            requests = []

            class AppServer(JsonRpcAppServer):
                def __init__(self) -> None:
                    super().__init__("codex", {}, workspace, workspace / "events.jsonl")

                def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                    payload = params or {}
                    requests.append((method, payload))
                    if method == "thread/start" and payload.get("sandbox") == "workspace-write":
                        raise RuntimeError("Invalid request: unknown variant `workspace-write`, expected one of `workspaceWrite`")
                    if method == "thread/start":
                        return {"threadId": "thread_1"}
                    return {}

            server = AppServer()
            first_thread_id = server.start_thread(workspace, "gpt-5.5")
            second_thread_id = server.start_thread(workspace, "gpt-5.5")

        thread_sandboxes = [payload["sandbox"] for method, payload in requests if method == "thread/start"]
        self.assertEqual(first_thread_id, "thread_1")
        self.assertEqual(second_thread_id, "thread_1")
        self.assertEqual(thread_sandboxes, ["workspace-write", "workspaceWrite", "workspaceWrite"])

    def test_codex_app_server_caches_supported_turn_sandbox_policy_after_variant_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            requests = []

            class AppServer(JsonRpcAppServer):
                def __init__(self) -> None:
                    super().__init__("codex", {}, workspace, workspace / "events.jsonl")

                def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                    payload = params or {}
                    requests.append((method, payload))
                    if method == "turn/start" and payload.get("sandboxPolicy", {}).get("type") == "readOnly":
                        raise RuntimeError("Invalid request: unknown variant `readOnly`, expected one of `read-only`")
                    if method == "turn/start":
                        return {"turnId": ""}
                    return {}

            server = AppServer()
            server.run_turn(
                thread_id="thread_1",
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=2,
            )
            server.run_turn(
                thread_id="thread_1",
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=2,
            )

        policy_types = [payload["sandboxPolicy"]["type"] for method, payload in requests if method == "turn/start"]
        self.assertEqual(policy_types, ["readOnly", "read-only", "read-only"])

    def test_writable_path_check_uses_available_no_follow_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "logs"

            ok, detail = writable_path_check(path)

        self.assertTrue(ok, detail)

    def test_codex_app_server_start_uses_stdio_and_initialize_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            calls = []
            launched = []

            class Process:
                stdin = None
                stdout = None
                stderr = None

            class AppServer(JsonRpcAppServer):
                def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                    calls.append(("request", method, params or {}, timeout_seconds))
                    return {}

                def notify(self, method: str, params: dict | None = None) -> None:
                    calls.append(("notify", method, params or {}, 0))

                def _reader(self) -> None:
                    return

            def popen(args: list[str], **kwargs: object) -> Process:
                launched.append((args, kwargs))
                return Process()

            with patch("pullwise_worker.review_worker_v1.subprocess.Popen", popen):
                server = AppServer("/opt/pullwise/codex", {"CODEX_HOME": str(workspace / "codex-home")}, workspace, workspace / "events.jsonl")
                server.start()

        self.assertEqual(launched[0][0], ["/opt/pullwise/codex", "app-server", "--listen", "stdio://"])
        self.assertEqual(launched[0][1]["cwd"], str(workspace))
        initialize = calls[0]
        self.assertEqual(initialize[1], "initialize")
        self.assertEqual(initialize[2]["clientInfo"]["name"], "codex_repo_review_worker")
        self.assertEqual(initialize[2]["capabilities"], {"experimentalApi": False})
        self.assertEqual(calls[1], ("notify", "initialized", {}, 0))

    def test_scoped_codex_command_defaults_inside_service_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service_home = Path(tmp_dir) / "service"
            command = scoped_codex_command(SimpleNamespace(service_home=str(service_home)))

        self.assertEqual(command, str(service_home / "workers" / "worker" / ".local" / "bin" / "codex"))

    def test_worker_config_default_codex_command_matches_installer_path(self) -> None:
        service_home = "/var/lib/pullwise-worker/wk_test"
        with patch.dict(
            os.environ,
            {
                "PULLWISE_SERVER_URL": "http://127.0.0.1:18080",
                "PULLWISE_WORKER_TOKEN": "pww_test",
                "PULLWISE_WORKER_ID": "wk_test",
                "PULLWISE_SERVICE_HOME": service_home,
            },
            clear=False,
        ):
            config = WorkerConfig(SimpleNamespace(), validate_server_url=False)

        self.assertEqual(config.codex_command, f"{service_home}/workers/wk_test/.local/bin/codex")
        self.assertEqual(config.worker_root, f"{service_home}/workers/wk_test")
        self.assertEqual(config.codex_home, f"{service_home}/workers/wk_test/codex-home")

    def test_subscription_plan_agent_config_validation_accepts_codex_config(self) -> None:
        plan_configs = {
            plan: {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "medium"}}
            for plan in ("free", "pro", "max")
        }

        self.assertEqual(subscription_plan_agent_configs_validation_error(plan_configs), "")

    def test_subscription_plan_agent_config_validation_rejects_bad_codex_config(self) -> None:
        missing_model = {
            plan: {"provider": "codex", "codex": {"reasoningEffort": "medium"}}
            for plan in ("free", "pro", "max")
        }
        bad_effort = {
            plan: {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "extreme"}}
            for plan in ("free", "pro", "max")
        }

        self.assertEqual(
            subscription_plan_agent_configs_validation_error(missing_model),
            "subscription plan agent configs invalid: free.codex.model is required",
        )
        self.assertEqual(
            subscription_plan_agent_configs_validation_error(bad_effort),
            "subscription plan agent configs invalid: free.codex.reasoningEffort is required",
        )

    def test_doctor_does_not_require_node_for_standalone_codex_cli(self) -> None:
        config = SimpleNamespace(
            provider="codex",
            provider_chain=["codex"],
            service_name="pullwise-worker-test",
            worker_id="wk_1",
        )
        heartbeats = []

        class Client:
            def __init__(self, _config: object) -> None:
                pass

            def heartbeat(self, **payload: object) -> None:
                heartbeats.append(payload)

        checks = [("provider_ready", True, "codex"), ("codex_ready", True, "ready")]
        with patch(
            "pullwise_worker._main_part_07_readiness_doctor.install_ubuntu_2204_dependencies",
            return_value=(True, "dependencies present"),
        ) as dependencies, patch(
            "pullwise_worker._main_part_07_readiness_doctor.worker_readiness_state",
            return_value=(checks, True, ["codex"]),
        ), patch(
            "pullwise_worker._main_part_07_readiness_doctor.command_ok",
            return_value=(True, "active"),
        ), patch(
            "pullwise_worker._main_part_07_readiness_doctor.PullwiseClient",
            Client,
        ):
            self.assertTrue(run_doctor(config))

        dependencies.assert_called_once_with(["git", "bwrap"])
        self.assertEqual(heartbeats[0]["ready_providers"], ["codex"])

    def test_doctor_preflight_can_skip_systemd_active_requirement(self) -> None:
        config = SimpleNamespace(
            provider="codex",
            provider_chain=["codex"],
            service_name="pullwise-worker-test",
            worker_id="wk_1",
            doctor_require_systemd_active=False,
        )
        heartbeats = []

        class Client:
            def __init__(self, config: object) -> None:
                self.config = config

            def heartbeat(self, **payload: object) -> None:
                heartbeats.append(payload)

        checks = [("provider_ready", True, "codex"), ("codex_ready", True, "ready")]
        with patch(
            "pullwise_worker._main_part_07_readiness_doctor.install_ubuntu_2204_dependencies",
            return_value=(True, "dependencies present"),
        ), patch(
            "pullwise_worker._main_part_07_readiness_doctor.worker_readiness_state",
            return_value=(checks, True, ["codex"]),
        ), patch(
            "pullwise_worker._main_part_07_readiness_doctor.command_ok",
            return_value=(False, "inactive"),
        ), patch(
            "pullwise_worker._main_part_07_readiness_doctor.PullwiseClient",
            Client,
        ):
            self.assertTrue(run_doctor(config))

        self.assertFalse(heartbeats[0]["systemd_active"])
        self.assertEqual(heartbeats[0]["doctor_status"], "ok")

    def test_scoped_codex_command_rejects_global_or_relative_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service_home = Path(tmp_dir) / "service"
            with self.assertRaisesRegex(RuntimeError, "inside worker_root"):
                scoped_codex_command(SimpleNamespace(service_home=str(service_home), codex_command="/usr/bin/codex"))
            with self.assertRaisesRegex(RuntimeError, "absolute path"):
                scoped_codex_command(SimpleNamespace(service_home=str(service_home), codex_command="codex"))

    def test_codex_quota_refresh_rejects_unscoped_codex_command_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SimpleNamespace(worker_id="wk_1", service_home=str(Path(tmp_dir) / "service"), codex_command="/usr/bin/codex")
            worker = ReviewWorkerV1(config, client=object())
            with patch("pullwise_worker.review_worker_v1.subprocess.Popen") as popen:
                snapshot = worker.quota_monitor.refresh(current_time=123)

        popen.assert_not_called()
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("inside worker_root", snapshot["lastError"])

    def test_approval_policy_allows_only_review_workspace_and_safe_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / ".codex-review" / "tools").mkdir(parents=True)
            (workspace.parent / "validation-repo").mkdir(parents=True, exist_ok=True)

            allowed_file, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "fileChange", "paths": [".codex-review/runs/run_1/out.json"]}},
                workspace,
            )
            allowed_validation_file, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "fileChange", "paths": [str(workspace.parent / "validation-repo" / "tests" / "itv.test.js")]}},
                workspace,
            )
            denied_file, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "fileChange", "paths": ["src/app.py"]}},
                workspace,
            )
            allowed_command, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "python3 .codex-review/tools/scan.py"}},
                workspace,
            )
            denied_install, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "npm install"}},
                workspace,
            )
            allowed_validation_test, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "npm test -- itv.test.js", "cwd": str(workspace.parent / "validation-repo")}},
                workspace,
            )
            allowed_git_status, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "git status --short", "cwd": str(workspace)}},
                workspace,
            )
            denied_cwd, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "git status", "cwd": ".."}},
                workspace,
            )
            denied_git_clean, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "git clean -fdx", "cwd": str(workspace)}},
                workspace,
            )
            denied_sed_in_place, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "sed -i s/a/b/ src/app.py", "cwd": str(workspace)}},
                workspace,
            )

        self.assertEqual(allowed_file, "acceptForSession")
        self.assertEqual(allowed_validation_file, "acceptForSession")
        self.assertEqual(denied_file, "decline")
        self.assertEqual(allowed_command, "acceptForSession")
        self.assertEqual(denied_install, "decline")
        self.assertEqual(allowed_validation_test, "acceptForSession")
        self.assertEqual(allowed_git_status, "acceptForSession")
        self.assertEqual(denied_cwd, "decline")
        self.assertEqual(denied_git_clean, "decline")
        self.assertEqual(denied_sed_in_place, "decline")

    def test_codex_approval_responses_use_current_app_server_enums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / ".codex-review" / "tools").mkdir(parents=True)

            command_response = approval_response_for_request(
                {
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "python3 .codex-review/tools/scan.py", "cwd": str(workspace)},
                },
                workspace,
            )
            file_response = approval_response_for_request(
                {"method": "item/fileChange/requestApproval", "params": {"grantRoot": str(workspace / ".codex-review")}},
                workspace,
            )
            denied_response = approval_response_for_request(
                {"method": "item/fileChange/requestApproval", "params": {"grantRoot": str(workspace / "src")}},
                workspace,
            )

        self.assertEqual(command_response, {"decision": "acceptForSession"})
        self.assertEqual(file_response, {"decision": "acceptForSession"})
        self.assertEqual(denied_response, {"decision": "decline"})

    def test_legacy_codex_approval_responses_use_legacy_review_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / ".codex-review" / "tools").mkdir(parents=True)

            exec_response = approval_response_for_request(
                {"method": "execCommandApproval", "params": {"command": ["python3", ".codex-review/tools/scan.py"], "cwd": str(workspace)}},
                workspace,
            )
            patch_response = approval_response_for_request(
                {"method": "applyPatchApproval", "params": {"fileChanges": {".codex-review/out.json": {"type": "add", "content": "{}"}}}},
                workspace,
            )
            denied_response = approval_response_for_request(
                {"method": "execCommandApproval", "params": {"command": ["npm", "install"], "cwd": str(workspace)}},
                workspace,
            )

        self.assertEqual(exec_response, {"decision": "approved_for_session"})
        self.assertEqual(patch_response, {"decision": "approved_for_session"})
        self.assertEqual(denied_response, {"decision": "denied"})

    def test_pipeline_has_explicit_codex_auth_check_before_bootstrap(self) -> None:
        phases = [phase for phase, _progress in PIPELINE_PHASES]

        self.assertLess(phases.index("initialize_codex_connection"), phases.index("check_codex_auth"))
        self.assertLess(phases.index("check_codex_auth"), phases.index("bootstrap_helper_scripts"))
        self.assertLess(phases.index("clustering_and_voting"), phases.index("intent_test_validation"))
        self.assertLess(phases.index("intent_test_failure_analysis"), phases.index("validator_disproof"))
        self.assertLess(phases.index("upload_artifacts"), phases.index("submit_result_envelope"))
        self.assertLess(phases.index("submit_result_envelope"), phases.index("cleanup_active_job"))

    def test_review_tree_includes_intent_validation_assets(self) -> None:
        self.assertIn("11_prepare_validation_workspace.py", REQUIRED_TOOL_FILES)
        self.assertIn("14_validate_intent_test_json.py", REQUIRED_TOOL_FILES)
        self.assertIn("intent-map.schema.json", REQUIRED_SCHEMA_FILES)
        self.assertIn("intent-test-plan.schema.json", REQUIRED_SCHEMA_FILES)
        self.assertIn("intent-test-result.schema.json", REQUIRED_SCHEMA_FILES)
        self.assertIn("intent/04_intent_miner.md", REQUIRED_PROMPT_FILES)
        self.assertIn("intent/07_intent_test_failure_analyzer.md", REQUIRED_PROMPT_FILES)
        self.assertIn("plausible_bug", INTENT_TEST_CLASSIFICATIONS)
        self.assertIn("passed_no_bug_reproduced", INTENT_TEST_CLASSIFICATIONS)
        self.assertIn("skipped_not_runnable", INTENT_TEST_CLASSIFICATIONS)

    def test_job_policy_requires_canonical_v1_policy_and_repository_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_profile.default_model"):
            validate_job_policy({
                "agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "high"}},
                "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000},
            })
        with self.assertRaisesRegex(ValueError, "model_profile.core_effort"):
            validate_job_policy({
                "model_profile": {"default_model": "gpt-5.5"},
                "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000},
            })
        with self.assertRaisesRegex(ValueError, "turn_timeout_seconds"):
            validate_job_policy({
                "model_profile": {"default_model": "gpt-5.5", "core_effort": "high"},
                "agentConfig": {"provider": "codex", "reviewWorker": {"turnTimeoutSeconds": 1800}},
                "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000},
            })
        with self.assertRaisesRegex(ValueError, "max_wall_time_seconds"):
            validate_job_policy({
                "model_profile": {"default_model": "gpt-5.5", "core_effort": "high"},
                "review_request": {
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 1800,
                    },
                },
                "agentConfig": {"provider": "codex", "reviewWorker": {"scanDeadlineSeconds": 14400}},
                "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000},
            })
        with self.assertRaisesRegex(ValueError, "repositoryLimits"):
            validate_job_policy({
                "model_profile": {"default_model": "gpt-5.5", "core_effort": "high"},
                "review_request": {
                    "budget": {"max_wall_time_seconds": 14400},
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 1800,
                    },
                },
            })
        unsafe_job = {
            "model_profile": {"default_model": "gpt-5.5", "core_effort": "high"},
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": True,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }
        with self.assertRaisesRegex(ValueError, "allow_dependency_install"):
            validate_job_policy(unsafe_job)

    def test_model_effort_and_timeout_come_from_job_policy(self) -> None:
        job = {
            "model_profile": {
                "default_model": "gpt-5.5",
                "core_effort": "high",
                "reviewer_effort": "high",
                "validator_effort": "high",
                "reporter_effort": "high",
                "intent_test_effort": "high",
                "non_core_effort": "medium",
            },
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                    "intent_test_validation": {
                        "enabled": True,
                        "only_tiers": ["P0"],
                        "max_tests_per_run": 7,
                        "max_tests_per_bundle": 1,
                        "max_test_run_seconds_per_test": 45,
                        "max_total_test_run_seconds": 315,
                    },
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        self.assertEqual(model_for_job(job), "gpt-5.5")
        self.assertEqual(turn_timeout_for_job(job), 1800)
        self.assertEqual(review_worker_policy_for_job(job)["scanDeadlineSeconds"], 14400)
        self.assertEqual(effort_for_phase(job, "reviewer_fanout"), "high")
        self.assertEqual(effort_for_phase(job, "inventory_repository"), "medium")
        parsed = validate_job_policy(job)["intent_test_validation"]
        self.assertEqual(parsed["max_tests_per_run"], 7)
        self.assertEqual(parsed["max_test_run_seconds_per_test"], 45)
        fallback_job = dict(job)
        fallback_job["review_request"] = {
            "budget": {"max_wall_time_seconds": 14400},
            "policy": {
                "allow_source_modification": False,
                "allow_dependency_install": False,
                "allow_network": False,
                "helper_scripts_standard_library_only": True,
                "turn_timeout_seconds": 1800,
            },
        }
        fallback_job["agentConfig"] = {
            "provider": "codex",
            "reviewWorker": {
                "intentTestValidation": {
                    "enabled": False,
                    "onlyTiers": ["P3"],
                    "maxTestsPerRun": 99,
                    "maxTestsPerBundle": 99,
                    "maxTestRunSecondsPerTest": 99,
                    "maxTotalTestRunSeconds": 99,
                },
            },
        }
        fallback_policy = validate_job_policy(fallback_job)["intent_test_validation"]
        self.assertTrue(fallback_policy["enabled"])
        self.assertEqual(fallback_policy["only_tiers"], ["P0", "P1"])
        self.assertEqual(fallback_policy["max_tests_per_run"], 20)
        self.assertEqual(fallback_policy["max_test_run_seconds_per_test"], 60)

    def test_disabled_intent_validation_skips_child_phases_without_codex_turns(self) -> None:
        events = []
        semantic_calls = []
        intent_child_phases = (
            "intent_mining",
            "intent_test_planning",
            "validation_workspace_prepare",
            "intent_test_writing",
            "intent_test_running",
            "intent_test_failure_analysis",
        )

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                return {}

            def event(self, _run_id: str, event: dict) -> dict:
                events.append(event)
                return {}

        class Worker(ReviewWorkerV1):
            def prepare_workspace(self, _job: dict, run_id: str) -> tuple[Path, Path, Path]:
                repo_dir = root / "repo"
                artifact_dir = root / "artifacts" / run_id
                run_dir = repo_dir / ".codex-review" / "runs" / run_id
                run_dir.mkdir(parents=True)
                artifact_dir.mkdir(parents=True)
                return repo_dir, run_dir, artifact_dir

            def run_semantic_phase(self, _app_server: object, _repo_dir: Path, _run_dir: Path, _job: dict, phase: str) -> None:
                semantic_calls.append(phase)
                raise AssertionError(f"semantic phase should have been skipped: {phase}")

        job = {
            "job_id": "job_1",
            "run_id": "run_1",
            "lease_id": "lease_1",
            "model_profile": {
                "default_model": "gpt-5.5",
                "core_effort": "high",
                "non_core_effort": "medium",
            },
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                    "intent_test_validation": {"enabled": False},
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = Worker(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            phases = (("intent_test_validation", 82),) + tuple((phase, 84 + index) for index, phase in enumerate(intent_child_phases))
            with patch("pullwise_worker.review_worker_v1.PIPELINE_PHASES", phases):
                worker.run_job(job)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            validation = json.loads((run_dir / "intent" / "intent-test-validation.json").read_text(encoding="utf-8"))

        self.assertFalse(validation["enabled"])
        self.assertEqual(semantic_calls, [])
        skipped = {event["phase"]: event for event in events if event["phase"] in intent_child_phases}
        self.assertEqual(set(skipped), set(intent_child_phases))
        for phase in intent_child_phases:
            self.assertEqual(skipped[phase]["event_type"], "phase_completed")
            self.assertEqual(skipped[phase]["progress"]["status"], "skipped")
            self.assertEqual(skipped[phase]["data"]["skip_reason"], "intent test validation disabled")

    def test_prepare_workspace_bootstraps_design_review_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))

            repo_dir, run_dir, _artifact_dir = worker.prepare_workspace({"job_id": "job_1", "checkout_dir": str(source)}, "run_1")

            review_root = repo_dir / ".codex-review"
            self.assertTrue((review_root / "AGENTS.review.md").is_file())
            self.assertTrue((run_dir / "bundles").is_dir())
            self.assertTrue(all((review_root / "tools" / name).is_file() for name in REQUIRED_TOOL_FILES))
            self.assertTrue(all((review_root / "schemas" / name).is_file() for name in REQUIRED_SCHEMA_FILES))
            self.assertTrue(all((review_root / "prompts" / name).is_file() for name in REQUIRED_PROMPT_FILES))
            self.assertTrue((run_dir / "intent" / "generated-tests").is_dir())

    def test_prepare_workspace_skips_checkout_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            source.mkdir()
            outside = root / "outside-secret.txt"
            outside.write_text("secret-from-outside\n", encoding="utf-8")
            (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
            try:
                os.symlink(outside, source / "linked-secret.txt")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not available in this environment")
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))

            repo_dir, _run_dir, _artifact_dir = worker.prepare_workspace(
                {"job_id": "job_1", "checkout_dir": str(source), "repositoryLimits": {"maxFiles": 10, "maxBytes": 4096}},
                "run_1",
            )

            self.assertTrue((repo_dir / "app.py").is_file())
            self.assertFalse((repo_dir / "linked-secret.txt").exists())

    def test_prepare_workspace_enforces_repository_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            source.mkdir()
            (source / "one.py").write_text("1\n", encoding="utf-8")
            (source / "two.py").write_text("2\n", encoding="utf-8")
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))

            with self.assertRaisesRegex(RuntimeError, "maxFiles"):
                worker.prepare_workspace(
                    {"job_id": "job_1", "checkout_dir": str(source), "repositoryLimits": {"maxFiles": 1, "maxBytes": 4096}},
                    "run_1",
                )

    def test_run_job_rejects_cloned_checkout_over_repository_limit_before_app_server(self) -> None:
        results = []

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                return {}

            def event(self, _run_id: str, _event: dict) -> dict:
                return {}

            def artifact(self, _job_id: str, _artifact_id: str, _payload: dict) -> dict:
                return {}

            def result(self, _job_id: str, payload: dict) -> None:
                results.append(payload)

        class Worker(ReviewWorkerV1):
            def ensure_app_server(self, events_path: Path | None = None) -> JsonRpcAppServer:
                raise AssertionError("app server should not start after repository limit precheck fails")

        job = {
            "job_id": "job_1",
            "run_id": "run_1",
            "lease_id": "lease_1",
            "repo": "acme/api",
            "repository": {"clone_url": "https://github.com/acme/api.git"},
            "branch": "main",
            "commit": "pending",
            "model_profile": {"default_model": "gpt-5.5", "core_effort": "high", "non_core_effort": "medium"},
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 100, "maxBytes": 2 * 1024 * 1024},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = Worker(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
                if "checkout" in args:
                    repo_dir = Path(args[args.index("-C") + 1])
                    (repo_dir / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("pullwise_worker.review_worker_v1.subprocess.run", side_effect=fake_run):
                worker.run_job(job)

        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["error_code"], "REPOSITORY_TOO_LARGE")
        self.assertTrue(results[0]["preflight"]["repositoryLimitExceeded"])
        self.assertEqual(results[0]["preflight"]["repositoryLimitReasons"], ["total_bytes"])
        self.assertEqual(results[0]["preflight"]["repositoryLimits"], {"maxFiles": 100, "maxBytes": 2 * 1024 * 1024})
        self.assertEqual(results[0]["preflight"]["repositoryStats"]["totalBytes"], 2 * 1024 * 1024 + 1)

    def test_prepare_workspace_clones_claimed_repository_when_checkout_dir_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            calls: list[list[str]] = []
            envs: list[dict[str, str]] = []

            def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append(args)
                env = kwargs.get("env") if isinstance(kwargs.get("env"), dict) else {}
                envs.append(dict(env))
                self.assertNotIn("secret-token", " ".join(args))
                if "checkout" in args:
                    repo_dir = Path(args[args.index("-C") + 1])
                    (repo_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("pullwise_worker.review_worker_v1.subprocess.run", side_effect=fake_run):
                repo_dir, run_dir, _artifact_dir = worker.prepare_workspace(
                    {
                        "job_id": "job_1",
                        "repository": {"clone_url": "https://github.com/acme/api.git"},
                        "branch": "main",
                        "commit": "pending",
                        "clone_token": {"token": "secret-token"},
                        "repositoryLimits": {"maxFiles": 10, "maxBytes": 4096},
                    },
                    "run_1",
                )

            self.assertTrue((repo_dir / "app.py").is_file())
            self.assertTrue((run_dir / "bundles").is_dir())
            self.assertTrue(any(call[:2] == ["git", "init"] for call in calls))
            self.assertTrue(any(call[:6] == ["git", "-C", str(repo_dir), "fetch", "--depth", "1"] for call in calls))
            self.assertTrue(any(env.get("PULLWISE_GIT_TOKEN") == "secret-token" for env in envs))
            self.assertFalse((repo_dir.parent / "git-askpass.sh").exists())

    def test_prepare_workspace_rejects_empty_checkout_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            source.mkdir()
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))

            with self.assertRaisesRegex(RuntimeError, "no repository files"):
                worker.prepare_workspace({"job_id": "job_1", "checkout_dir": str(source)}, "run_1")

    def test_prepare_workspace_requires_checkout_source_or_clone_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))

            with self.assertRaisesRegex(RuntimeError, "checkout_dir or repository.clone_url"):
                worker.prepare_workspace({"job_id": "job_1"}, "run_1")

    def test_inventory_bundle_plan_and_packing_are_v1_2_shaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            (repo / "src" / "auth").mkdir(parents=True)
            run_dir.mkdir(parents=True)
            (repo / "src" / "auth" / "session.py").write_text("def refresh_session():\n    return True\n", encoding="utf-8")
            (repo / "README.md").write_text("docs\n", encoding="utf-8")

            inv = inventory(repo)
            (run_dir / "inventory.json").write_text(__import__("json").dumps(inv), encoding="utf-8")
            plan = bundle_plan_payload(run_dir)
            (run_dir / "bundle-plan.json").write_text(__import__("json").dumps(plan), encoding="utf-8")
            pack_bundles(repo, run_dir)

            bundle = next(item for item in plan["bundles"] if "src/auth/session.py" in item["paths"])
            bundle_text = (run_dir / "bundles" / f"{bundle['bundle_id']}.md").read_text(encoding="utf-8")
            coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))

        self.assertEqual(inv["schema_version"], "inventory/v1")
        self.assertTrue(any(item["path"] == "src/auth/session.py" and "auth" in item["risk_hints"] for item in inv["files"]))
        self.assertEqual(bundle["tier"], "P0")
        self.assertIn("1 | def refresh_session():", bundle_text)
        self.assertIn("Intent test eligible: true", bundle_text)
        reviewed = sum(coverage[key] for key in ("deep_reviewed_files", "standard_reviewed_files", "light_reviewed_files", "inventory_only_files"))
        self.assertEqual(reviewed + coverage["skipped_files"], coverage["source_like_files_total"])

    def test_bundle_plan_uses_semantic_risk_routing_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "inventory.json").write_text(
                json.dumps(
                    {
                        "schema_version": "inventory/v1",
                        "files": [
                            {
                                "path": "scripts/migrate.py",
                                "is_source_like": True,
                                "is_binary": False,
                                "is_generated_candidate": False,
                                "risk_hints": [],
                                "estimated_tokens": 10,
                            },
                            {
                                "path": "src/low.py",
                                "is_source_like": True,
                                "is_binary": False,
                                "is_generated_candidate": False,
                                "risk_hints": [],
                                "estimated_tokens": 10,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "risk-routing.json").write_text(
                json.dumps(
                    {
                        "schema_version": "risk-routing/v1",
                        "default_depth": "P2",
                        "routes": [
                            {"path": "scripts/migrate.py", "tier": "P0", "reasons": ["semantic high risk"]},
                            {"path": "src/low.py", "tier": "P3", "reasons": ["semantic inventory only"]},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            plan = bundle_plan_payload(run_dir)
            coverage = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))

        p0_bundle = next(item for item in plan["bundles"] if item["tier"] == "P0")
        bundled_paths = [path for bundle in plan["bundles"] for path in bundle["paths"]]
        self.assertIn("scripts/migrate.py", p0_bundle["paths"])
        self.assertNotIn("src/low.py", bundled_paths)
        self.assertEqual(coverage["deep_reviewed_files"], 1)
        self.assertEqual(coverage["inventory_only_files"], 1)

    def test_intent_tests_run_in_disposable_validation_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('source')\n", encoding="utf-8")
            (repo / "test_intent_marker.py").write_text(
                "from pathlib import Path\nimport unittest\n\nclass IntentMarkerTest(unittest.TestCase):\n    def test_marker(self):\n        Path('intent_marker.txt').write_text('validation')\n\n",
                encoding="utf-8",
            )
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            prepare_validation_workspace(repo, run_dir)
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": True, "max_tests_per_run": 2, "max_test_run_seconds_per_test": 5, "max_total_test_run_seconds": 10}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps({"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITV-001"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [
                            {
                                "test_id": "ITV-001",
                                "command": [sys.executable, "-m", "unittest", "test_intent_marker"],
                                "artifact_refs": ["art_intent_test_source"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "win32"):
                result = run_intent_tests(run_dir)
            validation_repo = repo.parent / "validation-repo"
            test_run = result["test_runs"][0]
            marker_in_validation_repo = (validation_repo / "intent_marker.txt").is_file()
            marker_in_source_repo = (repo / "intent_marker.txt").exists()
            stdout_exists = Path(test_run["stdout_path"]).is_file()
            stderr_exists = Path(test_run["stderr_path"]).is_file()

        self.assertEqual(result["schema_version"], "intent-test-run-results/v1")
        self.assertEqual(test_run["status"], "passed")
        self.assertEqual(test_run["exit_code"], 0)
        self.assertTrue(marker_in_validation_repo)
        self.assertFalse(marker_in_source_repo)
        self.assertTrue(stdout_exists)
        self.assertTrue(stderr_exists)

    def test_intent_tests_skip_cwd_that_escapes_validation_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir()
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            prepare_validation_workspace(repo, run_dir)
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": True}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [
                            {
                                "test_id": "ITV-escape",
                                "cwd": "..",
                                "command": [sys.executable, "-c", "raise SystemExit(0)"],
                                "artifact_refs": ["art_intent_test_source"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_intent_tests(run_dir)

        self.assertEqual(result["test_runs"][0]["status"], "skipped")
        self.assertIn("escapes validation workspace", result["test_runs"][0]["skip_reason"])

    def test_intent_tests_skip_disallowed_generated_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            validation_repo = root / "validation-repo"
            validation_repo.mkdir(parents=True)
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "validation-workspace.json").write_text(
                json.dumps({"validation_repo_root": str(validation_repo)}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": True}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-plan/v1",
                        "test_targets": [{"test_id": "ITV-install", "cwd": "."}],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [
                            {
                                "test_id": "ITV-install",
                                "command": ["pip", "install", "unexpected-package"],
                                "artifact_refs": ["art_intent_test_source"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "pullwise_worker.review_worker_v1.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run:
                result = run_intent_tests(run_dir)

        run.assert_not_called()
        self.assertEqual(result["test_runs"][0]["status"], "skipped")
        self.assertRegex(result["test_runs"][0]["skip_reason"], "disallowed|not allowed|policy")

    def test_intent_tests_run_with_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            validation_repo = root / "validation-repo"
            validation_repo.mkdir(parents=True)
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "validation-workspace.json").write_text(
                json.dumps({"validation_repo_root": str(validation_repo)}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": True}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps({"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITV-env"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [
                            {
                                "test_id": "ITV-env",
                                "command": [sys.executable, "-m", "unittest", "test_env_marker"],
                                "artifact_refs": ["art_intent_test_source"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "PULLWISE_WORKER_TOKEN": "secret-token",
                    "OPENAI_API_KEY": "sk-secret",
                    "HTTPS_PROXY": "http://proxy.invalid",
                },
                clear=False,
            ), patch("pullwise_worker.review_worker_v1.sys.platform", "win32"), patch(
                "pullwise_worker.review_worker_v1.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run:
                result = run_intent_tests(run_dir)

            env = run.call_args.kwargs["env"]

        self.assertEqual(result["test_runs"][0]["status"], "passed")
        self.assertNotIn("PULLWISE_WORKER_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["HOME"], str(validation_repo / ".intent-test-home"))
        self.assertEqual(env["PULLWISE_INTENT_TEST_NETWORK_DISABLED"], "1")
    def test_intent_tests_skip_when_linux_sandbox_runner_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            validation_repo = root / "validation-repo"
            validation_repo.mkdir(parents=True)
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "validation-workspace.json").write_text(
                json.dumps({"validation_repo_root": str(validation_repo)}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": True}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps({"schema_version": "intent-test-plan/v1", "test_targets": [{"test_id": "ITV-sandbox"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [
                            {
                                "test_id": "ITV-sandbox",
                                "command": [sys.executable, "-m", "unittest", "test_env_marker"],
                                "artifact_refs": ["art_intent_test_source"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.shutil.which",
                return_value=None,
            ), patch("pullwise_worker.review_worker_v1.subprocess.run") as run:
                result = run_intent_tests(run_dir)

        run.assert_not_called()
        self.assertEqual(result["test_runs"][0]["status"], "skipped")
        self.assertIn("sandbox runner is unavailable", result["test_runs"][0]["skip_reason"])
    def test_intent_failure_analysis_fallback_classifies_raw_runs_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-results.raw.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-run-results/v1",
                        "test_runs": [
                            {
                                "test_id": "ITV-pass",
                                "status": "passed",
                                "exit_code": 0,
                                "duration_ms": 12,
                                "stdout_path": str(run_dir / "intent" / "test-output" / "ITV-pass.stdout.log"),
                            },
                            {"test_id": "ITV-fail", "status": "failed", "exit_code": 1, "duration_ms": 13},
                            {"test_id": "ITV-timeout", "status": "timeout", "timed_out": True, "duration_ms": 5000},
                            {"test_id": "ITV-skip", "status": "skipped", "skip_reason": "no command"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_test_failure_analysis")
            payload = json.loads((run_dir / "intent" / "intent-test-results.json").read_text(encoding="utf-8"))

        classes = {result["test_id"]: result["classification"] for result in payload["test_results"]}
        self.assertEqual(classes["ITV-pass"], "unclear_requirement")
        self.assertEqual(classes["ITV-fail"], "unclear_requirement")
        self.assertEqual(classes["ITV-timeout"], "test_harness_error")
        self.assertEqual(classes["ITV-skip"], "test_harness_error")
        self.assertTrue(all(result["finding_confidence_impact"] == "none" for result in payload["test_results"]))
        self.assertFalse({"confirmed_bug", "plausible_bug"} & set(classes.values()))
        fallback_by_id = {result["test_id"]: result for result in payload["test_results"]}
        self.assertEqual(fallback_by_id["ITV-pass"]["status"], "passed")
        self.assertEqual(fallback_by_id["ITV-pass"]["confidence"], 0.0)
        self.assertEqual(fallback_by_id["ITV-pass"]["artifact_refs"], ["art_intent_test_output_ITV_pass_stdout_log"])

    def test_qa_gate_rejects_invalid_main_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                '{"schema_version":"coverage/v1","source_like_files_total":1,"deep_reviewed_files":1,"standard_reviewed_files":0,"light_reviewed_files":0,"inventory_only_files":0,"skipped_files":0}',
                encoding="utf-8",
            )
            (run_dir / "token-budget.json").write_text('{"schema_version":"token-budget/v1"}', encoding="utf-8")
            (run_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (run_dir / "report.agent.json").write_text(
                '{"schema_id":"codex-full-repo-review","schema_version":"v1","findings":[{"title":"Bad","severity":"high","confidence":1.2,"locations":[]}]}',
                encoding="utf-8",
            )

            qa = qa_gate_payload(repo, run_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("locations" in error for error in qa["errors"]))
        self.assertTrue(any("confidence" in error for error in qa["errors"]))

    def test_qa_gate_validates_source_intent_refs_and_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            app_file = repo / "app.py"
            app_file.write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            artifact_dir = Path(tmp_dir) / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent").mkdir(parents=True, exist_ok=True)
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps({"schema_version": "intent-test-source/v1", "generated_tests": [{"id": "itv_1"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-results.json").write_text(
                json.dumps({"schema_version": "intent-test-result/v1", "test_results": [{"test_id": "itv_1", "classification": "not-a-classification"}]}),
                encoding="utf-8",
            )
            materialize_artifacts(run_dir, artifact_dir)
            (artifact_dir / "report.md").write_text("tampered\n", encoding="utf-8")
            app_file.write_text("print('changed')\n", encoding="utf-8")

            qa = qa_gate_payload(repo, run_dir, artifact_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("source file modified" in error for error in qa["errors"]))
        self.assertTrue(any("generated_tests[0] missing artifact_refs" in error for error in qa["errors"]))
        self.assertTrue(any("classification is invalid" in error for error in qa["errors"]))
        self.assertTrue(any("report.md sha256 mismatch" in error for error in qa["errors"]))

    def test_qa_gate_rejects_duplicate_artifact_manifest_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            artifact_dir = Path(tmp_dir) / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-validation/v1",
                        "enabled": True,
                        "require_intent_evidence": True,
                        "skip_reason": "no P0/P1 intent targets selected",
                    }
                ),
                encoding="utf-8",
            )
            materialize_artifacts(run_dir, artifact_dir)
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            duplicate_id = manifest_payload["items"][0]["artifact_id"]
            manifest_payload["items"][1]["artifact_id"] = duplicate_id
            manifest_payload["items"][1]["storage"]["url"] = f"/v1/review-runs/run_1/artifacts/{duplicate_id}"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

            qa = qa_gate_payload(repo, run_dir, artifact_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("artifact_id is duplicated" in error for error in qa["errors"]))

    def test_qa_gate_rejects_artifact_storage_url_for_wrong_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            artifact_dir = Path(tmp_dir) / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-validation/v1",
                        "enabled": True,
                        "require_intent_evidence": True,
                        "skip_reason": "no P0/P1 intent targets selected",
                    }
                ),
                encoding="utf-8",
            )
            materialize_artifacts(run_dir, artifact_dir)
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact_id = manifest_payload["items"][0]["artifact_id"]
            manifest_payload["items"][0]["storage"]["url"] = f"/v1/review-runs/run_2/artifacts/{artifact_id}"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

            qa = qa_gate_payload(repo, run_dir, artifact_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("storage must reference server_artifact" in error for error in qa["errors"]))

    def test_qa_gate_rejects_artifact_manifest_run_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            artifact_dir = Path(tmp_dir) / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-validation/v1",
                        "enabled": True,
                        "require_intent_evidence": True,
                        "skip_reason": "no P0/P1 intent targets selected",
                    }
                ),
                encoding="utf-8",
            )
            materialize_artifacts(run_dir, artifact_dir)
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_payload["run_id"] = "run_2"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

            qa = qa_gate_payload(repo, run_dir, artifact_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("run_id must match artifact directory" in error for error in qa["errors"]))

    def test_qa_gate_rejects_intent_result_entries_missing_required_schema_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-results.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-result/v1",
                        "test_results": [{"test_id": "ITV-001", "classification": "unclear_requirement"}],
                    }
                ),
                encoding="utf-8",
            )

            qa = qa_gate_payload(repo, run_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("test_results[0].status is invalid" in error for error in qa["errors"]))
        self.assertTrue(any("test_results[0].confidence is outside 0..1" in error for error in qa["errors"]))

    def test_qa_gate_requires_intent_results_when_validation_enabled_without_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            validation_path = run_dir / "intent" / "intent-test-validation.json"
            validation_path.parent.mkdir(parents=True)
            validation_path.write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": True, "require_intent_evidence": True}),
                encoding="utf-8",
            )

            missing = qa_gate_payload(repo, run_dir)
            validation_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-validation/v1",
                        "enabled": True,
                        "require_intent_evidence": True,
                        "skip_reason": "no P0/P1 targets selected",
                    }
                ),
                encoding="utf-8",
            )
            skipped = qa_gate_payload(repo, run_dir)
            validation_path.write_text(
                json.dumps({"schema_version": "intent-test-validation/v1", "enabled": False, "require_intent_evidence": True}),
                encoding="utf-8",
            )
            disabled = qa_gate_payload(repo, run_dir)

        self.assertEqual(missing["status"], "fail")
        self.assertTrue(any("intent-test-results.json is missing" in error for error in missing["errors"]))
        self.assertEqual(skipped["status"], "pass")
        self.assertEqual(disabled["status"], "pass")

    def test_qa_gate_requires_validator_status_for_bug_supporting_intent_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            finding = {
                "title": "Intent-only signal",
                "severity": "high",
                "confidence": 0.7,
                "locations": [{"path": "app.py", "start_line": 1, "end_line": 1}],
                "evidence": ["generated test failed"],
                "impact": "bad state",
                "recommendation": "validate before reporting",
                "validation_sources": {"intent_test": {"test_id": "ITV-001", "classification": "plausible_bug"}},
            }
            report = {"schema_id": "codex-full-repo-review", "schema_version": "v1", "findings": [finding]}
            (run_dir / "report.agent.json").write_text(json.dumps(report), encoding="utf-8")
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-results.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-result/v1",
                        "test_results": [
                            {
                                "test_id": "ITV-001",
                                "status": "failed",
                                "classification": "plausible_bug",
                                "confidence": 0.4,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            missing_validator = qa_gate_payload(repo, run_dir)
            finding["validation_sources"]["validator_status"] = "confirmed"
            (run_dir / "report.agent.json").write_text(json.dumps(report), encoding="utf-8")
            confirmed_validator = qa_gate_payload(repo, run_dir)

        self.assertEqual(missing_validator["status"], "fail")
        self.assertTrue(any("without validator_status" in error for error in missing_validator["errors"]))
        self.assertEqual(confirmed_validator["status"], "pass")

    def test_qa_gate_phase_materializes_final_qa_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "inventory.json").write_text(json.dumps(inventory(repo)), encoding="utf-8")
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-validation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-validation/v1",
                        "enabled": True,
                        "require_intent_evidence": True,
                        "skip_reason": "no P0/P1 intent targets selected",
                    }
                ),
                encoding="utf-8",
            )
            worker = ReviewWorkerV1(
                SimpleNamespace(worker_id="wk_1", service_home=str(root / "service")),
                client=object(),
            )

            worker.run_mechanical_phase(
                repo,
                run_dir,
                {"job_id": "job_1", "run_id": "run_1", "commit": "abc123", "attempt": 1},
                "qa_gate",
            )
            artifact_dir = root / "service" / "workers" / "wk_1" / "artifacts" / "run_1"
            qa = json.loads((run_dir / "qa.json").read_text(encoding="utf-8"))
            manifest_payload = json.loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            manifest = artifact_manifest_items(manifest_payload)
            qa_item = next(item for item in manifest if item["kind"] == "qa")
            run_qa_bytes = (run_dir / "qa.json").read_bytes()
            artifact_qa_bytes = (artifact_dir / "qa.json").read_bytes()
            validate_phase_outputs(run_dir, "qa_gate")
            validate_phase_outputs(run_dir, "hash_artifacts", artifact_dir)

        self.assertEqual(qa["status"], "pass")
        self.assertEqual(run_qa_bytes, artifact_qa_bytes)
        self.assertEqual(qa_item["sha256"], hashlib.sha256(artifact_qa_bytes).hexdigest())
        self.assertEqual(qa_item["size_bytes"], len(artifact_qa_bytes))

    def test_heartbeat_includes_progress_snapshot_when_busy(self) -> None:
        calls = []

        class Client:
            def heartbeat(self, **payload: dict) -> dict:
                calls.append(payload)
                return {}

        worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home="/tmp"), client=Client())
        worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
        active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")
        active.overall_percent = 42.0
        active.current_phase = "intent_test_running"
        active.current_phase_status = "running"
        active.thread_id = "thr_123"
        active.apply_progress_data({"reviewer_runs_total": 3, "reviewer_runs_completed": 2})
        worker.state.set_active(active)

        worker.heartbeat()

        self.assertEqual(calls[0]["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(calls[0]["worker_id"], "wk_1")
        self.assertEqual(calls[0]["status"], "leased")
        self.assertEqual(calls[0]["active_run_id"], "run_1")
        self.assertEqual(calls[0]["concurrency"]["active_jobs"], 1)
        self.assertEqual(calls[0]["concurrency"]["available_job_slots"], 0)
        self.assertFalse(calls[0]["concurrency"]["maintains_local_queue"])
        self.assertEqual(calls[0]["codex_app_server"]["active_thread_id"], "thr_123")
        self.assertEqual(calls[0]["progress"]["current_phase"], "intent_test_running")
        self.assertEqual(calls[0]["progress"]["counters"]["reviewer_runs_total"], 3)
        self.assertEqual(calls[0]["progress"]["counters"]["reviewer_runs_completed"], 2)
        self.assertIn("active_unit", calls[0]["progress"])
        self.assertNotIn("running_jobs", calls[0])
        self.assertNotIn("active_job_ids", calls[0])

    def test_heartbeat_includes_cached_worker_machine_metrics(self) -> None:
        calls = []

        class Client:
            def heartbeat(self, **payload: dict) -> dict:
                calls.append(payload)
                return {}

        class AppServer:
            def is_running(self) -> bool:
                return True

            def close(self) -> None:
                return None

        metrics = {
            "ok": True,
            "collectedAt": 1781200000,
            "worker": {"hostname": "worker-host"},
            "memory": {"usedPercent": 62.5},
            "storage": {"usedPercent": 40.0},
        }

        with tempfile.TemporaryDirectory() as root:
            work_dir = str(Path(root) / "checkouts")
            worker = ReviewWorkerV1(
                SimpleNamespace(
                    worker_id="wk_1",
                    service_home=root,
                    work_dir=work_dir,
                    machine_metrics_interval_seconds=60,
                ),
                client=Client(),
            )
            worker.app_server = AppServer()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.worker_machine_metrics_payload", return_value=metrics) as collect:
                worker.heartbeat()
                worker.heartbeat()

        self.assertEqual(calls[0]["machine_metrics"], metrics)
        self.assertEqual(calls[1]["machine_metrics"], metrics)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.kwargs["storage_path"], work_dir)

    def test_pullwise_client_uses_v1_review_protocol_routes(self) -> None:
        calls = []

        class Client(PullwiseClient):
            def post(self, path: str, payload: dict, *, compress: bool = False) -> PullwiseResponse:
                calls.append((path, payload, compress))
                body = {"job": {"job_id": "job_1", "run_id": "run_job_1"}} if path.endswith("/lease") else {"ok": True}
                return PullwiseResponse(json.dumps(body).encode("utf-8"))

        client = Client(
            SimpleNamespace(
                worker_id="wk_1",
                worker_token="secret",
                server_url="https://api.pullwise.dev",
                provider="codex",
                provider_chain=["codex"],
                result_upload_compress_min_bytes=1024,
            )
        )

        with patch("pullwise_worker._main_part_01_bootstrap.sys.platform", "linux"):
            client.register()
        client.heartbeat(
            protocol_version="review-worker-protocol/v1",
            worker_id="wk_1",
            status="busy",
            active_run_id="run_job_1",
            concurrency={
                "max_active_jobs": 1,
                "active_jobs": 1,
                "available_job_slots": 0,
                "maintains_local_queue": False,
                "local_queue_depth": 0,
            },
            codex_app_server={"status": "ready", "transport": "stdio", "active_thread_id": "thr_123"},
            progress={"run_id": "run_job_1"},
        )
        self.assertEqual(client.claim()["run_id"], "run_job_1")
        client.event("run_job_1", {"run_id": "run_job_1", "event_type": "phase_started"})
        client.artifact("job_1", "art_report_human", {"run_id": "run_job_1", "artifact": {"artifact_id": "art_report_human"}})
        client.result("job_1", {"reviewWorkerProtocol": {"job": {"run_id": "run_job_1"}}})

        self.assertEqual(
            [path for path, _payload, _compress in calls],
            [
                "/v1/workers/register",
                "/v1/workers/wk_1/heartbeat",
                "/v1/workers/wk_1/lease",
                "/v1/review-runs/run_job_1/events",
                "/v1/review-runs/run_job_1/artifacts",
                "/v1/review-runs/run_job_1/result",
            ],
        )
        self.assertEqual(calls[0][1]["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(calls[0][1]["worker"]["worker_id"], "wk_1")
        self.assertEqual(calls[0][1]["worker"]["concurrency"]["max_active_jobs"], 1)
        self.assertFalse(calls[0][1]["worker"]["concurrency"]["maintains_local_queue"])
        heartbeat_payload = calls[1][1]
        self.assertEqual(heartbeat_payload["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(heartbeat_payload["status"], "busy")
        self.assertEqual(heartbeat_payload["active_run_id"], "run_job_1")
        self.assertEqual(heartbeat_payload["concurrency"]["active_jobs"], 1)
        self.assertEqual(heartbeat_payload["concurrency"]["available_job_slots"], 0)
        self.assertEqual(heartbeat_payload["codex_app_server"]["status"], "ready")
        self.assertEqual(heartbeat_payload["codex_app_server"]["active_thread_id"], "thr_123")
        self.assertNotIn("running_jobs", heartbeat_payload)
        self.assertTrue(calls[-2][2])
        self.assertTrue(calls[-1][2])

    def test_pullwise_client_uses_worker_identity_headers(self) -> None:
        client = PullwiseClient(
            SimpleNamespace(
                worker_id="wk_1",
                worker_token="secret",
                server_url="https://api.pullwise.dev",
                provider="codex",
                provider_chain=["codex"],
                result_upload_compress_min_bytes=1024,
            )
        )

        self.assertEqual(client.headers["User-Agent"], PULLWISE_WORKER_USER_AGENT)
        self.assertEqual(client.headers["X-Pullwise-Worker-Id"], "wk_1")
        self.assertEqual(client.headers["X-Pullwise-Worker-Version"], __version__)
        self.assertEqual(client.headers["Accept"], "application/json")

    def test_pullwise_client_has_no_legacy_review_progress_route(self) -> None:
        bootstrap_source = (Path(__file__).resolve().parents[1] / "pullwise_worker" / "_main_part_01_bootstrap.py").read_text(
            encoding="utf-8"
        )

        self.assertFalse(hasattr(PullwiseClient, "progress"))
        self.assertNotIn("/worker/jobs/", bootstrap_source)
        self.assertNotIn("running_jobs", bootstrap_source)
        self.assertNotIn("active_job_ids", bootstrap_source)
        self.assertNotIn("client_active_job_ids", bootstrap_source)

    def test_pullwise_client_reports_cancelling_heartbeat_status(self) -> None:
        calls = []

        class Client(PullwiseClient):
            def post(self, path: str, payload: dict, *, compress: bool = False) -> PullwiseResponse:
                calls.append((path, payload, compress))
                return PullwiseResponse(b"{\"ok\": true}")

        client = Client(
            SimpleNamespace(
                worker_id="wk_1",
                worker_token="secret",
                server_url="https://api.pullwise.dev",
                provider="codex",
                provider_chain=["codex"],
                result_upload_compress_min_bytes=1024,
            )
        )

        client.heartbeat(
            protocol_version="review-worker-protocol/v1",
            worker_id="wk_1",
            status="cancelling",
            active_run_id="run_1",
            concurrency={
                "max_active_jobs": 1,
                "active_jobs": 1,
                "available_job_slots": 0,
                "maintains_local_queue": False,
                "local_queue_depth": 0,
            },
            codex_app_server={"status": "ready", "transport": "stdio", "active_thread_id": "thr_123"},
            progress={"run_id": "run_1", "current_phase_status": "running"},
        )

        payload = calls[0][1]
        self.assertEqual(calls[0][0], "/v1/workers/wk_1/heartbeat")
        self.assertEqual(payload["status"], "cancelling")
        self.assertEqual(payload["concurrency"]["active_jobs"], 1)
        self.assertEqual(payload["concurrency"]["available_job_slots"], 0)

    def test_pullwise_client_accepts_direct_v1_heartbeat_payload(self) -> None:
        calls = []

        class Client(PullwiseClient):
            def post(self, path: str, payload: dict, *, compress: bool = False) -> PullwiseResponse:
                calls.append((path, payload, compress))
                return PullwiseResponse(b"{\"ok\": true}")

        client = Client(
            SimpleNamespace(
                worker_id="wk_1",
                worker_token="secret",
                server_url="https://api.pullwise.dev",
                provider="codex",
                provider_chain=["codex"],
                result_upload_compress_min_bytes=1024,
            )
        )

        client.heartbeat(
            protocol_version="review-worker-protocol/v1",
            worker_id="wk_1",
            status="idle",
            active_run_id=None,
            concurrency={
                "max_active_jobs": 1,
                "active_jobs": 0,
                "available_job_slots": 1,
                "maintains_local_queue": False,
                "local_queue_depth": 0,
            },
            codex_app_server={"status": "needs_attention", "transport": "stdio", "active_thread_id": None},
            codex_ready=False,
            ready_providers=[],
            codex_quota={"ready": False, "reason": "quota exhausted"},
        )

        payload = calls[0][1]
        self.assertEqual(calls[0][0], "/v1/workers/wk_1/heartbeat")
        self.assertEqual(payload["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(payload["status"], "idle")
        self.assertEqual(payload["concurrency"]["available_job_slots"], 1)
        self.assertFalse(payload["codex_ready"])
        self.assertEqual(payload["codex_quota"]["reason"], "quota exhausted")
        self.assertNotIn("running_jobs", payload)

    def test_worker_honors_v1_cancel_run_command_from_heartbeat(self) -> None:
        events = []

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                return {"commands": [{"type": "cancel_run", "run_id": "run_1", "reason": "user_requested"}]}

            def event(self, run_id: str, event: dict) -> dict:
                events.append((run_id, event))
                return {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run_1"
            run_dir.mkdir(parents=True)
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home="/tmp"), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")
            active.run_dir = run_dir
            active.current_phase = "reviewer_fanout"
            active.overall_percent = 64.0
            active.current_phase_percent = 20.0
            worker.state.set_active(active)

            worker.heartbeat()
            worker.heartbeat()
            event_lines = (run_dir / "progress.log.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertTrue(active.cancel_requested)
        self.assertEqual(active.cancel_reason, "user_requested")
        self.assertEqual(active.state, "cancelling")
        self.assertEqual(active.current_phase_status, "running")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "run_1")
        self.assertEqual(events[0][1]["event_type"], "run_cancel_requested")
        self.assertEqual(events[0][1]["progress"]["status"], "running")
        self.assertEqual(events[0][1]["data"]["reason"], "user_requested")
        self.assertEqual(len(event_lines), 1)
        self.assertEqual(json.loads(event_lines[0])["event_type"], "run_cancel_requested")

    def test_cancelled_run_posts_cancel_requested_before_cancelled_result(self) -> None:
        events = []
        results = []

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                return {}

            def event(self, run_id: str, event: dict) -> dict:
                events.append((run_id, event))
                return {}

            def artifact(self, _job_id: str, _artifact_id: str, _payload: dict) -> dict:
                return {}

            def result(self, job_id: str, payload: dict) -> None:
                results.append((job_id, payload))

        class CancellingWorker(ReviewWorkerV1):
            def prepare_workspace(self, job: dict, run_id: str) -> tuple[Path, Path, Path]:
                repo_dir = root / "repo"
                artifact_dir = root / "artifacts" / run_id
                run_dir = repo_dir / ".codex-review" / "runs" / run_id
                run_dir.mkdir(parents=True)
                artifact_dir.mkdir(parents=True)
                return repo_dir, run_dir, artifact_dir

            def start_phase(self, active: ActiveJob, run_dir: Path, phase: str, progress: int) -> None:
                super().start_phase(active, run_dir, phase, progress)
                if phase == "prepare_workspace":
                    self.request_cancel(active, reason="user_requested")

        job = {
            "job_id": "job_1",
            "run_id": "run_1",
            "lease_id": "lease_1",
            "repo": "acme/api",
            "commit": "abc123",
            "model_profile": {
                "default_model": "gpt-5.5",
                "core_effort": "high",
                "non_core_effort": "medium",
            },
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = CancellingWorker(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            worker.run_job(job)

            log_events = [
                json.loads(line)["event_type"]
                for line in (root / "repo" / ".codex-review" / "runs" / "run_1" / "progress.log.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        posted_event_types = [event["event_type"] for _run_id, event in events]
        self.assertLess(posted_event_types.index("run_cancel_requested"), posted_event_types.index("run_cancelled"))
        self.assertLess(log_events.index("run_cancel_requested"), log_events.index("run_cancelled"))
        self.assertEqual(results[0][0], "job_1")
        self.assertEqual(results[0][1]["status"], "cancelled")
        self.assertEqual(results[0][1]["reviewWorkerProtocol"]["execution"]["status"], "cancelled")

    def test_qa_gate_failure_submits_partial_completed_result(self) -> None:
        events = []
        results = []

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                return {}

            def event(self, run_id: str, event: dict) -> dict:
                events.append((run_id, event))
                return {}

            def artifact(self, _job_id: str, _artifact_id: str, _payload: dict) -> dict:
                return {}

            def result(self, job_id: str, payload: dict) -> None:
                results.append((job_id, payload))

        class PartialWorker(ReviewWorkerV1):
            def prepare_workspace(self, job: dict, run_id: str) -> tuple[Path, Path, Path]:
                repo_dir = root / "repo"
                artifact_dir = root / "artifacts" / run_id
                run_dir = repo_dir / ".codex-review" / "runs" / run_id
                run_dir.mkdir(parents=True)
                artifact_dir.mkdir(parents=True)
                return repo_dir, run_dir, artifact_dir

            def run_mechanical_phase(
                self,
                _repo_dir: Path,
                run_dir: Path,
                _job: dict,
                phase: str,
                *,
                active: ActiveJob | None = None,
                progress: int = 0,
            ) -> None:
                if phase == "qa_gate":
                    (run_dir / "qa.json").write_text(
                        json.dumps({"schema_version": "qa/v1", "status": "fail", "errors": ["qa failed"], "warnings": []}),
                        encoding="utf-8",
                    )

        job = {
            "job_id": "job_1",
            "run_id": "run_1",
            "lease_id": "lease_1",
            "repo": "acme/api",
            "commit": "abc123",
            "model_profile": {
                "default_model": "gpt-5.5",
                "core_effort": "high",
                "non_core_effort": "medium",
            },
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = PartialWorker(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            with patch("pullwise_worker.review_worker_v1.PIPELINE_PHASES", (("qa_gate", 99),)):
                worker.run_job(job)

            log_events = [
                json.loads(line)["event_type"]
                for line in (root / "repo" / ".codex-review" / "runs" / "run_1" / "progress.log.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        posted_event_types = [event["event_type"] for _run_id, event in events]
        self.assertLess(posted_event_types.index("phase_failed"), posted_event_types.index("qa_failed"))
        self.assertLess(posted_event_types.index("qa_failed"), posted_event_types.index("run_partial_completed"))
        self.assertLess(log_events.index("phase_failed"), log_events.index("qa_failed"))
        self.assertLess(log_events.index("qa_failed"), log_events.index("run_partial_completed"))
        partial_events = [event for _run_id, event in events if event["event_type"] == "run_partial_completed"]
        self.assertEqual(partial_events[0]["progress"]["status"], "partial_completed")
        self.assertEqual(results[0][0], "job_1")
        self.assertEqual(results[0][1]["status"], "partial_completed")
        envelope = results[0][1]["reviewWorkerProtocol"]
        self.assertEqual(envelope["execution"]["status"], "partial_completed")
        self.assertEqual(envelope["summary"]["result_status"], "incomplete")
        self.assertEqual(envelope["quality_gate"]["status"], "warn")

    def test_worker_registers_before_heartbeat_and_lease(self) -> None:
        calls = []

        class Client:
            def register(self) -> dict:
                calls.append("register")
                return {}

            def heartbeat(self, **_payload: dict) -> dict:
                calls.append("heartbeat")
                return {}

            def claim(self) -> None:
                calls.append("claim")
                return None

        with tempfile.TemporaryDirectory() as root:
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=root, poll_seconds=1), client=Client())
            worker.lock.acquire = lambda: None  # type: ignore[method-assign]
            worker.lock.release = lambda: None  # type: ignore[method-assign]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            class AppServer:
                def is_running(self) -> bool:
                    return True

                def close(self) -> None:
                    return None

            worker.app_server = AppServer()  # type: ignore[assignment]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

        self.assertEqual(calls, ["register", "heartbeat", "claim"])

    def test_heartbeat_keeps_ready_when_quota_probe_is_unavailable_but_app_server_runs(self) -> None:
        heartbeat_payloads = []

        class Client:
            def heartbeat(self, **payload: dict) -> dict:
                heartbeat_payloads.append(payload)
                return {}

        class AppServer:
            def is_running(self) -> bool:
                return True

            def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                raise RuntimeError("rate limit endpoint unavailable")

            def set_events_path(self, _events_path: Path) -> None:
                return None

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as root:
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=root), client=Client())
            worker.ensure_app_server = lambda events_path=None: AppServer()  # type: ignore[method-assign, return-value]
            worker.app_server = worker.ensure_app_server()

            worker.heartbeat()

        self.assertEqual(heartbeat_payloads[0]["codex_app_server"]["status"], "ready")
        self.assertTrue(heartbeat_payloads[0]["codex_ready"])
        self.assertEqual(heartbeat_payloads[0]["doctor_status"], "ok")
        self.assertEqual(heartbeat_payloads[0]["ready_providers"], ["codex"])
        self.assertEqual(heartbeat_payloads[0]["codex_quota"]["status"], "unavailable")
        self.assertEqual(heartbeat_payloads[0]["codex_quota"]["reason"], "codex_quota_unavailable")

    def test_worker_closes_app_server_when_control_plane_stops_accepting_heartbeat(self) -> None:
        events = []

        class Client:
            def register(self) -> dict:
                return {}

            def heartbeat(self, **_payload: dict) -> dict:
                raise RuntimeError("worker disabled")

            def claim(self) -> None:
                return None

        class AppServer:
            def is_running(self) -> bool:
                return True

            def close(self) -> None:
                events.append("closed")

        with tempfile.TemporaryDirectory() as root:
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=root, poll_seconds=1), client=Client())
            worker.lock.acquire = lambda: None  # type: ignore[method-assign]
            worker.lock.release = lambda: None  # type: ignore[method-assign]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            worker.app_server = AppServer()  # type: ignore[assignment]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                with self.assertRaisesRegex(RuntimeError, "worker disabled"):
                    worker.run(once=True)

        self.assertEqual(events, ["closed"])
    def test_worker_does_not_claim_when_codex_quota_is_not_ready(self) -> None:
        calls = []
        heartbeat_payloads = []

        class Client:
            def register(self) -> dict:
                calls.append("register")
                return {}

            def heartbeat(self, **payload: dict) -> dict:
                calls.append("heartbeat")
                heartbeat_payloads.append(payload)
                return {}

            def claim(self) -> None:
                calls.append("claim")
                return None

        with tempfile.TemporaryDirectory() as root:
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=root, poll_seconds=1), client=Client())
            worker.lock.acquire = lambda: None  # type: ignore[method-assign]
            worker.lock.release = lambda: None  # type: ignore[method-assign]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {  # type: ignore[method-assign]
                "ready": False,
                "reason": "codex usage limit exhausted",
            }

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

        self.assertEqual(calls, ["register", "heartbeat"])
        self.assertEqual(heartbeat_payloads[0]["status"], "idle")
        self.assertFalse(heartbeat_payloads[0]["codex_ready"])
        self.assertEqual(heartbeat_payloads[0]["codex_app_server"]["status"], "needs_attention")
        self.assertEqual(heartbeat_payloads[0]["concurrency"]["active_jobs"], 0)
        self.assertEqual(heartbeat_payloads[0]["concurrency"]["available_job_slots"], 1)

    def test_worker_run_rejects_non_linux_platform_before_registration(self) -> None:
        calls = []

        class Client:
            def register(self) -> dict:
                calls.append("register")
                return {}

        with tempfile.TemporaryDirectory() as root:
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=root, poll_seconds=1), client=Client())
            with patch("pullwise_worker.review_worker_v1.sys.platform", "darwin"):
                with self.assertRaisesRegex(RuntimeError, "Linux only"):
                    worker.run(once=True)

        self.assertEqual(calls, [])

    def test_worker_registration_payload_is_v1_one_slot_linux_metadata(self) -> None:
        with patch("pullwise_worker._main_part_01_bootstrap.sys.platform", "linux"), patch(
            "pullwise_worker._main_part_01_bootstrap.shutil.which",
            side_effect=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        ):
            payload = worker_registration_payload(SimpleNamespace(worker_id="wk_1", service_home="/var/lib/pullwise-worker"))

        self.assertEqual(payload["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(payload["worker"]["worker_id"], "wk_1")
        self.assertEqual(payload["worker"]["concurrency"]["max_active_jobs"], 1)
        self.assertFalse(payload["worker"]["concurrency"]["maintains_local_queue"])
        self.assertFalse(payload["worker"]["concurrency"]["prefetch_jobs"])
        self.assertTrue(payload["worker"]["capabilities"]["codex_app_server"])
        self.assertTrue(payload["worker"]["capabilities"]["progress_events"])
        self.assertTrue(payload["worker"]["capabilities"]["intent_test_validation"])
        self.assertEqual(payload["worker"]["platform"]["os"], "linux")
        self.assertEqual(payload["worker"]["capabilities"]["codex_app_server_transport"], ["stdio", "unix"])
        with patch("pullwise_worker._main_part_01_bootstrap.sys.platform", "linux"), patch("pullwise_worker._main_part_01_bootstrap.shutil.which", return_value=None):
            unavailable = worker_registration_payload(SimpleNamespace(worker_id="wk_1", service_home="/var/lib/pullwise-worker"))
        self.assertFalse(unavailable["worker"]["capabilities"]["intent_test_validation"])

        with patch("pullwise_worker._main_part_01_bootstrap.sys.platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "requires Linux"):
                worker_registration_payload(SimpleNamespace(worker_id="wk_1", service_home="/var/lib/pullwise-worker"))

    def test_codex_error_mapper_returns_stable_protocol_codes(self) -> None:
        self.assertEqual(codex_error_code({"codexErrorInfo": "UsageLimitExceeded"}), "CODEX_QUOTA_EXHAUSTED")
        self.assertEqual(codex_error_code('{"codexErrorInfo":"ContextWindowExceeded"}'), "CODEX_CONTEXT_WINDOW_EXCEEDED")
        self.assertEqual(codex_error_code("unexpected"), "CODEX_UNKNOWN_ERROR")

    def test_quota_probe_auth_error_is_not_exhaustion(self) -> None:
        self.assertFalse(
            quota_refresh_error_is_exhaustion("codex account authentication required to read rate limits")
        )
        self.assertTrue(quota_refresh_error_is_exhaustion("rate_limit_reached"))


    def test_codex_quota_payload_selects_main_codex_bucket(self) -> None:
        payload = codex_quota_payload_from_rate_limits(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 8, "windowDurationMins": 300, "resetsAt": 1782918371},
                    "secondary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": 1783419385},
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    "planType": "pro",
                    "rateLimitReachedType": None,
                },
                "rateLimitsByLimitId": {
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "limitName": "GPT-5.3-Codex-Spark",
                        "primary": {"usedPercent": 80, "windowDurationMins": 300, "resetsAt": 1782918371},
                    },
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 8, "windowDurationMins": 300, "resetsAt": 1782918371},
                        "secondary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": 1783419385},
                        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                        "planType": "pro",
                        "rateLimitReachedType": None,
                    },
                },
                "rateLimitResetCredits": {"availableCount": 1},
            },
            threshold_percent=5,
            checked_at=1782900000,
            next_check_at=1782900300,
        )

        self.assertEqual(payload["limitId"], "codex")
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["remainingPercent"], 78)
        self.assertEqual(payload["planType"], "pro")
        self.assertEqual(payload["rateLimitResetCredits"]["availableCount"], 1)
        self.assertEqual(payload["credits"]["hasCredits"], False)
        self.assertEqual([window["windowKind"] for window in payload["windows"]], ["five_hour", "weekly"])
        self.assertEqual(payload["windows"][0]["remainingPercent"], 92)
        self.assertEqual(payload["windows"][1]["remainingPercent"], 78)
    def test_codex_quota_monitor_merges_rate_limit_updates(self) -> None:
        monitor = CodexQuotaMonitor(
            SimpleNamespace(
                codex_quota_check_seconds=60,
                codex_quota_degraded_check_seconds=30,
                codex_quota_min_remaining_percent=20,
            ),
            SimpleNamespace(),
        )

        monitor.apply_rate_limit_update(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 90, "windowDurationMins": 300, "resetsAt": 123},
                    "credits": {"hasCredits": False, "unlimited": False},
                },
                "rateLimitResetCredits": {"availableCount": 1},
            }
        )

        self.assertEqual(monitor.snapshot["status"], "low")
        self.assertFalse(monitor.snapshot["ready"])
        self.assertEqual(monitor.snapshot["rateLimitResetCredits"]["availableCount"], 1)
        self.assertEqual(monitor.snapshot["blockedWindows"][0]["windowKind"], "five_hour")

        monitor.apply_rate_limit_update({"rateLimits": {"primary": {"usedPercent": 100}}})

        self.assertEqual(monitor.snapshot["status"], "exhausted")
        self.assertEqual(monitor.snapshot["reason"], "codex_quota_exhausted")

    def test_codex_quota_refresh_reuses_worker_app_server_without_closing_it(self) -> None:
        calls = []

        class AppServer:
            def request(self, method: str, params: dict | None = None, timeout_seconds: int = 30) -> dict:
                calls.append((method, params or {}, timeout_seconds))
                return {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 123},
                        "rateLimitReachedType": None,
                    }
                }

            def close(self) -> None:
                calls.append(("close", {}, 0))

        server = AppServer()
        monitor = CodexQuotaMonitor(
            SimpleNamespace(codex_quota_check_seconds=60, codex_quota_min_remaining_percent=20),
            SimpleNamespace(),
            lambda: server,  # type: ignore[arg-type]
        )

        snapshot = monitor.refresh(current_time=100)

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(calls, [("account/rateLimits/read", {}, 15)])

    def test_core_semantic_phases_use_plan_effort_and_other_phases_use_medium(self) -> None:
        job = {
            "model_profile": {
                "default_model": "gpt-5.5",
                "core_effort": "high",
                "reviewer_effort": "high",
                "validator_effort": "high",
                "reporter_effort": "high",
                "intent_test_effort": "high",
                "non_core_effort": "medium",
            },
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        self.assertEqual(effort_for_phase(job, "reviewer_fanout"), "high")
        self.assertEqual(effort_for_phase(job, "repo_map"), "high")
        self.assertEqual(effort_for_phase(job, "final_report_json"), "high")
        self.assertEqual(effort_for_phase(job, "bootstrap_helper_scripts"), "medium")
        self.assertEqual(effort_for_phase(job, "inventory_repository"), "medium")

    def test_artifact_manifest_contains_required_completed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "intent" / "test-output").mkdir(parents=True)
            (run_dir / "intent" / "test-output" / "ITV-001.stdout.log").write_text("ok\n", encoding="utf-8")
            (run_dir / "intent" / "test-output" / "ITV-001.stderr.log").write_text("", encoding="utf-8")
            (run_dir / "raw-reviewers").mkdir(parents=True)
            (run_dir / "verified-reviewers").mkdir(parents=True)
            reviewer_payload = {"schema_version": "codex-reviewer-output/v1", "findings": []}
            (run_dir / "raw-reviewers" / "security.json").write_text(json.dumps(reviewer_payload), encoding="utf-8")
            (run_dir / "verified-reviewers" / "security.json").write_text(json.dumps(reviewer_payload), encoding="utf-8")
            materialize_artifacts(run_dir, artifact_dir)

            manifest_payload = __import__("json").loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            run_manifest = __import__("json").loads((run_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            manifest = artifact_manifest_items(manifest_payload)
            kinds = {item["kind"] for item in manifest if item.get("required")}
            output_items = [item for item in manifest if item["kind"] == "intent_test_output"]
            reviewer_items = [item for item in manifest if item["kind"] in {"raw_reviewer_output", "verified_reviewer_output"}]
            debug_item = next(item for item in manifest if item["kind"] == "debug_bundle")
            self.assertEqual(manifest_payload["schema_version"], "artifact-manifest/v1")
            self.assertEqual(manifest_payload["items"], manifest)
            self.assertTrue(REQUIRED_COMPLETED_ARTIFACTS.issubset(kinds))
            self.assertEqual(
                {item["name"] for item in output_items},
                {"intent-test-output-ITV-001.stdout.log", "intent-test-output-ITV-001.stderr.log"},
            )
            self.assertEqual(len({item["artifact_id"] for item in output_items}), 2)
            self.assertEqual({item["kind"] for item in reviewer_items}, {"raw_reviewer_output", "verified_reviewer_output"})
            self.assertEqual({item["name"] for item in reviewer_items}, {"raw-reviewer-security.json", "verified-reviewer-security.json"})
            self.assertEqual(len({item["artifact_id"] for item in reviewer_items}), 2)
            self.assertEqual(debug_item["artifact_id"], "art_debug_bundle")
            self.assertEqual(debug_item["name"], "debug-bundle.zip")
            with zipfile.ZipFile(artifact_dir / "debug-bundle.zip", "r") as archive:
                names = set(archive.namelist())
                self.assertIn("debug-summary.json", names)
                self.assertIn("run/worker.log.jsonl", names)
                self.assertIn("artifacts/report.md", names)
                self.assertNotIn("audit.json", names)
                self.assertFalse(any(name.endswith("audit-bundle.zip") for name in names))
            self.assertEqual(manifest_payload, run_manifest)
            for item in manifest:
                self.assertIn("sha256", item)
                self.assertIn("size_bytes", item)
                self.assertEqual(item["schema_version"], "v1")
                self.assertEqual(item["encoding"], "utf-8")
                self.assertEqual(item["compression"], "none")

    def test_terminal_artifacts_do_not_require_completed_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
            materialize_terminal_artifacts(run_dir, artifact_dir, "failed", error="boom")

            manifest_payload = __import__("json").loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            manifest = artifact_manifest_items(manifest_payload)
            self.assertTrue(manifest)
            self.assertEqual(manifest_payload["schema_version"], "artifact-manifest/v1")
            required_kinds = {item["kind"] for item in manifest if item.get("required")}
            self.assertTrue({"worker_log", "qa", "error_report"}.issubset(required_kinds))
            self.assertIn("qa.json", {item["name"] for item in manifest})
            self.assertIn("error-report.json", {item["name"] for item in manifest})
            qa = __import__("json").loads((run_dir / "qa.json").read_text(encoding="utf-8"))
            self.assertEqual(qa["status"], "fail")

            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        uploaded_ids = [artifact_id for _job_id, artifact_id, _payload in calls]
        self.assertEqual(uploaded_ids[0], "art_debug_bundle")
        self.assertIn("art_error_report", set(uploaded_ids))

    def test_upload_log_artifacts_refreshes_and_reuploads_final_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            (run_dir / "progress.log.jsonl").write_text(
                json.dumps({"event_type": "phase_completed", "phase": "upload_artifacts"}) + "\n",
                encoding="utf-8",
            )
            (run_dir / "worker.log.jsonl").write_text("", encoding="utf-8")
            (run_dir / "codex-events.jsonl").write_text("", encoding="utf-8")
            materialize_artifacts(run_dir, artifact_dir)
            with (run_dir / "progress.log.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_type": "run_completed", "phase": "cleanup_active_job"}) + "\n")
            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            upload_log_artifacts(Client(), "job_1", "wk_1-1", run_dir, artifact_dir)

            progress_upload = next(payload for _job_id, _artifact_id, payload in calls if payload["artifact"]["name"] == "progress.log.jsonl")
            uploaded_progress = base64.b64decode(progress_upload["content_base64"]).decode("utf-8")

        self.assertEqual(len(calls), 3)
        self.assertIn("run_completed", uploaded_progress)
        self.assertTrue(progress_upload["final_log_upload"])
        self.assertEqual(progress_upload["artifact"]["size_bytes"], len(uploaded_progress.encode("utf-8")))
    def test_completed_run_uploads_final_logs_after_result_submit(self) -> None:
        calls = []

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                calls.append(("heartbeat", None, None))
                return {}

            def event(self, _run_id: str, event: dict) -> dict:
                calls.append(("event", event["event_type"], event))
                return {}

            def artifact(self, _job_id: str, artifact_id: str, payload: dict) -> dict:
                calls.append(("artifact", artifact_id, payload))
                return {"accepted": True}

            def result(self, _job_id: str, payload: dict) -> None:
                calls.append(("result", payload["status"], payload))

        class CompletedWorker(ReviewWorkerV1):
            def prepare_workspace(self, _job: dict, run_id: str) -> tuple[Path, Path, Path]:
                repo_dir = root / "repo"
                run_dir = repo_dir / ".codex-review" / "runs" / run_id
                artifact_dir = root / "artifacts" / run_id
                write_completed_artifact_inputs(run_dir)
                materialize_artifacts(run_dir, artifact_dir)
                return repo_dir, run_dir, artifact_dir

        job = {
            "job_id": "job_1",
            "run_id": "run_1",
            "lease_id": "lease_1",
            "repo": "acme/api",
            "commit": "abc123",
            "model_profile": {"default_model": "gpt-5.5", "core_effort": "high", "non_core_effort": "medium"},
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = CompletedWorker(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            with patch("pullwise_worker.review_worker_v1.PIPELINE_PHASES", (("submit_result_envelope", 100),)):
                worker.run_job(job)

        result_index = next(index for index, call in enumerate(calls) if call[0] == "result")
        log_upload_indexes = [index for index, call in enumerate(calls) if call[0] == "artifact" and call[2]["artifact"]["kind"] in {"worker_log", "progress_log", "codex_event_log"}]
        self.assertTrue(log_upload_indexes)
        self.assertLess(result_index, min(log_upload_indexes))
        progress_payload = next(call[2] for call in calls if call[0] == "artifact" and call[2]["artifact"]["kind"] == "progress_log")
        uploaded_progress = base64.b64decode(progress_payload["content_base64"]).decode("utf-8")
        self.assertIn("phase_completed", uploaded_progress)
        self.assertIn("run_completed", uploaded_progress)
    def test_completed_run_does_not_emit_completed_or_final_logs_when_result_submit_pending(self) -> None:
        calls = []

        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                calls.append(("heartbeat", None, None))
                return {}

            def event(self, _run_id: str, event: dict) -> dict:
                calls.append(("event", event["event_type"], event))
                return {}

            def artifact(self, _job_id: str, artifact_id: str, payload: dict) -> dict:
                calls.append(("artifact", artifact_id, payload))
                return {"accepted": True}

            def result(self, _job_id: str, payload: dict) -> None:
                calls.append(("result", payload["status"], payload))
                raise RuntimeError("network down")

        class PendingWorker(ReviewWorkerV1):
            def prepare_workspace(self, _job: dict, run_id: str) -> tuple[Path, Path, Path]:
                repo_dir = root / "repo"
                run_dir = repo_dir / ".codex-review" / "runs" / run_id
                artifact_dir = root / "artifacts" / run_id
                write_completed_artifact_inputs(run_dir)
                materialize_artifacts(run_dir, artifact_dir)
                return repo_dir, run_dir, artifact_dir

        job = {
            "job_id": "job_1",
            "run_id": "run_1",
            "lease_id": "lease_1",
            "repo": "acme/api",
            "commit": "abc123",
            "model_profile": {"default_model": "gpt-5.5", "core_effort": "high", "non_core_effort": "medium"},
            "review_request": {
                "budget": {"max_wall_time_seconds": 14400},
                "policy": {
                    "allow_source_modification": False,
                    "allow_dependency_install": False,
                    "allow_network": False,
                    "helper_scripts_standard_library_only": True,
                    "turn_timeout_seconds": 1800,
                },
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = PendingWorker(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            with patch("pullwise_worker.review_worker_v1.PIPELINE_PHASES", (("submit_result_envelope", 100),)):
                worker.run_job(job)
            progress_lines = (root / "repo" / ".codex-review" / "runs" / "run_1" / "progress.log.jsonl").read_text(encoding="utf-8")

        self.assertIn(('result', 'done'), [(kind, value) for kind, value, _payload in calls])
        self.assertNotIn("run_completed", progress_lines)
        self.assertNotIn("run_completed", [value for kind, value, _payload in calls if kind == "event"])
        self.assertFalse([call for call in calls if call[0] == "artifact"])
    def test_upload_artifacts_posts_manifest_entries_before_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            calls = []
            progress_calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            upload_artifacts(
                Client(),
                "job_1",
                "wk_1-1",
                artifact_dir,
                progress_callback=lambda uploaded, total, item: progress_calls.append((uploaded, total, item["artifact_id"])),
            )

        uploaded_ids = {artifact_id for _job_id, artifact_id, _payload in calls}
        self.assertIn("art_report_human", uploaded_ids)
        self.assertIn("art_report_agent", uploaded_ids)
        self.assertEqual(progress_calls[-1][0], progress_calls[-1][1])
        self.assertEqual(progress_calls[-1][1], len(calls))
        for job_id, _artifact_id, payload in calls:
            self.assertEqual(job_id, "job_1")
            self.assertEqual(payload["protocol_version"], "review-worker-protocol/v1")
            self.assertEqual(payload["attempt_id"], "wk_1-1")
            self.assertEqual(payload["run_id"], "run_1")
            self.assertIn("content_base64", payload)

    def test_upload_artifacts_phase_posts_progress_per_uploaded_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service_home = root / "service"
            repo_dir = root / "repo"
            run_dir = repo_dir / ".codex-review" / "runs" / "run_1"
            artifact_dir = service_home / "workers" / "wk_1" / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            uploads = []
            events = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    uploads.append((job_id, artifact_id, payload))
                    return {"accepted": True}

                def event(self, run_id: str, payload: dict) -> dict:
                    events.append((run_id, payload))
                    return {"accepted": True}

            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(service_home)), client=Client())
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")

            worker.run_mechanical_phase(
                repo_dir,
                run_dir,
                {"job_id": "job_1", "run_id": "run_1", "attempt": 1},
                "upload_artifacts",
                active=active,
                progress=100,
            )

        upload_progress = [
            event for _run_id, event in events if event["event_type"] == "progress_updated" and event["phase"] == "upload_artifacts"
        ]
        self.assertEqual(len(upload_progress), len(uploads))
        self.assertGreater(len(upload_progress), 0)
        self.assertEqual(upload_progress[-1]["data"]["artifacts_total"], len(uploads))
        self.assertEqual(upload_progress[-1]["data"]["artifacts_uploaded"], len(uploads))
        self.assertEqual(active.counters["artifacts_total"], len(uploads))
        self.assertEqual(active.counters["artifacts_uploaded"], len(uploads))

    def test_upload_artifacts_rejects_duplicate_manifest_artifact_ids_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            duplicate_id = manifest_payload["items"][0]["artifact_id"]
            manifest_payload["items"][1]["artifact_id"] = duplicate_id
            manifest_payload["items"][1]["storage"]["url"] = f"/v1/review-runs/run_1/artifacts/{duplicate_id}"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            with self.assertRaisesRegex(RuntimeError, "duplicate artifact_id"):
                upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        self.assertEqual(calls, [])

    def test_upload_artifacts_rejects_missing_manifest_artifact_file_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            manifest_payload = json.loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            missing_item = next(item for item in manifest_payload["items"] if item["name"] == "worker.log.jsonl")
            self.assertFalse(missing_item["required"])
            (artifact_dir / missing_item["name"]).unlink()
            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            with self.assertRaisesRegex(RuntimeError, "artifact listed in manifest is missing"):
                upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        self.assertEqual(calls, [])

    def test_upload_artifacts_rejects_manifest_path_escape_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            outside = artifact_dir.parent / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_payload["items"][0]["name"] = "../outside.txt"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            with self.assertRaisesRegex(RuntimeError, "escapes artifact directory"):
                upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        self.assertEqual(calls, [])

    def test_upload_artifacts_rejects_wrong_run_storage_url_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact_id = manifest_payload["items"][0]["artifact_id"]
            manifest_payload["items"][0]["storage"]["url"] = f"/v1/review-runs/run_2/artifacts/{artifact_id}"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            with self.assertRaisesRegex(RuntimeError, "storage does not match upload run"):
                upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        self.assertEqual(calls, [])

    def test_upload_artifacts_rejects_manifest_run_id_mismatch_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            materialize_artifacts(run_dir, artifact_dir)
            manifest_path = artifact_dir / "artifact-manifest.json"
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_payload["run_id"] = "run_2"
            manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
            calls = []

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            with self.assertRaisesRegex(RuntimeError, "run_id does not match upload run"):
                upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        self.assertEqual(calls, [])

    def test_phase_progress_data_reports_required_v1_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            (run_dir / "raw-reviewers").mkdir(parents=True)
            (run_dir / "intent").mkdir(parents=True)
            write_completed_artifact_inputs(run_dir)
            (run_dir / "bundle-plan.json").write_text(
                json.dumps({"bundles": [{"bundle_id": "b1"}, {"bundle_id": "b2"}]}),
                encoding="utf-8",
            )
            (run_dir / "raw-reviewers" / "b1.json").write_text("{}", encoding="utf-8")
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps({"test_targets": [{"id": "itv_1"}, {"id": "itv_2"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps({"generated_tests": [{"id": "itv_1"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-results.raw.json").write_text(
                json.dumps({"test_runs": [{"id": "itv_1"}]}),
                encoding="utf-8",
            )
            materialize_artifacts(run_dir, artifact_dir)

            reviewer = phase_progress_data(run_dir, "reviewer_fanout")
            intent = phase_progress_data(run_dir, "intent_test_validation")
            upload = phase_completion_data(run_dir, "upload_artifacts", artifact_dir)

        self.assertEqual(reviewer["reviewer_runs_total"], 2)
        self.assertEqual(reviewer["reviewer_runs_completed"], 1)
        self.assertEqual(intent["intent_tests_total"], 2)
        self.assertEqual(intent["intent_tests_written"], 1)
        self.assertEqual(intent["intent_tests_run"], 1)
        self.assertGreaterEqual(upload["artifacts_total"], 5)
        self.assertEqual(upload["artifacts_uploaded"], upload["artifacts_total"])

    def test_validate_reviewer_outputs_rejects_invalid_json_before_verified_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            raw_dir = run_dir / "raw-reviewers"
            raw_dir.mkdir(parents=True)
            (raw_dir / "security.json").write_text(json.dumps({"findings": []}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "schema_version"):
                validate_reviewer_outputs(run_dir)

            validation = json.loads((run_dir / "json-errors.json").read_text(encoding="utf-8"))
            self.assertEqual(validation["schema_version"], "reviewer-json-validation/v1")
            self.assertTrue(validation["errors"])
            self.assertFalse((run_dir / "verified-reviewers" / "security.json").exists())
            with self.assertRaisesRegex(RuntimeError, "reviewer JSON validation failed"):
                validate_phase_outputs(run_dir, "reviewer_json_validation")

    def test_validate_reviewer_outputs_copies_valid_outputs_to_verified_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            raw_dir = run_dir / "raw-reviewers"
            raw_dir.mkdir(parents=True)
            payload = {"schema_version": "codex-reviewer-output/v1", "findings": []}
            (raw_dir / "security.json").write_text(json.dumps(payload), encoding="utf-8")

            validate_reviewer_outputs(run_dir)

            validation = json.loads((run_dir / "json-errors.json").read_text(encoding="utf-8"))
            verified = json.loads((run_dir / "verified-reviewers" / "security.json").read_text(encoding="utf-8"))
            self.assertEqual(validation["errors"], [])
            self.assertEqual(verified, payload)

    def test_reviewer_json_validation_repair_turn_fixes_invalid_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            raw_dir = run_dir / "raw-reviewers"
            raw_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text(json.dumps({"thread_id": "thread_1"}), encoding="utf-8")
            (raw_dir / "security.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
            calls = []

            class AppServer:
                def run_turn(self, **kwargs: object) -> None:
                    calls.append(kwargs)
                    (raw_dir / "security.json").write_text(
                        json.dumps({"schema_version": "codex-reviewer-output/v1", "findings": []}),
                        encoding="utf-8",
                    )

            job = {
                "model_profile": {
                    "default_model": "gpt-5.5",
                    "core_effort": "high",
                    "non_core_effort": "medium",
                },
                "review_request": {
                    "budget": {"max_wall_time_seconds": 14400},
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 1800,
                    },
                },
                "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
            }
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=object())

            worker.run_reviewer_json_validation_phase(AppServer(), repo, run_dir, job)

            validation = json.loads((run_dir / "json-errors.json").read_text(encoding="utf-8"))
            verified = json.loads((run_dir / "verified-reviewers" / "security.json").read_text(encoding="utf-8"))

        self.assertEqual(validation["errors"], [])
        self.assertEqual(verified["schema_version"], "codex-reviewer-output/v1")
        self.assertEqual(calls[0]["thread_id"], "thread_1")
        self.assertEqual(calls[0]["effort"], "medium")
        self.assertFalse(calls[0]["read_only"])
        self.assertIn("Reviewer JSON output repair", calls[0]["prompt"])

    def test_progress_phase_posts_v1_progress_updated_event_with_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            posted = []

            class Client:
                def event(self, run_id: str, payload: dict) -> dict:
                    posted.append((run_id, payload))
                    return {"ack": True}

            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")
            counters = {"reviewer_runs_total": 2, "reviewer_runs_completed": 1}

            worker.progress_phase(
                active,
                run_dir,
                "reviewer_fanout",
                70,
                current_phase_percent=50,
                message="Reviewer fanout progress.",
                data=counters,
            )
            progress_snapshot = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))

        self.assertEqual(posted[0][0], "run_1")
        event = posted[0][1]
        self.assertEqual(event["event_type"], "progress_updated")
        self.assertEqual(event["phase"], "reviewer_fanout")
        self.assertEqual(event["progress"]["status"], "running")
        self.assertEqual(event["data"]["reviewer_runs_total"], 2)
        self.assertEqual(event["data"]["reviewer_runs_completed"], 1)
        self.assertEqual(progress_snapshot["counters"]["reviewer_runs_total"], 2)
        self.assertEqual(progress_snapshot["counters"]["reviewer_runs_completed"], 1)

    def test_phase_prompt_uses_phase_specific_contract_and_prompt_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            review_root = root / "repo" / ".codex-review"
            run_dir = review_root / "runs" / "run_1"
            prompts = review_root / "prompts"
            run_dir.mkdir(parents=True)
            prompts.mkdir(parents=True)
            (prompts / "00_repo_mapper.md").write_text("CUSTOM REPO MAP TEMPLATE\n", encoding="utf-8")

            prompt = phase_prompt("repo_map", run_dir)

            self.assertIn("Role: Repo Mapper", prompt)
            self.assertIn("Required outputs:", prompt)
            self.assertIn(f"- Paths are relative to the run artifact directory: {run_dir}", prompt)
            self.assertIn("- repo-map.json", prompt)
            self.assertIn("--- 00_repo_mapper.md ---", prompt)
            self.assertIn("CUSTOM REPO MAP TEMPLATE", prompt)
            self.assertIn("Do not report bugs in this phase.", prompt)

    def test_all_semantic_phases_have_specific_prompt_contracts(self) -> None:
        self.assertEqual(set(SEMANTIC_PHASE_PROMPT_SPECS), set(SEMANTIC_PHASES))
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)

            for phase in sorted(SEMANTIC_PHASES):
                with self.subTest(phase=phase):
                    spec = SEMANTIC_PHASE_PROMPT_SPECS[phase]
                    self.assertTrue(spec.get("role"))
                    self.assertTrue(spec.get("inputs"))
                    self.assertTrue(spec.get("outputs"))
                    self.assertTrue(spec.get("instructions"))
                    prompt = phase_prompt(phase, run_dir)
                    self.assertIn("Role:", prompt)
                    self.assertIn("Inputs:", prompt)
                    self.assertIn("Required outputs:", prompt)
                    self.assertIn("Phase instructions:", prompt)

    def test_phase_prompt_names_reviewer_outputs_and_exact_intent_classifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)

            reviewer_prompt = phase_prompt("reviewer_fanout", run_dir)
            failure_prompt = phase_prompt("intent_test_failure_analysis", run_dir)

        self.assertIn("raw-reviewers/*.json", reviewer_prompt)
        self.assertIn("reviewers/security.md", reviewer_prompt)
        self.assertIn("reviewers/correctness.md", reviewer_prompt)
        self.assertIn("codex-reviewer-output/v1", reviewer_prompt)
        self.assertIn("intent/intent-test-results.json", failure_prompt)
        self.assertIn("flaky_or_nondeterministic", failure_prompt)
        self.assertIn("passed_no_bug_reproduced", failure_prompt)
        self.assertIn("skipped_not_runnable", failure_prompt)
        self.assertNotIn("flaky_nondeterministic", failure_prompt)

    def test_validate_phase_outputs_rejects_missing_or_wrong_schema_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "repo-map.json"):
                validate_phase_outputs(run_dir, "repo_map")

            (run_dir / "repo-map.json").write_text(json.dumps({"schema_version": "wrong/v1"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "repo-map/v1"):
                validate_phase_outputs(run_dir, "repo_map")

            (run_dir / "repo-map.json").write_text(json.dumps({"schema_version": "repo-map/v1", "areas": []}), encoding="utf-8")
            validate_phase_outputs(run_dir, "repo_map")

    def test_validate_phase_outputs_rejects_empty_reviewer_fanout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            raw_dir = run_dir / "raw-reviewers"
            raw_dir.mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "no raw reviewer JSON"):
                validate_phase_outputs(run_dir, "reviewer_fanout")

            (raw_dir / "security.json").write_text(
                json.dumps({"schema_version": "codex-reviewer-output/v1", "findings": []}),
                encoding="utf-8",
            )
            validate_phase_outputs(run_dir, "reviewer_fanout")

    def test_fallback_semantic_artifact_repairs_string_generated_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            generated_dir = run_dir / "intent" / "generated-tests"
            generated_dir.mkdir(parents=True)
            first_path = "intent/generated-tests/intent-agent-fix-api-base.test.jsx"
            second_path = "intent/generated-tests/intent-review-artifact-url.test.jsx"
            (run_dir / first_path).write_text("test('first', () => {})\n", encoding="utf-8")
            (run_dir / second_path).write_text("test('second', () => {})\n", encoding="utf-8")
            source_path = run_dir / "intent" / "intent-test-source.json"
            source_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [first_path, second_path],
                        "tests": [
                            {
                                "test_id": "ITP-001",
                                "path": first_path,
                                "command": ["npm", "test", "--", first_path],
                                "target_finding_ids": ["COR-P0-002-01"],
                            },
                            {
                                "test_id": "ITP-002",
                                "path": second_path,
                                "command": ["npm", "test", "--", second_path],
                                "target_finding_ids": ["SEC-P0-002-01"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "generated_tests\\[0\\] must be an object"):
                validate_phase_outputs(run_dir, "intent_test_writing")

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_test_writing")
            validate_phase_outputs(run_dir, "intent_test_writing")
            payload = json.loads(source_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["generated_tests"][0]["test_id"], "ITP-001")
        self.assertEqual(payload["generated_tests"][0]["path"], first_path)
        self.assertEqual(payload["generated_tests"][0]["artifact_refs"], ["art_intent_test_source"])
        self.assertEqual(payload["generated_tests"][1]["command"], ["npm", "test", "--", second_path])

    def test_fallback_semantic_artifact_repairs_outcome_style_intent_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            raw_path = run_dir / "intent" / "intent-test-results.raw.json"
            raw_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-run-results/v1",
                        "run_id": "run_1",
                        "test_runs": [
                            {
                                "schema_version": "project-test-run/v1",
                                "test_id": "intent-test-001",
                                "status": "skipped",
                                "exit_code": None,
                                "duration_ms": 0,
                                "timed_out": False,
                                "skip_reason": "generated test command is not allowed by worker policy",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result_path = run_dir / "intent" / "intent-test-results.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-result/v1",
                        "run_id": "run_1",
                        "test_results": [
                            {
                                "test_id": "intent-test-001",
                                "outcome": "skipped_not_runnable",
                                "raw_status": "skipped",
                                "classification_basis": "Worker policy blocked the generated command before execution.",
                                "observed_output": "generated test command is not allowed by worker policy",
                                "notes": "The test did not run.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "test_results\\[0\\].status"):
                validate_phase_outputs(run_dir, "intent_test_failure_analysis")

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_test_failure_analysis")
            validate_phase_outputs(run_dir, "intent_test_failure_analysis")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            result = payload["test_results"][0]

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["classification"], "skipped_not_runnable")
        self.assertEqual(result["confidence"], 0.0)
        self.assertIn("Worker policy blocked", result["evidence"][0])

    def test_validate_phase_outputs_rejects_malformed_intent_test_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            result_path = run_dir / "intent" / "intent-test-results.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-result/v1",
                        "test_results": [{"test_id": "ITV-001", "classification": "unclear_requirement"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "test_results\\[0\\].status"):
                validate_phase_outputs(run_dir, "intent_test_failure_analysis")

            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-result/v1",
                        "test_results": [
                            {
                                "test_id": "ITV-001",
                                "status": "failed",
                                "classification": "unclear_requirement",
                                "confidence": 0.0,
                                "evidence": [],
                                "artifacts": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            validate_phase_outputs(run_dir, "intent_test_failure_analysis")

            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-result/v1",
                        "test_results": [
                            {
                                "test_id": "ITV-001",
                                "status": "passed",
                                "classification": "passed_no_bug_reproduced",
                                "confidence": 0.0,
                                "evidence": [],
                                "artifacts": [],
                            },
                            {
                                "test_id": "ITV-002",
                                "status": "skipped",
                                "classification": "skipped_not_runnable",
                                "confidence": 0.0,
                                "evidence": [],
                                "artifacts": [],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            validate_phase_outputs(run_dir, "intent_test_failure_analysis")

    def test_validate_phase_outputs_rejects_malformed_intent_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            map_path = run_dir / "intent" / "intent-map.json"
            map_path.write_text(json.dumps({"schema_version": "intent-map/v1", "bundle_id": "all"}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "behavioral_contracts"):
                validate_phase_outputs(run_dir, "intent_mining")

            map_path.write_text(
                json.dumps({"schema_version": "intent-map/v1", "bundle_id": "all", "behavioral_contracts": []}),
                encoding="utf-8",
            )
            validate_phase_outputs(run_dir, "intent_mining")

    def test_fallback_semantic_artifact_repairs_object_intent_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            map_path = run_dir / "intent" / "intent-map.json"
            map_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-map/v1",
                        "behavioral_contracts": {
                            "BC-001": {
                                "title": "Admin routes require authenticated access",
                                "evidence": ["docs/admin-auth.md"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "bundle_id"):
                validate_phase_outputs(run_dir, "intent_mining")

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_mining")
            validate_phase_outputs(run_dir, "intent_mining")
            payload = json.loads(map_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["bundle_id"], "all")
        self.assertEqual(payload["behavioral_contracts"][0]["contract_id"], "BC-001")
        self.assertEqual(payload["behavioral_contracts"][0]["title"], "Admin routes require authenticated access")

    def test_fallback_semantic_artifact_repairs_missing_intent_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            map_path = run_dir / "intent" / "intent-map.json"
            map_path.write_text(json.dumps({"schema_version": "intent-map/v1", "bundle_id": "all"}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "behavioral_contracts"):
                validate_phase_outputs(run_dir, "intent_mining")

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_mining")
            validate_phase_outputs(run_dir, "intent_mining")
            payload = json.loads(map_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["behavioral_contracts"], [])
        self.assertIn("omitted or malformed", payload["unknowns"][0])

    def test_fallback_semantic_artifact_repairs_split_intent_test_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "clusters.json").write_text(
                json.dumps(
                    {
                        "schema_version": "cluster-output/v1",
                        "clusters": [
                            {
                                "cluster_id": "cluster-SEC-001",
                                "candidate_findings": [{"id": "SEC-001"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            plan_path = run_dir / "intent" / "intent-test-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-plan/v1",
                        "test_targets": [
                            {
                                "id": "target-SEC-001",
                                "test_id": "intent-test-001",
                                "finding_ids": ["SEC-001"],
                                "priority": "high",
                            }
                        ],
                        "tests": [
                            {
                                "id": "intent-test-001",
                                "finding_id": "SEC-001",
                                "goal": "Demonstrate proxy request-size enforcement rejects oversized streamed bodies.",
                                "files_under_test": ["worker.js", "worker.test.js"],
                                "runnable_command": "npm test -- worker.test.js",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "title is missing"):
                validate_phase_outputs(run_dir, "intent_test_planning")

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "intent_test_planning")
            validate_phase_outputs(run_dir, "intent_test_planning")
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            target = payload["test_targets"][0]

        self.assertEqual(target["title"], "Demonstrate proxy request-size enforcement rejects oversized streamed bodies.")
        self.assertEqual(target["expected_result_before_fix"], "unknown")
        self.assertEqual(target["linked_finding_ids"], ["cluster-SEC-001"])
        self.assertEqual(target["target_files"], ["worker.js", "worker.test.js"])
        self.assertEqual(target["command"], "npm test -- worker.test.js")

    def test_validate_phase_outputs_rejects_duplicate_intent_plan_test_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "clusters.json").write_text(
                json.dumps({"schema_version": "cluster-output/v1", "clusters": [{"cluster_id": "C-001"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-plan/v1",
                        "test_targets": [
                            {
                                "test_id": "ITV-001",
                                "linked_finding_ids": ["C-001"],
                                "title": "first",
                                "expected_result_before_fix": "fail",
                            },
                            {
                                "test_id": "ITV-001",
                                "linked_finding_ids": ["C-001"],
                                "title": "second",
                                "expected_result_before_fix": "fail",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "duplicated"):
                validate_phase_outputs(run_dir, "intent_test_planning")

    def test_validate_phase_outputs_rejects_unknown_intent_linked_finding_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "clusters.json").write_text(
                json.dumps({"schema_version": "cluster-output/v1", "clusters": [{"cluster_id": "C-001"}]}),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-plan.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-plan/v1",
                        "test_targets": [
                            {
                                "test_id": "ITV-001",
                                "linked_finding_ids": ["C-999"],
                                "title": "unknown cluster link",
                                "expected_result_before_fix": "fail",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "unknown cluster id C-999"):
                validate_phase_outputs(run_dir, "intent_test_planning")

    def test_validate_phase_outputs_rejects_missing_generated_test_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "intent" / "intent-test-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-source/v1",
                        "generated_tests": [
                            {
                                "test_id": "ITV-001",
                                "path": ".codex-review/generated-tests/missing.test.py",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "path does not exist"):
                validate_phase_outputs(run_dir, "intent_test_writing")

    def test_validate_phase_outputs_rejects_missing_intent_raw_output_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            output_dir = run_dir / "intent" / "test-output"
            output_dir.mkdir(parents=True)
            raw_path = run_dir / "intent" / "intent-test-results.raw.json"
            stdout_path = output_dir / "ITV-001.stdout.log"
            stderr_path = output_dir / "ITV-001.stderr.log"
            raw_path.write_text(
                json.dumps(
                    {
                        "schema_version": "intent-test-run-results/v1",
                        "test_runs": [
                            {
                                "test_id": "ITV-001",
                                "status": "failed",
                                "command": "pytest .codex-review/generated-tests/test_intent.py",
                                "stdout_path": str(stdout_path),
                                "stderr_path": str(stderr_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "stdout_path output artifact is missing"):
                validate_phase_outputs(run_dir, "intent_test_running")

            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("failed\n", encoding="utf-8")
            validate_phase_outputs(run_dir, "intent_test_running")

    def test_fallback_semantic_outputs_satisfy_phase_output_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "inventory.json").write_text(
                json.dumps({"schema_version": "inventory/v1", "summary": {"source_like_files": 2}}),
                encoding="utf-8",
            )

            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "repo_map")
            fallback_semantic_artifact(run_dir, {"job_id": "job_1"}, "risk_routing")

            validate_phase_outputs(run_dir, "repo_map")
            validate_phase_outputs(run_dir, "risk_routing")

    def test_final_report_fallback_repairs_model_output_to_full_repo_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            (run_dir / "intent").mkdir(parents=True)
            (run_dir / "coverage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "coverage/v1",
                        "source_like_files_total": 1,
                        "deep_reviewed_files": 1,
                        "standard_reviewed_files": 0,
                        "light_reviewed_files": 0,
                        "inventory_only_files": 0,
                        "skipped_files": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "intent" / "intent-test-results.json").write_text(
                json.dumps({"schema_version": "intent-test-result/v1", "test_results": []}),
                encoding="utf-8",
            )
            (run_dir / "report.agent.json").write_text(
                json.dumps(
                    {
                        "schema_version": "v1",
                        "findings": [
                            {
                                "title": "Bad output",
                                "severity": "high",
                                "confidence": 0.9,
                                "locations": [{"path": "app.py", "line_start": 3, "line_end": 4}],
                                "evidence": [],
                                "impact": "Impact.",
                                "recommendation": "Fix it.",
                                "next_agent_task": "Patch the issue.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "codex-full-repo-review"):
                validate_phase_outputs(run_dir, "final_report_json")
            fallback_semantic_artifact(run_dir, {"job_id": "job_1", "commit": "abc"}, "final_report_json")
            validate_phase_outputs(run_dir, "final_report_json")
            report = json.loads((run_dir / "report.agent.json").read_text(encoding="utf-8"))

        self.assertEqual(report["schema_id"], "codex-full-repo-review")
        self.assertEqual(report["commit_sha"], "abc")
        self.assertEqual(report["findings"][0]["locations"][0]["start_line"], 3)
        self.assertEqual(report["findings"][0]["locations"][0]["end_line"], 4)
        self.assertEqual(report["next_agent_tasks"], ["Patch the issue."])

    def test_run_semantic_phase_requires_codex_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=object())

            with self.assertRaisesRegex(RuntimeError, "Codex app-server is missing"):
                worker.run_semantic_phase(None, repo, run_dir, {"job_id": "job_1"}, "repo_map")

            self.assertFalse((run_dir / "repo-map.json").exists())

    def test_run_semantic_phase_does_not_synthesize_missing_codex_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text(json.dumps({"thread_id": "thread_1"}), encoding="utf-8")
            calls = []

            class AppServer:
                def run_turn(self, **kwargs: object) -> None:
                    calls.append(kwargs)
                    return None

            job = {
                "job_id": "job_1",
                "model_profile": {
                    "default_model": "gpt-5",
                    "core_effort": "high",
                    "non_core_effort": "medium",
                },
                "review_request": {
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 1800,
                    },
                    "budget": {"max_wall_time_seconds": 14400},
                },
                "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
            }
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=object())

            worker.run_semantic_phase(AppServer(), repo, run_dir, job, "repo_map")

            self.assertFalse(calls[0]["read_only"])
            self.assertFalse((run_dir / "repo-map.json").exists())
            with self.assertRaisesRegex(RuntimeError, "repo-map"):
                validate_phase_outputs(run_dir, "repo_map")

    def test_repair_semantic_phase_requires_codex_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=object())

            with self.assertRaisesRegex(RuntimeError, "Codex app-server is missing"):
                worker.repair_semantic_phase_outputs(None, repo, run_dir, {"job_id": "job_1"}, "repo_map", RuntimeError("bad schema"))

            self.assertFalse((run_dir / "repo-map.json").exists())

    def test_semantic_phase_output_repair_turn_fixes_invalid_schema_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text(json.dumps({"thread_id": "thread_1"}), encoding="utf-8")
            (run_dir / "repo-map.json").write_text(json.dumps({"schema_version": "wrong/v1"}), encoding="utf-8")
            calls = []

            class AppServer:
                def run_turn(self, **kwargs: object) -> None:
                    calls.append(kwargs)
                    (run_dir / "repo-map.json").write_text(
                        json.dumps({"schema_version": "repo-map/v1", "areas": []}),
                        encoding="utf-8",
                    )

            job = {
                "model_profile": {"default_model": "gpt-5.5", "core_effort": "high"},
                "review_request": {
                    "budget": {"max_wall_time_seconds": 14400},
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 1800,
                    },
                },
                "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
            }
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=object())

            with self.assertRaisesRegex(RuntimeError, "repo-map/v1"):
                validate_phase_outputs(run_dir, "repo_map")
            worker.repair_semantic_phase_outputs(AppServer(), repo, run_dir, job, "repo_map", RuntimeError("bad schema"))
            validate_phase_outputs(run_dir, "repo_map")

        self.assertEqual(calls[0]["thread_id"], "thread_1")
        self.assertEqual(calls[0]["effort"], "high")
        self.assertFalse(calls[0]["read_only"])
        self.assertIn("Phase output repair: repo_map", calls[0]["prompt"])
        self.assertIn("Repair only the required output file", calls[0]["prompt"])

    def test_semantic_phase_output_repair_falls_back_when_codex_omits_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "repo"
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text(json.dumps({"thread_id": "thread_1"}), encoding="utf-8")
            calls = []

            class AppServer:
                def run_turn(self, **kwargs: object) -> None:
                    calls.append(kwargs)

            job = {
                "model_profile": {"default_model": "gpt-5.5", "core_effort": "high"},
                "review_request": {
                    "budget": {"max_wall_time_seconds": 14400},
                    "policy": {
                        "allow_source_modification": False,
                        "allow_dependency_install": False,
                        "allow_network": False,
                        "helper_scripts_standard_library_only": True,
                        "turn_timeout_seconds": 1800,
                    },
                },
                "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
            }
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=object())

            with self.assertRaisesRegex(RuntimeError, "repo-map.json"):
                validate_phase_outputs(run_dir, "repo_map")
            worker.repair_semantic_phase_outputs(AppServer(), repo, run_dir, job, "repo_map", RuntimeError("missing output"))
            validate_phase_outputs(run_dir, "repo_map")

        self.assertEqual(len(calls), 1)

    def test_hash_artifact_phase_requires_v1_manifest_object_in_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "artifact-manifest.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "must be an object"):
                validate_phase_outputs(run_dir, "hash_artifacts", artifact_dir)

            (artifact_dir / "artifact-manifest.json").write_text(
                json.dumps({"schema_version": "artifact-manifest/v1", "items": []}),
                encoding="utf-8",
            )
            validate_phase_outputs(run_dir, "hash_artifacts", artifact_dir)

    def test_build_envelope_contains_stable_v1_protocol_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            write_completed_artifact_inputs(run_dir)
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            job = {
                "job_id": "job_1",
                "run_id": "run_1",
                "lease_id": "lease_1",
                "repo": "acme/api",
                "commit": "abc123",
            }

            envelope = worker.build_envelope(job, "run_1", "completed", 1.0, artifact_dir, run_dir)

        self.assertEqual(envelope["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(envelope["message_type"], "review_run_result")
        self.assertEqual(envelope["job"]["job_id"], "job_1")
        self.assertEqual(envelope["job"]["run_id"], "run_1")
        self.assertEqual(envelope["job"]["lease_id"], "lease_1")
        self.assertEqual(envelope["worker"]["worker_id"], "wk_1")
        self.assertEqual(envelope["execution"]["status"], "completed")
        self.assertEqual(envelope["progress_final"]["status"], "completed")
        self.assertEqual(envelope["progress_final"]["overall_percent"], 100.0)
        self.assertEqual(envelope["progress_final"]["run_id"], "run_1")
        self.assertEqual(envelope["quality_gate"]["status"], "pass")
        self.assertTrue(envelope["artifact_manifest"])
        for item in envelope["artifact_manifest"]:
            self.assertIn("artifact_id", item)
            self.assertIn("kind", item)
            self.assertIn("media_type", item)
            self.assertIn("schema_id", item)
            self.assertIn("schema_version", item)
            self.assertIsInstance(item.get("required"), bool)
            self.assertIsInstance(item.get("size_bytes"), int)
            self.assertEqual(item.get("storage", {}).get("type"), "server_artifact")

    def test_failed_envelope_includes_failure_category_and_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            job = {"job_id": "job_1", "run_id": "run_1", "lease_id": "lease_1", "repo": "acme/api"}

            envelope = worker.build_envelope(
                job,
                "run_1",
                "failed",
                1.0,
                artifact_dir,
                run_dir,
                error='{"codexErrorInfo":"ContextWindowExceeded"}',
                phase="reviewer_fanout",
            )

        self.assertEqual(envelope["error"]["code"], "CODEX_CONTEXT_WINDOW_EXCEEDED")
        self.assertEqual(envelope["error"]["category"], "context_budget_failure")
        self.assertEqual(envelope["error"]["failure_action"], "split_bundle_and_retry")
        self.assertIs(envelope["error"]["retryable"], True)

    def test_cancelled_envelope_includes_cancel_failure_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)))
            job = {"job_id": "job_1", "run_id": "run_1", "lease_id": "lease_1", "repo": "acme/api"}

            envelope = worker.build_envelope(
                job,
                "run_1",
                "cancelled",
                1.0,
                artifact_dir,
                run_dir,
                error="cancel requested: user_requested",
                phase="reviewer_fanout",
            )

        self.assertEqual(envelope["error"]["category"], "job_cancelled")
        self.assertEqual(envelope["error"]["failure_action"], "cancel_job")
        self.assertIs(envelope["error"]["retryable"], False)

    def test_phase_failure_persists_structured_failure_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "repo" / ".codex-review" / "runs" / "run_1"
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=tmp_dir))
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")

            worker.fail_phase(active, run_dir, "reviewer_json_validation", RuntimeError("invalid JSON"))

            run_state = json.loads((run_dir / "run-state.json").read_text(encoding="utf-8"))

        self.assertEqual(run_state["failure"]["category"], "json_schema_failure")
        self.assertEqual(run_state["failure"]["failure_action"], "repair_output")
        self.assertIs(run_state["failure"]["retryable"], True)

    def test_result_submit_failure_spools_pending_and_keeps_active_job(self) -> None:
        class Client:
            def result(self, job_id: str, payload: dict) -> None:
                raise RuntimeError("server unavailable")

            def heartbeat(self, **payload: dict) -> dict:
                return {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_dir = root / "artifacts" / "run_1"
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=Client())
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")
            worker.state.set_active(active)

            submitted = worker.submit_result_or_mark_pending(
                active,
                "job_1",
                {"status": "done"},
                artifact_dir,
                {"protocol_version": "review-worker-protocol/v1"},
            )

            if active.state in {"completed", "failed", "cancelled", "partial_completed"}:
                worker.state.clear_active(active.state)

            pending = __import__("json").loads((artifact_dir / "pending-submit.json").read_text(encoding="utf-8"))

        self.assertFalse(submitted)
        self.assertEqual(worker.state.active_job.job_id, "job_1")
        self.assertEqual(active.state, "finishing")
        self.assertEqual(pending["status"], "result_submit_pending")

    def test_recover_pending_submission_retries_and_clears_active_job(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.results = []

            def result(self, job_id: str, payload: dict) -> None:
                self.results.append((job_id, payload))

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            client = Client()
            worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home=str(root)), client=client)
            artifact_dir = worker.isolation.artifacts / "run_1"
            artifact_dir.mkdir(parents=True)
            envelope = {
                "protocol_version": "review-worker-protocol/v1",
                "job": {"run_id": "run_1", "job_id": "job_1", "lease_id": "lease_1"},
                "execution": {"status": "completed", "duration_ms": 10},
                "summary": {"top_findings": []},
                "artifact_manifest": [],
            }
            (artifact_dir / "result-envelope.json").write_text(json.dumps(envelope), encoding="utf-8")
            (artifact_dir / "pending-submit.json").write_text(
                json.dumps(
                    {
                        "run_id": "run_1",
                        "job_id": "job_1",
                        "lease_id": "lease_1",
                        "attempt_id": "wk_1-1",
                        "status": "result_submit_pending",
                        "result_status": "done",
                        "result_envelope_path": "result-envelope.json",
                    }
                ),
                encoding="utf-8",
            )

            worker.recover_pending_submissions()

            pending_exists = (artifact_dir / "pending-submit.json").exists()
            active_job = worker.state.active_job
            results = client.results

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "job_1")
        self.assertEqual(results[0][1]["status"], "done")
        self.assertEqual(results[0][1]["attempt_id"], "wk_1-1")
        self.assertFalse(pending_exists)
        self.assertIsNone(active_job)

    def test_isolation_env_does_not_inherit_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                worker_id="wk_1",
                service_home=str(root),
                codex_env={"CUSTOM_FLAG": "1", "HOME": "/tmp/host", "PATH": "/opt/provider/bin"},
            )
            with patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "host-secret", "HOME": "/home/host", "PATH": "/usr/bin", "LANG": "C.UTF-8"},
                clear=True,
            ):
                worker = ReviewWorkerV1(config)
                env = worker.isolation.env(config)
                worker_root = worker.isolation.worker_root

        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["CUSTOM_FLAG"], "1")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["HOME"], str(worker_root))
        self.assertEqual(env["USERPROFILE"], str(worker_root))
        self.assertEqual(env["CODEX_HOME"], str(worker_root / "codex-home"))
        self.assertEqual(env["CODEX_SQLITE_HOME"], str(worker_root / "codex-sqlite"))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(worker_root / ".config"))
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(worker_root / ".local" / "bin"))
        self.assertIn("/opt/provider/bin", env["PATH"])
    def test_result_payload_uses_stable_v1_envelope_without_derived_topology_payload(self) -> None:
        active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk-1")
        envelope = {
            "protocol_version": "review-worker-protocol/v1",
            "job": {"run_id": "run_1"},
            "execution": {"status": "completed", "duration_ms": 10},
            "summary": {"top_findings": []},
            "artifact_manifest": [],
        }

        payload = result_payload(active, envelope, "done")

        self.assertEqual(payload["reviewWorkerProtocol"], envelope)
        self.assertFalse(any(key.lower().startswith("graph") for key in payload))
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["attempt_id"], "wk-1")

    def test_completed_progress_final_marks_cleanup_step_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run_1"
            active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk-1")
            active.current_phase = "submit_result_envelope"
            active.current_phase_status = "completed"
            active.current_phase_percent = 100.0
            active.overall_percent = 100.0
            write_json(run_dir / "progress.json", active.progress_snapshot())

            payload = progress_final_payload(run_dir, "run_1", "completed")

        cleanup_step = next(step for step in payload["steps"] if step["id"] == "cleanup_active_job")
        self.assertEqual(payload["current_phase"], "cleanup_active_job")
        self.assertEqual(cleanup_step["status"], "completed")
        self.assertEqual(cleanup_step["percent"], 100.0)

    def test_default_agent_report_is_full_repo_schema(self) -> None:
        report = default_agent_report({"job_id": "job_1", "commit": "abc"})
        self.assertEqual(report["schema_id"], "codex-full-repo-review")
        self.assertEqual(report["schema_version"], "v1")
        self.assertIn("next_agent_tasks", report)


if __name__ == "__main__":
    unittest.main()
