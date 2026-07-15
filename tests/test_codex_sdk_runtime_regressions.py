from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pullwise_worker.review_worker_v1 import CodexSdkClient, JobCancelled


class CodexSdkRuntimeRegressionTests(unittest.TestCase):
    def _thread_server(self, workspace: Path, thread_start: object) -> CodexSdkClient:
        server = CodexSdkClient("", {}, workspace, workspace / "events-run-1.jsonl")
        server._codex = SimpleNamespace(thread_start=thread_start)
        server._runtime = SimpleNamespace(
            ApprovalMode=SimpleNamespace(deny_all="deny_all"),
            Sandbox=SimpleNamespace(workspace_write="workspace_write"),
        )
        return server

    def test_thread_start_is_bounded_by_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            entered = threading.Event()
            release = threading.Event()

            def blocking_thread_start(**_kwargs: object) -> SimpleNamespace:
                entered.set()
                release.wait(5)
                return SimpleNamespace(id="thread-late")

            server = self._thread_server(workspace, blocking_thread_start)
            outcome: list[BaseException] = []

            def start() -> None:
                try:
                    server.start_thread(workspace, "gpt-5.5", timeout_seconds=1)
                except BaseException as exc:  # noqa: BLE001 - asserted below.
                    outcome.append(exc)

            runner = threading.Thread(target=start, daemon=True)
            started_at = time.monotonic()
            runner.start()
            try:
                self.assertTrue(entered.wait(1), "thread_start was never invoked")
                runner.join(1.5)
                self.assertFalse(runner.is_alive(), "thread_start exceeded its caller-visible timeout")
                self.assertIsInstance(outcome[0], TimeoutError)
                self.assertLess(time.monotonic() - started_at, 2.0)
            finally:
                release.set()
                runner.join(2)

    def test_thread_start_honors_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            entered = threading.Event()
            release = threading.Event()
            cancelled = threading.Event()

            def blocking_thread_start(**_kwargs: object) -> SimpleNamespace:
                entered.set()
                release.wait(5)
                return SimpleNamespace(id="thread-late")

            server = self._thread_server(workspace, blocking_thread_start)
            outcome: list[BaseException] = []

            def start() -> None:
                try:
                    server.start_thread(
                        workspace,
                        "gpt-5.5",
                        timeout_seconds=30,
                        cancel_requested=cancelled.is_set,
                    )
                except BaseException as exc:  # noqa: BLE001 - asserted below.
                    outcome.append(exc)

            runner = threading.Thread(target=start, daemon=True)
            runner.start()
            try:
                self.assertTrue(entered.wait(1), "thread_start was never invoked")
                cancelled.set()
                runner.join(1.5)
                self.assertFalse(runner.is_alive(), "thread_start ignored cancellation")
                self.assertIsInstance(outcome[0], JobCancelled)
            finally:
                release.set()
                runner.join(2)

    def test_turn_start_without_turn_id_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            class Client:
                def turn_start(self, _thread_id: str, _items: list, params: dict | None = None) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id=""), turn_id="")

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()

            with self.assertRaisesRegex(RuntimeError, "turn id"):
                server.run_turn(
                    thread_id="thread-1",
                    repo_dir=workspace,
                    prompt="review",
                    effort="medium",
                    read_only=True,
                    timeout_seconds=2,
                )

    def test_abandoned_turn_consumer_cannot_write_to_later_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            first_events = workspace / "events-run-1.jsonl"
            later_events = workspace / "events-run-2.jsonl"
            record_entered = threading.Event()
            release_record = threading.Event()
            consumer_finished = threading.Event()

            class Client:
                def turn_start(self, _thread_id: str, _items: list, params: dict | None = None) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, turn_id: str) -> SimpleNamespace:
                    return SimpleNamespace(
                        method="turn/completed",
                        payload=SimpleNamespace(turn=SimpleNamespace(id=turn_id, error=None)),
                    )

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    consumer_finished.set()

                def turn_interrupt(self, _thread_id: str, _turn_id: str) -> None:
                    return

            server = CodexSdkClient("", {}, workspace, first_events)
            server._client = Client()
            original_record = server._record_sdk_notification

            def delayed_record(*args: object, **kwargs: object) -> None:
                record_entered.set()
                release_record.wait(3)
                original_record(*args, **kwargs)

            server._record_sdk_notification = delayed_record  # type: ignore[method-assign]
            outcome: list[BaseException] = []
            runner = threading.Thread(
                target=lambda: self._capture_turn_outcome(server, workspace, outcome),
                daemon=True,
            )
            runner.start()
            try:
                self.assertTrue(record_entered.wait(1), "consumer never reached event persistence")
                runner.join(1.5)
                self.assertFalse(runner.is_alive(), "turn timeout did not return to its caller")
                self.assertIsInstance(outcome[0], TimeoutError)
                server.set_events_path(later_events)
            finally:
                release_record.set()
            self.assertTrue(consumer_finished.wait(1), "abandoned notification consumer did not exit")
            self.assertFalse(first_events.exists(), "abandoned consumer wrote after run_turn returned")
            self.assertFalse(later_events.exists(), "abandoned consumer contaminated the next run")

    def _capture_turn_outcome(
        self,
        server: CodexSdkClient,
        workspace: Path,
        outcome: list[BaseException],
    ) -> None:
        try:
            server.run_turn(
                thread_id="thread-1",
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=1,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted by the caller.
            outcome.append(exc)

    def test_switching_run_event_sink_releases_tracked_thread_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            next_id = 0

            def thread_start(**_kwargs: object) -> SimpleNamespace:
                nonlocal next_id
                next_id += 1
                return SimpleNamespace(id=f"thread-{next_id}")

            server = self._thread_server(workspace, thread_start)
            server.start_thread(workspace, "gpt-5.5")
            server.start_thread(workspace, "gpt-5.5")
            self.assertEqual(len(server._threads), 2)

            server.set_events_path(workspace / "events-run-2.jsonl")

        self.assertEqual(server._threads, {})


if __name__ == "__main__":
    unittest.main()
