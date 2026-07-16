from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pullwise_worker._main_part_01_bootstrap import (
    PullwiseHTTPError,
    PullwiseRequestError,
    pullwise_http_error,
)
from pullwise_worker.review_worker_v1 import ReviewWorkerV1, control_plane_error_is_retryable


class StopLoop(BaseException):
    pass


class RunningCodexClient:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_error = close_error
        self.closed = False

    def is_running(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def worker_config(
    root: Path,
    *,
    poll_seconds: int = 1,
    jitter_seconds: float = 0,
    max_backoff_seconds: int = 4,
) -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="wk_control_plane",
        service_home=str(root),
        worker_root=str(root),
        poll_seconds=poll_seconds,
        poll_jitter_seconds=jitter_seconds,
        max_backoff_seconds=max_backoff_seconds,
        cleanup_interval_seconds=3600,
    )


def prepare_worker(
    root: Path,
    client: object,
    *,
    config: SimpleNamespace | None = None,
) -> tuple[ReviewWorkerV1, list[str]]:
    worker = ReviewWorkerV1(config or worker_config(root), client=client)
    releases: list[str] = []
    worker.lock.acquire = lambda: None  # type: ignore[method-assign]
    worker.lock.release = lambda: releases.append("released")  # type: ignore[method-assign]
    worker.codex_client = RunningCodexClient()  # type: ignore[assignment]
    worker.machine_metrics_payload = lambda: None  # type: ignore[method-assign]
    worker.quota_monitor.snapshot_if_due = lambda active=False: {"ready": True}  # type: ignore[method-assign]
    worker.cleanup_idle_v1_workspaces_if_due = lambda force=False: []  # type: ignore[method-assign]
    return worker, releases


class ControlPlaneLoopRegressionTests(unittest.TestCase):
    def test_http_error_preserves_machine_code_and_cancellation_bindings(self) -> None:
        response_payload = {
            "error": "A cancellation-state job only accepts a cancelled result.",
            "code": "JOB_CANCELLATION_AUTHORITATIVE",
            "jobStatus": "cancel_requested",
            "jobId": "job_1",
            "runId": "run_1",
            "attemptId": "wk_1-1",
        }
        raw_error = urllib.error.HTTPError(
            "https://pullwise.test/v1/review-runs/run_1/result",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps(response_payload).encode("utf-8")),
        )

        error = pullwise_http_error(raw_error)

        self.assertEqual(error.status_code, 409)
        self.assertEqual(
            error.error_code,
            "JOB_CANCELLATION_AUTHORITATIVE",
        )
        self.assertEqual(error.response_payload, response_payload)
        self.assertIn("cancellation-state job", str(error))

    def test_only_retryable_control_plane_errors_are_retried(self) -> None:
        self.assertTrue(control_plane_error_is_retryable(PullwiseRequestError("transport")))
        for status_code in (408, 429, 500, 503, 599):
            with self.subTest(status_code=status_code):
                self.assertTrue(
                    control_plane_error_is_retryable(
                        PullwiseHTTPError("temporary", status_code)
                    )
                )
        for status_code in (400, 401, 403, 404, 409, 600):
            with self.subTest(status_code=status_code):
                self.assertFalse(
                    control_plane_error_is_retryable(
                        PullwiseHTTPError("permanent", status_code)
                    )
                )
        self.assertFalse(control_plane_error_is_retryable(RuntimeError("programming error")))

    def test_continuous_loop_retries_transient_register_heartbeat_and_claim_in_safe_order(self) -> None:
        calls: list[str] = []

        class Client:
            register_count = 0
            heartbeat_count = 0
            claim_count = 0

            def register(self) -> dict:
                self.register_count += 1
                calls.append("register")
                if self.register_count == 1:
                    raise PullwiseHTTPError("register unavailable", 503)
                return {}

            def heartbeat(self, **_payload: object) -> dict:
                self.heartbeat_count += 1
                calls.append("heartbeat")
                if self.heartbeat_count == 1:
                    raise PullwiseRequestError("heartbeat unavailable")
                return {}

            def claim(self) -> None:
                self.claim_count += 1
                calls.append("claim")
                if self.claim_count == 1:
                    raise PullwiseRequestError("claim unavailable")
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker, releases = prepare_worker(Path(tmp_dir), Client())
            sleeps: list[float] = []

            def stop_after_four_sleeps(seconds: float) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 4:
                    raise StopLoop

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.time.sleep",
                side_effect=stop_after_four_sleeps,
            ):
                with self.assertRaises(StopLoop):
                    worker.run()

        self.assertEqual(
            calls,
            ["register", "register", "heartbeat", "heartbeat", "claim", "heartbeat", "claim"],
        )
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 1.0])
        self.assertEqual(releases, ["released"])

    def test_empty_queue_uses_bounded_exponential_backoff(self) -> None:
        calls: list[str] = []

        class Client:
            def register(self) -> dict:
                calls.append("register")
                return {}

            def heartbeat(self, **_payload: object) -> dict:
                calls.append("heartbeat")
                return {}

            def claim(self) -> None:
                calls.append("claim")
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker, _releases = prepare_worker(Path(tmp_dir), Client())
            sleeps: list[float] = []

            def stop_after_four_sleeps(seconds: float) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 4:
                    raise StopLoop

            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.time.sleep",
                side_effect=stop_after_four_sleeps,
            ):
                with self.assertRaises(StopLoop):
                    worker.run()

        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 4.0])
        self.assertEqual(calls.count("heartbeat"), 4)
        self.assertEqual(calls.count("claim"), 4)

    def test_backoff_jitter_remains_within_configured_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = worker_config(root, poll_seconds=2, jitter_seconds=1, max_backoff_seconds=5)
            worker, _releases = prepare_worker(root, object(), config=config)

            with patch("pullwise_worker.review_worker_v1.random.uniform", return_value=1.0) as uniform:
                delays = [
                    worker.next_poll_sleep(claimed_job=False, loop_error=False),
                    worker.next_poll_sleep(claimed_job=False, loop_error=False),
                    worker.next_poll_sleep(claimed_job=False, loop_error=False),
                ]

        self.assertEqual(delays, [3.0, 5.0, 5.0])
        self.assertTrue(all(delay <= 5 for delay in delays))
        self.assertEqual(uniform.call_count, 3)
        uniform.assert_called_with(0.0, 1.0)

    def test_once_does_not_retry_or_sleep_after_control_plane_failure(self) -> None:
        for failing_stage, expected_calls in (
            ("register", ["register"]),
            ("heartbeat", ["register", "heartbeat"]),
            ("claim", ["register", "heartbeat", "claim"]),
        ):
            with self.subTest(failing_stage=failing_stage), tempfile.TemporaryDirectory() as tmp_dir:
                calls: list[str] = []

                class Client:
                    def register(self) -> dict:
                        calls.append("register")
                        if failing_stage == "register":
                            raise PullwiseRequestError("register unavailable")
                        return {}

                    def heartbeat(self, **_payload: object) -> dict:
                        calls.append("heartbeat")
                        if failing_stage == "heartbeat":
                            raise PullwiseRequestError("heartbeat unavailable")
                        return {}

                    def claim(self) -> None:
                        calls.append("claim")
                        if failing_stage == "claim":
                            raise PullwiseRequestError("claim unavailable")
                        return None

                worker, releases = prepare_worker(Path(tmp_dir), Client())
                with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                    "pullwise_worker.review_worker_v1.time.sleep"
                ) as sleep:
                    with self.assertRaisesRegex(PullwiseRequestError, "unavailable"):
                        worker.run(once=True)

                self.assertEqual(calls, expected_calls)
                sleep.assert_not_called()
                self.assertEqual(releases, ["released"])

    def test_local_recovery_runs_before_control_plane_registration(self) -> None:
        calls: list[str] = []

        class Client:
            def register(self) -> dict:
                calls.append("register")
                raise PullwiseRequestError("register unavailable")

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker, releases = prepare_worker(Path(tmp_dir), Client())

            def recover() -> None:
                calls.append("recover")
                return None

            worker.recover_persisted_active_job = recover  # type: ignore[method-assign]
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                with self.assertRaisesRegex(PullwiseRequestError, "register unavailable"):
                    worker.run(once=True)

        self.assertEqual(calls, ["recover", "register"])
        self.assertEqual(releases, ["released"])

    def test_continuous_loop_does_not_retry_nonretryable_http_error(self) -> None:
        calls: list[str] = []

        class Client:
            def register(self) -> dict:
                calls.append("register")
                raise PullwiseHTTPError("unauthorized", 401)

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker, releases = prepare_worker(Path(tmp_dir), Client())
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"), patch(
                "pullwise_worker.review_worker_v1.time.sleep"
            ) as sleep:
                with self.assertRaisesRegex(PullwiseHTTPError, "unauthorized"):
                    worker.run()

        self.assertEqual(calls, ["register"])
        sleep.assert_not_called()
        self.assertEqual(releases, ["released"])

    def test_lock_is_released_when_codex_close_raises(self) -> None:
        class Client:
            def register(self) -> dict:
                return {}

            def heartbeat(self, **_payload: object) -> dict:
                return {}

            def claim(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker, releases = prepare_worker(Path(tmp_dir), Client())
            worker.codex_client = RunningCodexClient(  # type: ignore[assignment]
                close_error=RuntimeError("close failed")
            )
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    worker.run(once=True)

        self.assertEqual(releases, ["released"])

    def test_codex_close_failure_does_not_mask_control_plane_failure(self) -> None:
        class Client:
            def register(self) -> dict:
                return {}

            def heartbeat(self, **_payload: object) -> dict:
                raise RuntimeError("heartbeat failed")

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker, releases = prepare_worker(Path(tmp_dir), Client())
            worker.codex_client = RunningCodexClient(  # type: ignore[assignment]
                close_error=RuntimeError("close failed")
            )
            with patch("pullwise_worker.review_worker_v1.sys.platform", "linux"):
                with self.assertRaisesRegex(RuntimeError, "heartbeat failed"):
                    worker.run(once=True)

        self.assertEqual(releases, ["released"])


if __name__ == "__main__":
    unittest.main()
