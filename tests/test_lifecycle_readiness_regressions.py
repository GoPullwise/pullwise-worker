from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker import _main_part_08_lifecycle_cleanup as lifecycle
from pullwise_worker import review_worker_v1 as review_worker_v1_module
from pullwise_worker._main_part_01_bootstrap import PullwiseHTTPError, PullwiseRequestError
from pullwise_worker.review_worker_v1 import (
    ActiveJob,
    ReviewWorkerV1,
    write_uploaded_artifact_manifest,
)


class FakeCodexClient:
    def __init__(self, *, quota_error: str | None = None) -> None:
        self.quota_error = quota_error

    def is_running(self) -> bool:
        return True

    def request(
        self,
        _method: str,
        _params: dict | None = None,
        *,
        timeout_seconds: int = 30,
    ) -> dict:
        del timeout_seconds
        if self.quota_error:
            raise RuntimeError(self.quota_error)
        return {"rateLimits": {"codex": {"usedPercent": 0}}}

    def set_events_path(self, _events_path: Path) -> None:
        return None

    def close(self) -> None:
        return None


class RecordingControlPlane:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.heartbeats: list[dict] = []
        self.results: list[tuple[str, dict]] = []

    def register(self) -> dict:
        self.calls.append("register")
        return {}

    def heartbeat(self, **payload: dict) -> dict:
        self.calls.append("heartbeat")
        self.heartbeats.append(payload)
        return {}

    def claim(self) -> None:
        self.calls.append("claim")
        return None

    def result(self, job_id: str, payload: dict) -> None:
        self.calls.append("result")
        self.results.append((job_id, payload))


def worker_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="wk_1",
        service_home=str(root),
        worker_root=str(root),
        poll_seconds=1,
        cleanup_interval_seconds=1,
    )


def disable_posix_lock(worker: ReviewWorkerV1) -> None:
    worker.lock.acquire = lambda: None  # type: ignore[method-assign]
    worker.lock.release = lambda: None  # type: ignore[method-assign]


class LifecycleReadinessRegressionTests(unittest.TestCase):
    def test_restart_replays_terminal_outbox_before_claiming_another_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "workspaces" / "run_1" / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            runtime_dir = root / "runtime"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            active_marker = {
                "job_id": "job_1",
                "run_id": "run_1",
                "lease_id": "lease_1",
                "attempt_id": "wk_1-1",
                "state": "finishing",
                "current_phase": "submit_result_envelope",
            }
            (runtime_dir / "active-run.json").write_text(
                json.dumps(active_marker), encoding="utf-8"
            )
            terminal_payload = {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "a" * 64,
                "reviewWorkerProtocol": {
                    "protocol_version": "review-worker-protocol/v1",
                    "job": {
                        "job_id": "job_1",
                        "run_id": "run_1",
                        "lease_id": "lease_1",
                    },
                    "execution": {"status": "completed"},
                    "artifact_manifest": [],
                },
            }
            (artifact_dir / "terminal-result-outbox.json").write_text(
                json.dumps(
                    {
                        "schema_version": "terminal-result-outbox/v1",
                        **active_marker,
                        "result_status": "done",
                        "payload_sha256": hashlib.sha256(
                            json.dumps(
                                terminal_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                        "payload": terminal_payload,
                        "attempt_count": 1,
                        "retryable": True,
                        "created_at": "2026-07-16T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            control_plane = RecordingControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual(control_plane.calls, ["register", "heartbeat", "result"])
            self.assertEqual(control_plane.results, [("job_1", terminal_payload)])
            self.assertIsNone(worker.state.active_job)
            self.assertFalse((artifact_dir / "terminal-result-outbox.json").exists())
            self.assertTrue((artifact_dir / "result-submit-succeeded.json").is_file())
            self.assertFalse((runtime_dir / "active-run.json").exists())

    def test_cancellation_authority_supersedes_done_outbox_without_mutating_audit_record(self) -> None:
        class CancellationAuthorityControlPlane(RecordingControlPlane):
            def __init__(self) -> None:
                super().__init__()
                self.artifacts: list[tuple[str, str, dict]] = []
                self.events: list[dict] = []

            def result(self, job_id: str, payload: dict) -> None:
                self.calls.append("result")
                self.results.append((job_id, payload))
                if payload.get("status") == "done":
                    raise PullwiseHTTPError(
                        "cancellation is authoritative",
                        409,
                        error_code="JOB_CANCELLATION_AUTHORITATIVE",
                        response_payload={
                            "jobStatus": "cancel_requested",
                            "jobId": "job_1",
                            "runId": "run_1",
                            "attemptId": "wk_1-1",
                        },
                    )

            def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
                self.calls.append("artifact")
                self.artifacts.append((job_id, artifact_id, payload))
                return {"accepted": True}

            def event(self, _run_id: str, payload: dict) -> dict:
                self.calls.append("event")
                self.events.append(payload)
                return {"accepted": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "workspaces" / "run_1" / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            runtime_dir = root / "runtime"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            active_marker = {
                "job_id": "job_1",
                "run_id": "run_1",
                "lease_id": "lease_1",
                "attempt_id": "wk_1-1",
                "state": "finishing",
                "current_phase": "submit_result_envelope",
            }
            (runtime_dir / "active-run.json").write_text(json.dumps(active_marker), encoding="utf-8")
            terminal_payload = {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "a" * 64,
                "reviewWorkerProtocol": {
                    "protocol_version": "review-worker-protocol/v1",
                    "message_type": "review_run_result",
                    "job": {
                        "job_id": "job_1",
                        "run_id": "run_1",
                        "lease_id": "lease_1",
                    },
                    "repository": {"full_name": "acme/api", "commit_sha": "abc123"},
                    "execution": {
                        "status": "completed",
                        "started_at": "2026-07-16T00:00:00Z",
                        "completed_at": "2026-07-16T00:10:00Z",
                        "duration_ms": 600000,
                    },
                    "progress_final": {
                        "status": "completed",
                        "current_phase": "cleanup_active_job",
                    },
                    "summary": {"result_status": "complete", "top_findings": []},
                    "artifact_manifest": [],
                    "extensions": {"worker_internal": {}},
                },
            }
            original_outbox = {
                "schema_version": "terminal-result-outbox/v1",
                **active_marker,
                "result_status": "done",
                "payload_sha256": hashlib.sha256(
                    json.dumps(
                        terminal_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "payload": terminal_payload,
                "attempt_count": 1,
                "retryable": True,
                "created_at": "2026-07-16T00:00:00Z",
            }
            (artifact_dir / "terminal-result-outbox.json").write_text(
                json.dumps(original_outbox), encoding="utf-8"
            )
            control_plane = CancellationAuthorityControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            submitted_statuses = [payload["status"] for _job_id, payload in control_plane.results]
            superseded = list(artifact_dir.glob("terminal-result-outbox.superseded.*.json"))
            self.assertEqual(submitted_statuses, ["done", "cancelled"])
            self.assertEqual(len(superseded), 1)
            archived_outbox = json.loads(superseded[0].read_text(encoding="utf-8"))
            self.assertEqual(archived_outbox["payload"], original_outbox["payload"])
            self.assertEqual(
                archived_outbox["payload_sha256"],
                original_outbox["payload_sha256"],
            )
            self.assertEqual(archived_outbox["state"], "submitting")
            self.assertEqual(archived_outbox["attempt_count"], 2)
            self.assertTrue((artifact_dir / "terminal-result-supersession.json").is_file())
            self.assertTrue((artifact_dir / "result-submit-succeeded.json").is_file())
            self.assertFalse((artifact_dir / "terminal-result-outbox.json").exists())
            self.assertIsNone(worker.state.active_job)
            required_kinds = {
                item["kind"]
                for item in control_plane.results[-1][1]["reviewWorkerProtocol"]["artifact_manifest"]
                if item.get("required") is True
            }
            self.assertEqual(required_kinds, {"qa", "worker_log", "error_report"})
            self.assertTrue(
                all("cancel_authority" in artifact_id for _job_id, artifact_id, _payload in control_plane.artifacts)
            )
            self.assertEqual(
                [event["event_type"] for event in control_plane.events],
                ["run_cancelled"],
            )

    def test_restart_resumes_cancellation_supersession_journal_without_resending_done(self) -> None:
        class ControlPlane(RecordingControlPlane):
            def __init__(self) -> None:
                super().__init__()
                self.events: list[dict] = []

            def artifact(self, _job_id: str, _artifact_id: str, _payload: dict) -> dict:
                self.calls.append("artifact")
                return {"accepted": True}

            def event(self, _run_id: str, payload: dict) -> dict:
                self.calls.append("event")
                self.events.append(payload)
                return {"accepted": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "workspaces" / "run_1" / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            runtime_dir = root / "runtime"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            terminal_payload = {
                "status": "done",
                "attempt_id": "wk_1-1",
                "reviewWorkerProtocol": {
                    "protocol_version": "review-worker-protocol/v1",
                    "message_type": "review_run_result",
                    "job": {"job_id": "job_1", "run_id": "run_1", "lease_id": "lease_1"},
                    "repository": {"full_name": "acme/api", "commit_sha": "abc123"},
                    "execution": {"status": "completed", "duration_ms": 1},
                    "progress_final": {"status": "completed"},
                    "summary": {"result_status": "complete"},
                    "artifact_manifest": [],
                },
            }
            original_sha = ReviewWorkerV1.terminal_result_payload_sha256(terminal_payload)
            original_outbox = {
                "schema_version": "terminal-result-outbox/v1",
                "run_id": "run_1",
                "job_id": "job_1",
                "lease_id": "lease_1",
                "attempt_id": "wk_1-1",
                "result_status": "done",
                "payload_sha256": original_sha,
                "payload": terminal_payload,
                "state": "submitting",
                "attempt_count": 2,
                "retryable": True,
                "created_at": "2026-07-16T00:00:00Z",
            }
            supersession_journal = {
                "schema_version": "terminal-result-supersession/v1",
                "run_id": "run_1",
                "job_id": "job_1",
                "lease_id": "lease_1",
                "attempt_id": "wk_1-1",
                "authority_code": "JOB_CANCELLATION_AUTHORITATIVE",
                "job_status": "cancel_requested",
                "original_payload_sha256": original_sha,
                "original_outbox": original_outbox,
                "state": "authority_recorded",
            }
            supersession_journal["journal_sha256"] = (
                ReviewWorkerV1.terminal_result_payload_sha256(
                    supersession_journal
                )
            )
            (artifact_dir / "terminal-result-supersession.json").write_text(
                json.dumps(supersession_journal),
                encoding="utf-8",
            )
            (runtime_dir / "active-run.json").write_text(
                json.dumps(
                    {
                        "job_id": "job_1",
                        "run_id": "run_1",
                        "lease_id": "lease_1",
                        "attempt_id": "wk_1-1",
                        "state": "finishing",
                    }
                ),
                encoding="utf-8",
            )
            control_plane = ControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual([payload["status"] for _job_id, payload in control_plane.results], ["cancelled"])
            self.assertIsNone(worker.state.active_job)
            archive = artifact_dir / f"terminal-result-outbox.superseded.{original_sha}.json"
            self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), original_outbox)
            self.assertEqual(
                [event["event_type"] for event in control_plane.events],
                ["run_cancelled"],
            )

    def test_unique_audit_record_is_not_published_until_fully_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = ReviewWorkerV1(worker_config(root), client=RecordingControlPlane())
            audit_path = root / "artifacts" / "run_1" / "terminal-result-supersession.json"
            payload = {"schema_version": "terminal-result-supersession/v1", "run_id": "run_1"}

            with patch.object(
                review_worker_v1_module.os,
                "link",
                side_effect=OSError("injected crash before final publish"),
            ):
                with self.assertRaisesRegex(OSError, "injected crash"):
                    worker._write_json_once_durable(audit_path, payload)

            self.assertFalse(audit_path.exists())
            worker._write_json_once_durable(audit_path, payload)
            self.assertEqual(json.loads(audit_path.read_text(encoding="utf-8")), payload)

            published_path = audit_path.with_name("terminal-result-outbox.superseded.json")
            real_link = os.link

            def publish_then_fail(source: Path, target: Path) -> None:
                real_link(source, target)
                raise OSError("injected crash after final publish")

            with patch.object(
                review_worker_v1_module.os,
                "link",
                side_effect=publish_then_fail,
            ):
                with self.assertRaisesRegex(OSError, "after final publish"):
                    worker._write_json_once_durable(published_path, payload)

            self.assertEqual(
                json.loads(published_path.read_text(encoding="utf-8")),
                payload,
            )
            worker._write_json_once_durable(published_path, payload)

    def test_generation_two_cancelled_wal_replays_after_ambiguous_acceptance(
        self,
    ) -> None:
        class ResponseLossControlPlane(RecordingControlPlane):
            def result(self, job_id: str, payload: dict) -> None:
                super().result(job_id, payload)
                if payload.get("status") == "done":
                    raise PullwiseHTTPError(
                        "cancellation is authoritative",
                        409,
                        error_code="JOB_CANCELLATION_AUTHORITATIVE",
                        response_payload={
                            "jobStatus": "cancel_requested",
                            "jobId": "job_1",
                            "runId": "run_1",
                            "attemptId": "wk_1-1",
                            "acceptedResultStatus": "cancelled",
                        },
                    )
                raise PullwiseRequestError(
                    "server accepted cancelled result but the response was lost"
                )

            def artifact(
                self,
                _job_id: str,
                _artifact_id: str,
                _payload: dict,
            ) -> dict:
                return {"accepted": True}

        class ReplayControlPlane(RecordingControlPlane):
            def __init__(self) -> None:
                super().__init__()
                self.events: list[dict] = []

            def artifact(
                self,
                _job_id: str,
                _artifact_id: str,
                _payload: dict,
            ) -> dict:
                return {"accepted": True}

            def event(self, _run_id: str, payload: dict) -> dict:
                self.events.append(payload)
                return {"accepted": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = (
                root
                / "workspaces"
                / "run_1"
                / "repo"
                / ".codex-review"
                / "runs"
                / "run_1"
            )
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1")
            active.run_dir = run_dir
            envelope = {
                "protocol_version": "review-worker-protocol/v1",
                "message_type": "review_run_result",
                "job": {
                    "job_id": "job_1",
                    "run_id": "run_1",
                    "lease_id": "lease_1",
                },
                "repository": {
                    "full_name": "acme/api",
                    "commit_sha": "abc123",
                },
                "execution": {"status": "completed", "duration_ms": 1},
                "progress_final": {"status": "completed"},
                "summary": {"result_status": "complete"},
                "artifact_manifest": [],
                "extensions": {"worker_internal": {}},
            }
            done_payload = {
                "status": "done",
                "attempt_id": active.attempt_id,
                "result_checksum": "a" * 64,
                "reviewWorkerProtocol": envelope,
            }
            first_control_plane = ResponseLossControlPlane()
            first_worker = ReviewWorkerV1(
                worker_config(root),
                client=first_control_plane,
            )
            first_worker.state.set_active(active)

            accepted = first_worker.submit_result_or_record_failure(
                active,
                active.job_id,
                done_payload,
                artifact_dir,
                envelope,
            )

            self.assertFalse(accepted)
            generation_two = json.loads(
                (artifact_dir / "terminal-result-outbox.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(generation_two["generation"], 2)
            self.assertEqual(generation_two["result_status"], "cancelled")
            self.assertEqual(
                [
                    payload["status"]
                    for _job_id, payload in first_control_plane.results
                ],
                ["done", "cancelled"],
            )

            replay_control_plane = ReplayControlPlane()
            replay_worker = ReviewWorkerV1(
                worker_config(root),
                client=replay_control_plane,
            )
            disable_posix_lock(replay_worker)
            replay_worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            replay_worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                replay_worker.run(once=True)

            self.assertEqual(
                [
                    payload["status"]
                    for _job_id, payload in replay_control_plane.results
                ],
                ["cancelled"],
            )
            self.assertEqual(
                replay_control_plane.results[0][1],
                generation_two["payload"],
            )
            self.assertEqual(
                [event["event_type"] for event in replay_control_plane.events],
                ["run_cancelled"],
            )
            self.assertFalse(
                (artifact_dir / "terminal-result-outbox.json").exists()
            )
            self.assertFalse((root / "runtime" / "active-run.json").exists())
            self.assertIsNone(replay_worker.state.active_job)

    def test_non_authoritative_result_conflict_remains_blocked(self) -> None:
        class ConflictControlPlane(RecordingControlPlane):
            def result(self, job_id: str, payload: dict) -> None:
                super().result(job_id, payload)
                raise PullwiseHTTPError(
                    "checksum conflict",
                    409,
                    error_code="RESULT_CHECKSUM_CONFLICT",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_dir = root / "artifacts" / "run_1"
            worker = ReviewWorkerV1(worker_config(root), client=ConflictControlPlane())
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1")
            worker.state.set_active(active)
            envelope = {
                "protocol_version": "review-worker-protocol/v1",
                "execution": {"status": "completed"},
                "artifact_manifest": [],
            }

            submitted = worker.submit_result_or_record_failure(
                active,
                "job_1",
                {"status": "done", "attempt_id": "wk_1-1", "reviewWorkerProtocol": envelope},
                artifact_dir,
                envelope,
            )

            outbox = json.loads((artifact_dir / "terminal-result-outbox.json").read_text(encoding="utf-8"))
            self.assertFalse(submitted)
            self.assertFalse(outbox["retryable"])
            self.assertEqual(outbox["state"], "blocked")
            self.assertFalse((artifact_dir / "terminal-result-supersession.json").exists())
            self.assertEqual(list(artifact_dir.glob("terminal-result-outbox.superseded.*.json")), [])

    def test_invalid_artifact_only_outbox_blocks_slot_instead_of_claiming(self) -> None:
        invalid_records = {
            "invalid_json": "{",
            "wrong_schema": json.dumps({"schema_version": "terminal-result-outbox/v0"}),
            "checksum_mismatch": json.dumps(
                {
                    "schema_version": "terminal-result-outbox/v1",
                    "run_id": "run_1",
                    "job_id": "job_1",
                    "lease_id": "lease_1",
                    "attempt_id": "wk_1-1",
                    "result_status": "done",
                    "payload_sha256": "0" * 64,
                    "payload": {"status": "done"},
                }
            ),
        }
        for label, outbox_text in invalid_records.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                artifact_dir = root / "artifacts" / "run_1"
                artifact_dir.mkdir(parents=True)
                (artifact_dir / "terminal-result-outbox.json").write_text(outbox_text, encoding="utf-8")
                control_plane = RecordingControlPlane()
                worker = ReviewWorkerV1(worker_config(root), client=control_plane)
                disable_posix_lock(worker)
                worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
                worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

                with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                    worker.run(once=True)

                self.assertEqual(control_plane.calls, ["register", "heartbeat"])
                self.assertIsNotNone(worker.state.active_job)
                self.assertEqual(control_plane.heartbeats[0]["status"], "finishing")
                self.assertEqual(control_plane.heartbeats[0]["concurrency"]["active_jobs"], 1)
                self.assertEqual(control_plane.heartbeats[0]["concurrency"]["available_job_slots"], 0)
                self.assertTrue((artifact_dir / "terminal-result-outbox.json").is_file())

    def test_success_receipt_workspace_cleanup_waits_until_after_first_heartbeat(self) -> None:
        class OrderingControlPlane(RecordingControlPlane):
            def __init__(self, sentinel: Path) -> None:
                super().__init__()
                self.sentinel = sentinel

            def register(self) -> dict:
                self.assert_workspace_exists()
                return super().register()

            def heartbeat(self, **payload: dict) -> dict:
                self.assert_workspace_exists()
                return super().heartbeat(**payload)

            def claim(self) -> None:
                self.assert_workspace_exists()
                return super().claim()

            def assert_workspace_exists(self) -> None:
                if not self.sentinel.is_file():
                    raise AssertionError("workspace was removed before Worker registration/heartbeat")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspaces" / "run_1"
            sentinel = workspace / "repo" / "sentinel.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep until heartbeat", encoding="utf-8")
            artifact_dir = root / "artifacts" / "run_1"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "result-submit-succeeded.json").write_text(
                json.dumps({"run_id": "run_1", "status": "result_submit_succeeded"}),
                encoding="utf-8",
            )
            control_plane = OrderingControlPlane(sentinel)
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual(control_plane.calls, ["register", "heartbeat", "claim"])
            self.assertFalse(workspace.exists())

    def test_active_marker_and_uploaded_manifest_use_durable_file_and_directory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            worker = ReviewWorkerV1(worker_config(root), client=object())
            active = ActiveJob("job_1", "run_1", "lease_1", "wk_1-1")
            artifact_dir = root / "artifacts" / "run_1"
            run_dir = root / "workspaces" / "run_1" / "repo" / ".codex-review" / "runs" / "run_1"
            manifest = {"schema_version": "artifact-manifest/v1", "run_id": "run_1", "items": []}

            with patch(
                "pullwise_worker.review_worker_v1.os.fsync",
                wraps=os.fsync,
            ) as file_sync, patch(
                "pullwise_worker.review_worker_v1._fsync_directory",
                wraps=review_worker_v1_module._fsync_directory,
            ) as directory_sync:
                worker.persist_active_run_marker(active)
                write_uploaded_artifact_manifest(
                    artifact_dir,
                    manifest,
                    [],
                    source_run_dir=run_dir,
                )

            self.assertGreaterEqual(file_sync.call_count, 3)
            synced_directories = {Path(call.args[0]) for call in directory_sync.call_args_list}
            self.assertIn(worker.isolation.runtime, synced_directories)
            self.assertIn(artifact_dir, synced_directories)
            self.assertIn(run_dir, synced_directories)

    def test_restart_recovers_finishing_run_before_first_heartbeat_and_does_not_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "workspaces" / "run_1" / "repo" / ".codex-review" / "runs" / "run_1"
            artifact_dir = root / "artifacts" / "run_1"
            run_dir.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text(
                json.dumps(
                    {
                        "active_job": {
                            "job_id": "job_1",
                            "run_id": "run_1",
                            "lease_id": "lease_1",
                            "state": "finishing",
                            "current_phase": "submit_result_envelope",
                        },
                        "progress": {
                            "run_id": "run_1",
                            "overall_percent": 100,
                            "current_phase": "submit_result_envelope",
                            "current_phase_status": "failed",
                            "current_phase_percent": 100,
                            "last_event_sequence": 41,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (artifact_dir / "result-submit-failed.json").write_text(
                json.dumps(
                    {
                        "run_id": "run_1",
                        "job_id": "job_1",
                        "lease_id": "lease_1",
                        "attempt_id": "wk_1-1",
                        "status": "result_submit_failed",
                    }
                ),
                encoding="utf-8",
            )
            control_plane = RecordingControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual(control_plane.calls, ["register", "heartbeat"])
            heartbeat = control_plane.heartbeats[0]
            self.assertEqual(heartbeat["status"], "finishing")
            self.assertEqual(heartbeat["active_run_id"], "run_1")
            self.assertEqual(heartbeat["concurrency"]["active_jobs"], 1)
            self.assertEqual(heartbeat["concurrency"]["available_job_slots"], 0)
            self.assertTrue(run_dir.parent.parent.parent.parent.exists())

    def test_restart_recovers_active_slot_persisted_before_workspace_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_dir = root / "runtime"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "active-run.json").write_text(
                json.dumps(
                    {
                        "job_id": "job_checkout",
                        "run_id": "run_checkout",
                        "lease_id": "lease_checkout",
                        "attempt_id": "wk_1-1",
                        "state": "leased",
                        "current_phase": "prepare_workspace",
                    }
                ),
                encoding="utf-8",
            )
            control_plane = RecordingControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual(control_plane.calls, ["register", "heartbeat"])
            self.assertEqual(control_plane.heartbeats[0]["status"], "finishing")
            self.assertEqual(control_plane.heartbeats[0]["active_run_id"], "run_checkout")

    def test_unauthenticated_quota_probe_is_not_ready_and_cannot_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_plane = RecordingControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient(  # type: ignore[assignment]
                quota_error="account authentication required to read rate limits"
            )

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual(control_plane.calls, ["register", "heartbeat"])
            heartbeat = control_plane.heartbeats[0]
            self.assertFalse(heartbeat["codex_ready"])
            self.assertEqual(heartbeat["doctor_status"], "degraded")
            self.assertEqual(heartbeat["ready_providers"], [])
            self.assertEqual(heartbeat["codex_quota"]["status"], "unavailable")
            self.assertFalse(heartbeat["codex_quota"]["ready"])

    def test_checkout_and_mirror_cleanup_remove_real_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name, remover in (
                ("checkout", lifecycle.cleanup_checkout_path),
                ("mirror", lifecycle.cleanup_repository_mirror_cache_path),
            ):
                target = root / name
                target.mkdir()
                (target / "nested.txt").write_text("source", encoding="utf-8")

                self.assertTrue(remover(target))
                self.assertFalse(target.exists())

    def test_idle_loop_periodically_removes_orphaned_v1_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspaces" / "orphaned_run"
            repo_file = workspace / "repo" / "src" / "secret.py"
            repo_file.parent.mkdir(parents=True)
            repo_file.write_text("TOKEN = 'repository source'\n", encoding="utf-8")
            control_plane = RecordingControlPlane()
            worker = ReviewWorkerV1(worker_config(root), client=control_plane)
            disable_posix_lock(worker)
            worker.codex_client = FakeCodexClient()  # type: ignore[assignment]
            worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                worker.run(once=True)

            self.assertEqual(control_plane.calls, ["register", "heartbeat", "claim"])
            self.assertFalse(workspace.exists())

    def test_uninstall_ownership_rejects_unrelated_directory_with_worker_basename(self) -> None:
        config = SimpleNamespace(
            worker_id="wk_test",
            service_home="/var/lib/pullwise-worker/wk_test",
            worker_root="/var/lib/pullwise-worker/wk_test/workers/wk_test",
        )

        self.assertFalse(lifecycle.worker_instance_owned_path(Path("/unrelated/wk_test"), config))
        misconfigured = SimpleNamespace(
            worker_id="wk_test",
            service_home="/var/lib/pullwise-worker/wk_test",
            worker_root="/unrelated/wk_test",
        )
        self.assertFalse(
            lifecycle.worker_instance_owned_path(
                Path("/unrelated/wk_test"),
                misconfigured,
            )
        )
        self.assertFalse(
            lifecycle.worker_instance_owned_path(
                Path("/var/log/pullwise-worker/unrelated/wk_test"),
                config,
            )
        )
        self.assertTrue(
            lifecycle.worker_instance_owned_path(
                Path("/var/lib/pullwise-worker/wk_test/checkouts"),
                config,
            )
        )
        self.assertTrue(
            lifecycle.worker_instance_owned_path(
                Path("/var/lib/pullwise-worker/wk_test/workers/wk_test"),
                config,
            )
        )
        self.assertTrue(
            lifecycle.worker_instance_owned_path(
                Path("/var/log/pullwise-worker/wk_test"),
                config,
            )
        )
        self.assertTrue(
            lifecycle.worker_instance_owned_path(
                Path("/etc/pullwise-worker/wk_test"),
                config,
            )
        )


if __name__ == "__main__":
    unittest.main()
