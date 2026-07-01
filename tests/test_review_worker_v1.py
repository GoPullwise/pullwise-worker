from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from pullwise_worker.review_worker_v1 import (
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
    model_for_job,
    review_worker_policy_for_job,
    turn_timeout_for_job,
    result_payload,
    materialize_artifacts,
    materialize_terminal_artifacts,
    upload_artifacts,
    validate_job_policy,
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

            allowed_file, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "fileChange", "paths": [".codex-review/runs/run_1/out.json"]}},
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
            denied_cwd, _reason = decide_approval(
                {"method": "approval/request", "params": {"type": "commandExecution", "command": "git status", "cwd": ".."}},
                workspace,
            )

        self.assertEqual(allowed_file, "acceptForSession")
        self.assertEqual(denied_file, "decline")
        self.assertEqual(allowed_command, "acceptForSession")
        self.assertEqual(denied_install, "decline")
        self.assertEqual(denied_cwd, "decline")

    def test_pipeline_has_explicit_codex_auth_check_before_bootstrap(self) -> None:
        phases = [phase for phase, _progress in PIPELINE_PHASES]

        self.assertLess(phases.index("initialize_codex_connection"), phases.index("check_codex_auth"))
        self.assertLess(phases.index("check_codex_auth"), phases.index("bootstrap_helper_scripts"))

    def test_job_policy_requires_server_agent_config_and_repository_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "agentConfig.codex.model"):
            validate_job_policy({"repositoryLimits": {"maxFiles": 10, "maxBytes": 1000}})
        with self.assertRaisesRegex(ValueError, "reasoningEffort"):
            validate_job_policy({"agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5"}}, "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000}})
        with self.assertRaisesRegex(ValueError, "reviewWorker"):
            validate_job_policy({"agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "high"}}, "repositoryLimits": {"maxFiles": 10, "maxBytes": 1000}})
        with self.assertRaisesRegex(ValueError, "repositoryLimits"):
            validate_job_policy({"agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "high"}, "reviewWorker": {"turnTimeoutSeconds": 1800, "scanDeadlineSeconds": 14400}}})

    def test_model_effort_and_timeout_come_from_job_policy(self) -> None:
        job = {
            "agentConfig": {
                "provider": "codex",
                "codex": {"model": "gpt-5.5", "reasoningEffort": "high"},
                "reviewWorker": {"turnTimeoutSeconds": 1800, "scanDeadlineSeconds": 14400},
            },
            "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
        }

        self.assertEqual(model_for_job(job), "gpt-5.5")
        self.assertEqual(turn_timeout_for_job(job), 1800)
        self.assertEqual(review_worker_policy_for_job(job)["scanDeadlineSeconds"], 14400)
        self.assertEqual(effort_for_phase(job, "reviewer_fanout"), "high")
        self.assertEqual(effort_for_phase(job, "inventory_repository"), "medium")

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
            "agentConfig": {"provider": "codex", "codex": {"model": "gpt-5.5", "reasoningEffort": "high"}, "reviewWorker": {"turnTimeoutSeconds": 1800, "scanDeadlineSeconds": 14400}},
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
            kinds = {item["kind"] for item in manifest if item.get("required")}
            self.assertTrue(REQUIRED_COMPLETED_ARTIFACTS.issubset(kinds))
            for item in manifest:
                self.assertIn("sha256", item)
                self.assertIn("size_bytes", item)
                self.assertEqual(item["schema_version"], "v1")

    def test_terminal_artifacts_do_not_require_completed_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            run_dir = root / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
            materialize_terminal_artifacts(run_dir, artifact_dir, "failed", error="boom")

            manifest = __import__("json").loads((artifact_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest)
            self.assertFalse(any(item.get("required") for item in manifest))
            self.assertIn("error-report.json", {item["name"] for item in manifest})

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

            class Client:
                def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                    calls.append((job_id, artifact_id, payload))
                    return {"accepted": True}

            upload_artifacts(Client(), "job_1", "wk_1-1", artifact_dir)

        uploaded_ids = {artifact_id for _job_id, artifact_id, _payload in calls}
        self.assertIn("art_report_human", uploaded_ids)
        self.assertIn("art_report_agent", uploaded_ids)
        for job_id, _artifact_id, payload in calls:
            self.assertEqual(job_id, "job_1")
            self.assertEqual(payload["attempt_id"], "wk_1-1")
            self.assertEqual(payload["run_id"], "run_1")
            self.assertIn("content_base64", payload)

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
    def test_result_payload_uses_stable_v1_envelope_without_graph_report(self) -> None:
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

