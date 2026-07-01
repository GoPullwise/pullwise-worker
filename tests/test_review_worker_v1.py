from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from pullwise_worker._main_part_01_bootstrap import PullwiseClient, PullwiseResponse, worker_registration_payload
from pullwise_worker.review_worker_v1 import (
    INTENT_TEST_CLASSIFICATIONS,
    PIPELINE_PHASES,
    REQUIRED_COMPLETED_ARTIFACTS,
    REQUIRED_PROMPT_FILES,
    REQUIRED_SCHEMA_FILES,
    REQUIRED_TOOL_FILES,
    ActiveJob,
    CodexQuotaMonitor,
    JobCancelled,
    JsonRpcAppServer,
    ReviewWorkerV1,
    WorkerState,
    codex_error_code,
    codex_quota_payload_from_rate_limits,
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
    inventory,
    materialize_artifacts,
    materialize_terminal_artifacts,
    pack_bundles,
    qa_gate_payload,
    upload_artifacts,
    validate_job_policy,
    validate_phase_outputs,
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
            denied_cwd, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "git status", "cwd": ".."}},
                workspace,
            )

        self.assertEqual(allowed_file, "acceptForSession")
        self.assertEqual(allowed_validation_file, "acceptForSession")
        self.assertEqual(denied_file, "decline")
        self.assertEqual(allowed_command, "acceptForSession")
        self.assertEqual(denied_install, "decline")
        self.assertEqual(allowed_validation_test, "acceptForSession")
        self.assertEqual(denied_cwd, "decline")

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

    def test_job_policy_requires_server_agent_config_and_repository_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_profile.default_model"):
            validate_job_policy({"repositoryLimits": {"maxFiles": 10, "maxBytes": 1000}})
        with self.assertRaisesRegex(ValueError, "model_profile.core_effort"):
            validate_job_policy({"agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5"}}, "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000}})
        with self.assertRaisesRegex(ValueError, "turn_timeout_seconds"):
            validate_job_policy({"agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "high"}}, "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000}})
        with self.assertRaisesRegex(ValueError, "repositoryLimits"):
            validate_job_policy({"agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "high"}, "reviewWorker": {"turnTimeoutSeconds": 1800, "scanDeadlineSeconds": 14400}}})
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

        self.assertEqual(inv["schema_version"], "inventory/v1")
        self.assertTrue(any(item["path"] == "src/auth/session.py" and "auth" in item["risk_hints"] for item in inv["files"]))
        self.assertEqual(bundle["tier"], "P0")
        self.assertIn("1 | def refresh_session():", bundle_text)
        self.assertIn("Intent test eligible: true", bundle_text)

    def test_qa_gate_rejects_invalid_main_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            run_dir = repo / ".codex-review" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (run_dir / "coverage.json").write_text('{"source_like_files_total":1,"deep_reviewed_files":1}', encoding="utf-8")
            (run_dir / "token-budget.json").write_text('{"schema_version":"token-budget/v1"}', encoding="utf-8")
            (run_dir / "report.agent.json").write_text(
                '{"schema_id":"codex-full-repo-review","schema_version":"v1","findings":[{"title":"Bad","severity":"high","confidence":1.2,"locations":[]}]}',
                encoding="utf-8",
            )

            qa = qa_gate_payload(repo, run_dir)

        self.assertEqual(qa["status"], "fail")
        self.assertTrue(any("locations" in error for error in qa["errors"]))
        self.assertTrue(any("confidence" in error for error in qa["errors"]))

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
        worker.state.set_active(active)

        worker.heartbeat()

        self.assertEqual(calls[0]["running_jobs"], 1)
        self.assertEqual(calls[0]["progress"]["current_phase"], "intent_test_running")

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

        client.register()
        client.heartbeat(running_jobs=1, active_job_ids=["job_1"], progress={"run_id": "run_job_1"})
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
        self.assertNotIn("running_jobs", heartbeat_payload)
        self.assertTrue(calls[-2][2])
        self.assertTrue(calls[-1][2])

    def test_worker_honors_v1_cancel_run_command_from_heartbeat(self) -> None:
        class Client:
            def heartbeat(self, **_payload: dict) -> dict:
                return {"commands": [{"type": "cancel_run", "run_id": "run_1", "reason": "user_requested"}]}

        worker = ReviewWorkerV1(SimpleNamespace(worker_id="wk_1", service_home="/tmp"), client=Client())
        worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
        active = ActiveJob(job_id="job_1", run_id="run_1", lease_id="lease_1", attempt_id="wk_1-1")
        worker.state.set_active(active)

        worker.heartbeat()

        self.assertTrue(active.cancel_requested)

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
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            worker.run(once=True)

        self.assertEqual(calls, ["register", "heartbeat", "claim"])

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
        payload = worker_registration_payload(SimpleNamespace(worker_id="wk_1", service_home="/var/lib/pullwise-worker"))

        self.assertEqual(payload["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(payload["worker"]["worker_id"], "wk_1")
        self.assertEqual(payload["worker"]["concurrency"]["max_active_jobs"], 1)
        self.assertFalse(payload["worker"]["concurrency"]["maintains_local_queue"])
        self.assertFalse(payload["worker"]["concurrency"]["prefetch_jobs"])
        self.assertTrue(payload["worker"]["capabilities"]["codex_app_server"])
        self.assertTrue(payload["worker"]["capabilities"]["progress_events"])
        self.assertEqual(payload["worker"]["platform"]["os"], "linux")

    def test_codex_error_mapper_returns_stable_protocol_codes(self) -> None:
        self.assertEqual(codex_error_code({"codexErrorInfo": "UsageLimitExceeded"}), "CODEX_QUOTA_EXHAUSTED")
        self.assertEqual(codex_error_code('{"codexErrorInfo":"ContextWindowExceeded"}'), "CODEX_CONTEXT_WINDOW_EXCEEDED")
        self.assertEqual(codex_error_code("unexpected"), "CODEX_UNKNOWN_ERROR")


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
            run_dir.mkdir(parents=True)
            (run_dir / "report.agent.json").write_text(
                '{"schema_id":"codex-full-repo-review","schema_version":"v1","findings":[]}',
                encoding="utf-8",
            )
            materialize_artifacts(run_dir, artifact_dir)

            manifest = __import__("json").loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            run_manifest = __import__("json").loads((run_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            kinds = {item["kind"] for item in manifest if item.get("required")}
            self.assertTrue(REQUIRED_COMPLETED_ARTIFACTS.issubset(kinds))
            self.assertEqual(manifest, run_manifest)
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

            manifest = __import__("json").loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest)
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

        self.assertIn("art_error_report", {artifact_id for _job_id, artifact_id, _payload in calls})

    def test_upload_artifacts_posts_manifest_entries_before_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
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

    def test_phase_progress_data_reports_required_v1_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            (run_dir / "raw-reviewers").mkdir(parents=True)
            (run_dir / "intent").mkdir(parents=True)
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
            (run_dir / "intent" / "intent-test-run-results.raw.json").write_text(
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

        self.assertEqual(posted[0][0], "run_1")
        event = posted[0][1]
        self.assertEqual(event["event_type"], "progress_updated")
        self.assertEqual(event["phase"], "reviewer_fanout")
        self.assertEqual(event["progress"]["status"], "running")
        self.assertEqual(event["data"]["reviewer_runs_total"], 2)
        self.assertEqual(event["data"]["reviewer_runs_completed"], 1)

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
        self.assertIn("Required outputs:\n- repo-map.json", prompt)
        self.assertIn("--- 00_repo_mapper.md ---", prompt)
        self.assertIn("CUSTOM REPO MAP TEMPLATE", prompt)
        self.assertIn("Do not report bugs in this phase.", prompt)

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

    def test_hash_artifact_phase_requires_list_manifest_in_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "artifact-manifest.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "must be a list"):
                validate_phase_outputs(run_dir, "hash_artifacts", artifact_dir)

            (artifact_dir / "artifact-manifest.json").write_text("[]", encoding="utf-8")
            validate_phase_outputs(run_dir, "hash_artifacts", artifact_dir)

    def test_build_envelope_contains_stable_v1_protocol_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
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
        self.assertEqual(envelope["quality_gate"]["status"], "fail")
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

    def test_default_agent_report_is_full_repo_schema(self) -> None:
        report = default_agent_report({"job_id": "job_1", "commit": "abc"})
        self.assertEqual(report["schema_id"], "codex-full-repo-review")
        self.assertEqual(report["schema_version"], "v1")
        self.assertIn("next_agent_tasks", report)


if __name__ == "__main__":
    unittest.main()
