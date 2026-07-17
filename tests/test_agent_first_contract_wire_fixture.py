from __future__ import annotations

import base64
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest


WORKER_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = WORKER_ROOT.parent / "pullwise-server"
FIXTURE_PATH = (
    WORKER_ROOT
    / "contracts"
    / "agent-first"
    / "fixtures"
    / "review-worker-protocol-v1.json"
)

if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
server_app = importlib.import_module("pullwise_server.app")


class AgentFirstContractWireFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.protocol_version = cls.fixture["protocol_version"]
        cls.cases = cls.fixture["cases"]

    def case(self, name: str) -> dict:
        value = self.cases[name]
        self.assertIsInstance(value, dict)
        return value

    def test_fixture_has_only_the_named_strict_v1_cases(self) -> None:
        self.assertEqual(
            set(self.cases),
            {
                "register",
                "lease",
                "heartbeat_idle",
                "heartbeat_active_with_estimate",
                "event_progress",
                "artifact_report_agent",
                "result_completed",
                "result_partial_completed",
            },
        )
        self.assertEqual(self.protocol_version, "review-worker-protocol/v1")
        self.assertEqual(self.protocol_version, server_app.WORKER_PROTOCOL_VERSION)
        for name, body in self.cases.items():
            envelope = body.get("reviewWorkerProtocol") if name.startswith("result_") else body
            self.assertEqual(envelope["protocol_version"], self.protocol_version, name)

    def test_register_preserves_current_one_slot_linux_invariants(self) -> None:
        body = self.case("register")
        worker = body["worker"]
        concurrency = worker["concurrency"]
        capabilities = worker["capabilities"]

        self.assertEqual(worker["worker_id"], "wk_1")
        self.assertEqual(concurrency["max_active_jobs"], 1)
        self.assertIs(concurrency["maintains_local_queue"], False)
        self.assertIs(concurrency["prefetch_jobs"], False)
        self.assertEqual(worker["platform"], {"os": "linux", "arch": "x86_64"})
        for key in (
            "codex_app_server",
            "full_repo_scan",
            "progress_events",
            "cancellation",
            "intent_test_validation",
        ):
            self.assertIs(capabilities[key], True)
        self.assertEqual(capabilities["max_active_jobs"], 1)

    def test_lease_is_accepted_by_the_current_server_validator(self) -> None:
        body = self.case("lease")

        self.assertIsNone(server_app.worker_v1_lease_validation_error(body))
        self.assertEqual(body["worker_id"], "wk_1")
        self.assertEqual(
            body["capacity"],
            {
                "available_job_slots": 1,
                "active_jobs": 0,
                "maintains_local_queue": False,
                "local_queue_depth": 0,
            },
        )

    def test_idle_and_active_heartbeats_are_accepted(self) -> None:
        idle = self.case("heartbeat_idle")
        active = self.case("heartbeat_active_with_estimate")

        for name, body in (("idle", idle), ("active", active)):
            with self.subTest(name=name):
                self.assertIsNone(server_app.worker_v1_heartbeat_validation_error(body))
                self.assertNotIn("running_jobs", body)
                self.assertNotIn("active_job_ids", body)

        self.assertEqual(idle["status"], "idle")
        self.assertIsNone(idle["active_run_id"])
        self.assertEqual(idle["concurrency"]["available_job_slots"], 1)
        self.assertEqual(active["status"], "busy")
        self.assertEqual(active["active_run_id"], active["progress"]["run_id"])
        self.assertEqual(active["concurrency"]["available_job_slots"], 0)
        estimate = active["progress"]["estimate"]
        self.assertIsNone(server_app.scan_estimate_validation_error(estimate))
        self.assertEqual(estimate["basis"], "current_run_work_graph")

    def test_progress_event_is_accepted_and_uses_a_supported_type(self) -> None:
        body = self.case("event_progress")

        self.assertIn(body["event_type"], server_app.REVIEW_RUN_EVENT_TYPES)
        self.assertIsNone(
            server_app.worker_v1_event_validation_error(
                body,
                body["run_id"],
                body["worker_id"],
            )
        )
        self.assertGreater(body["sequence"], 0)
        self.assertEqual(body["progress"]["status"], "running")

    def test_report_agent_artifact_is_accepted_and_content_addressed(self) -> None:
        body = self.case("artifact_report_agent")

        self.assertIsNone(server_app.worker_v1_artifact_upload_validation_error(body))
        content = base64.b64decode(body["content_base64"], validate=True)
        artifact = body["artifact"]
        self.assertEqual(artifact["kind"], "report.agent")
        self.assertEqual(artifact["size_bytes"], len(content))
        self.assertEqual(artifact["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(body["attempt_id"], "wk_1-1")

    def test_terminal_result_envelopes_are_accepted(self) -> None:
        expected = {
            "result_completed": (
                "done",
                "completed",
                {"report.human", "report.agent", "coverage", "qa", "token_budget"},
            ),
            "result_partial_completed": (
                "partial_completed",
                "partial_completed",
                {"worker_log", "qa", "error_report"},
            ),
        }
        for name, (wrapper_status, execution_status, required_kinds) in expected.items():
            with self.subTest(name=name):
                body = self.case(name)
                envelope = body["reviewWorkerProtocol"]
                envelope_job = envelope["job"]
                job = {
                    "job_id": envelope_job["job_id"],
                    "run_id": envelope_job["run_id"],
                    "lease_id": envelope_job["lease_id"],
                    "claimed_by_worker_id": envelope["worker"]["worker_id"],
                }

                validated = server_app.validate_review_worker_protocol_envelope(
                    job,
                    body,
                    status=body["status"],
                )

                self.assertEqual(validated, envelope)
                self.assertEqual(body["status"], wrapper_status)
                self.assertEqual(envelope["message_type"], "review_run_result")
                self.assertEqual(envelope["job"]["job_type"], "repo_review.full_scan")
                self.assertEqual(envelope["execution"]["status"], execution_status)
                self.assertEqual(envelope["execution"]["review_mode"], "full_repo")
                self.assertEqual(
                    {item["kind"] for item in envelope["artifact_manifest"] if item["required"] is True},
                    required_kinds,
                )


if __name__ == "__main__":
    unittest.main()
