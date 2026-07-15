from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker import _main_part_08_lifecycle_cleanup as lifecycle
from pullwise_worker.review_worker_v1 import ReviewWorkerV1


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
