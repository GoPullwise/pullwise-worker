from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pullwise_worker.agent_kernel_review_worker import (
    AgentKernelShadowReviewWorker,
    build_review_worker,
)
from pullwise_worker.agent_kernel_supervisor import (
    LegacySlotMirror,
    SupervisorProjectionError,
    project_legacy_slot,
)
from pullwise_worker.review_worker_v1 import ActiveJob


def _marker(**changes: object) -> dict[str, object]:
    marker = {
        "job_id": "job-1",
        "run_id": "run-1",
        "lease_id": "lease-1",
        "attempt_id": "attempt-1",
        "state": "busy",
        "terminal_result_prepared": False,
    }
    marker.update(changes)
    return marker


def _outbox(**changes: object) -> dict[str, object]:
    outbox = {
        "schema_version": "terminal-result-outbox/v1",
        "job_id": "job-1",
        "run_id": "run-1",
        "lease_id": "lease-1",
        "attempt_id": "attempt-1",
        "state": "ready",
        "result_status": "done",
    }
    outbox.update(changes)
    return outbox


class AgentKernelSupervisorProjectionTest(unittest.TestCase):
    def test_idle_and_active_projections_preserve_one_slot_and_zero_queue(self) -> None:
        idle = project_legacy_slot(None, None)
        self.assertEqual(("IDLE", 1, 0, 0), (
            idle.slot_state,
            idle.available_job_slots,
            idle.active_jobs,
            idle.local_queue_depth,
        ))

        active = project_legacy_slot(_marker(), None)
        self.assertEqual(("ACTIVE", "ACTIVE", "RUN"), (
            active.slot_state,
            active.task_lifecycle,
            active.desired_state,
        ))
        self.assertEqual((0, 1, 0), (
            active.available_job_slots,
            active.active_jobs,
            active.local_queue_depth,
        ))
        self.assertFalse(active.maintains_local_queue)

    def test_cancelling_and_terminal_outbox_project_without_new_authority(self) -> None:
        cancelling = project_legacy_slot(_marker(state="cancelling"), None)
        self.assertEqual(("ACTIVE", "CANCEL"), (
            cancelling.task_lifecycle,
            cancelling.desired_state,
        ))

        publishing = project_legacy_slot(
            _marker(state="finishing", terminal_result_prepared=True), _outbox()
        )
        self.assertEqual("FINALIZING", publishing.task_lifecycle)
        self.assertEqual("ready", publishing.terminal_outbox_state)
        self.assertEqual("legacy_v1", publishing.terminal_authority)

        cancelled = project_legacy_slot(
            _marker(state="finishing", terminal_result_prepared=True),
            _outbox(result_status="cancelled"),
        )
        self.assertEqual("CANCEL", cancelled.desired_state)

    def test_outbox_identity_or_second_active_binding_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            SupervisorProjectionError, "TRANSPORT_IDENTITY_MISMATCH"
        ):
            project_legacy_slot(_marker(), _outbox(run_id="run-other"))

        mirror = LegacySlotMirror()
        mirror.observe(_marker(), None)
        with self.assertRaisesRegex(
            SupervisorProjectionError, "STATE_TRANSITION_INVALID"
        ):
            mirror.observe(_marker(run_id="run-2", job_id="job-2"), None)
        mirror.observe(None, None)
        mirror.observe(_marker(run_id="run-2", job_id="job-2"), None)
        self.assertEqual("run-2", mirror.snapshot().run_id)

    def test_runtime_factory_is_rollbackable_and_shadow_is_not_a_second_queue(self) -> None:
        class LegacyWorker:
            def __init__(self, config: object, client: object) -> None:
                self.config = config
                self.client = client

        config = SimpleNamespace(
            worker_id="wk-test",
            service_home="/tmp",
            agent_kernel_shadow_enabled=False,
        )
        legacy = build_review_worker(config, client=object(), legacy_class=LegacyWorker)
        self.assertIsInstance(legacy, LegacyWorker)

        enabled = SimpleNamespace(
            worker_id="wk-test",
            service_home="/tmp",
            agent_kernel_shadow_enabled=True,
        )
        worker = build_review_worker(enabled, client=object())
        self.assertIsInstance(worker, AgentKernelShadowReviewWorker)
        projection = worker.agent_kernel_slot_snapshot()
        self.assertEqual((0, False), (
            projection.local_queue_depth,
            projection.maintains_local_queue,
        ))

    def test_enabled_runtime_mirrors_the_existing_marker_and_outbox_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-kernel-supervisor-") as tmp:
            root = Path(tmp) / "worker"
            worker = AgentKernelShadowReviewWorker(
                SimpleNamespace(
                    worker_id="wk-test",
                    worker_root=root,
                    service_home=str(Path(tmp)),
                ),
                client=object(),
            )
            active = ActiveJob(
                job_id="job-1",
                run_id="run-1",
                lease_id="lease-1",
                attempt_id="attempt-1",
            )
            worker.persist_active_run_marker(active)
            self.assertEqual("ACTIVE", worker.agent_kernel_slot_snapshot().task_lifecycle)

            artifact_dir = worker.isolation.artifacts / active.run_id
            artifact_dir.mkdir(parents=True)
            worker.prepare_terminal_result_outbox(
                active,
                {"status": "done"},
                artifact_dir,
                {"execution": {"status": "completed"}},
            )
            active.state = "finishing"
            active.terminal_result_prepared = True
            worker.persist_active_run_marker(active)
            projected = worker.agent_kernel_slot_snapshot()
            self.assertEqual(("FINALIZING", "ready", None), (
                projected.task_lifecycle,
                projected.terminal_outbox_state,
                worker.agent_kernel_shadow_error,
            ))

            worker.clear_active_run_marker(active)
            self.assertEqual(
                "FINALIZING", worker.agent_kernel_slot_snapshot().task_lifecycle
            )
            self.assertIn("TRANSPORT_IDENTITY_MISMATCH", str(
                worker.agent_kernel_shadow_error
            ))

            worker.terminal_result_outbox_path(active.run_id).unlink()
            worker.clear_active_run_marker(active)
            self.assertEqual("IDLE", worker.agent_kernel_slot_snapshot().slot_state)


if __name__ == "__main__":
    unittest.main()
