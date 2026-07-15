from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pullwise_worker.review_worker_v1 as review_worker_v1
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

    def test_release_thread_archives_app_server_thread_before_dropping_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            calls: list[tuple[str, dict[str, object]]] = []

            class Client:
                def _request_raw(
                    self,
                    method: str,
                    params: dict[str, object],
                ) -> dict[str, object]:
                    calls.append((method, params))
                    return {"thread": {"id": params.get("threadId")}}

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()
            server._runtime_resources.register_thread("thread-1", object())

            server.release_thread("thread-1")

        self.assertEqual(calls, [("thread/archive", {"threadId": "thread-1"})])
        self.assertEqual(server._threads, {})

    def test_raw_request_timeout_marks_runtime_unhealthy_and_blocks_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            entered = threading.Event()
            release = threading.Event()
            late_request_finished = threading.Event()

            class Client:
                def __init__(self) -> None:
                    self.call_count = 0

                def _request_raw(
                    self,
                    _method: str,
                    _params: dict[str, object],
                ) -> dict[str, object]:
                    self.call_count += 1
                    call_number = self.call_count
                    if call_number == 1:
                        entered.set()
                        release.wait(5)
                        late_request_finished.set()
                    return {"call_number": call_number}

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()
            server._codex = SimpleNamespace(account=lambda **_kwargs: {"type": "chatgpt"})
            self.assertTrue(server.is_running())
            outcome: list[BaseException] = []
            returned: list[dict[str, object]] = []

            def request() -> None:
                try:
                    returned.append(server.request("test/block", timeout_seconds=0.1))
                except BaseException as exc:  # noqa: BLE001 - asserted below.
                    outcome.append(exc)

            runner = threading.Thread(target=request, daemon=True)
            started_at = time.monotonic()
            runner.start()
            try:
                self.assertTrue(entered.wait(0.5), "raw request was never invoked")
                runner.join(0.5)
                self.assertFalse(runner.is_alive(), "raw request exceeded its caller-visible timeout")
                self.assertEqual(returned, [])
                self.assertEqual(len(outcome), 1)
                self.assertIsInstance(outcome[0], TimeoutError)
                self.assertEqual(str(outcome[0]), "Codex raw request timed out: test/block")
                self.assertLess(time.monotonic() - started_at, 0.6)
                with self.assertRaisesRegex(RuntimeError, "unhealthy.*test/block"):
                    server.request("test/next", timeout_seconds=1)
                with self.assertRaisesRegex(RuntimeError, "unhealthy.*test/block"):
                    server.account()
                self.assertEqual(server._client.call_count, 1)
                self.assertFalse(server.is_running())
            finally:
                release.set()
                runner.join(1)

            self.assertTrue(late_request_finished.wait(1), "timed-out raw request never settled")
            self.assertEqual(returned, [])

    def test_raw_request_preserves_sdk_exception_without_poisoning_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            failure = LookupError("raw rpc failed")

            class Client:
                def _request_raw(self, _method: str, _params: dict[str, object]) -> dict[str, object]:
                    raise failure

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            client = Client()
            server._client = client

            with self.assertRaises(LookupError) as raised:
                server.request("test/failure", timeout_seconds=1)

            self.assertIs(raised.exception, failure)
            self.assertIs(server._client, client)

    def test_fallback_archive_uses_one_bounded_rpc_and_timeout_invalidates_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            entered = threading.Event()
            release = threading.Event()
            calls: list[tuple[str, dict[str, object]]] = []

            class Client:
                def _request_raw(self, method: str, params: dict[str, object]) -> dict[str, object]:
                    calls.append((method, params))
                    entered.set()
                    release.wait(5)
                    return {}

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()
            server._runtime_resources.register_thread("thread-1", object())

            try:
                with patch.object(review_worker_v1, "CODEX_THREAD_ARCHIVE_TIMEOUT_SECONDS", 0.05), patch.object(
                    review_worker_v1,
                    "run_bounded_call",
                    wraps=review_worker_v1.run_bounded_call,
                ) as bounded_call:
                    with self.assertRaisesRegex(TimeoutError, "thread/archive"):
                        server.release_thread("thread-1")

                self.assertTrue(entered.is_set())
                self.assertEqual(calls, [("thread/archive", {"threadId": "thread-1"})])
                self.assertEqual(bounded_call.call_count, 1)
                self.assertEqual(server._threads, {})
                with self.assertRaisesRegex(RuntimeError, "unhealthy.*thread/archive"):
                    server.request("test/next", timeout_seconds=1)
            finally:
                release.set()

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

    def test_turn_completion_rejects_non_completed_terminal_statuses(self) -> None:
        for status in ("failed", "interrupted", "cancelled"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp_dir:
                workspace = Path(tmp_dir)

                class Client:
                    def turn_start(
                        self,
                        _thread_id: str,
                        _items: list,
                        params: dict | None = None,
                    ) -> SimpleNamespace:
                        del params
                        return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                    def next_turn_notification(self, turn_id: str) -> SimpleNamespace:
                        return SimpleNamespace(
                            method="turn/completed",
                            payload=SimpleNamespace(
                                turn=SimpleNamespace(id=turn_id, status=status, error=None),
                            ),
                        )

                    def unregister_turn_notifications(self, _turn_id: str) -> None:
                        return None

                server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
                server._client = Client()

                with self.assertRaisesRegex(RuntimeError, status):
                    server.run_turn(
                        thread_id="thread-1",
                        repo_dir=workspace,
                        prompt="review",
                        effort="medium",
                        read_only=True,
                        timeout_seconds=2,
                    )

    def test_turn_completion_without_turn_id_is_a_protocol_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            class Client:
                def turn_start(
                    self,
                    _thread_id: str,
                    _items: list,
                    params: dict | None = None,
                ) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, _turn_id: str) -> SimpleNamespace:
                    return SimpleNamespace(
                        method="turn/completed",
                        payload=SimpleNamespace(
                            turn=SimpleNamespace(id="", status="completed", error=None),
                        ),
                    )

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    return None

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

    def test_turn_completion_reads_status_and_error_from_dict_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            class Client:
                def turn_start(
                    self,
                    _thread_id: str,
                    _items: list,
                    params: dict | None = None,
                ) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, turn_id: str) -> SimpleNamespace:
                    return SimpleNamespace(
                        method="turn/completed",
                        payload={
                            "turn": {
                                "id": turn_id,
                                "status": "failed",
                                "error": {
                                    "message": "quota exhausted",
                                    "codexErrorInfo": "usageLimitExceeded",
                                },
                            },
                        },
                    )

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    return None

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()

            with self.assertRaisesRegex(RuntimeError, "failed.*usageLimitExceeded"):
                server.run_turn(
                    thread_id="thread-1",
                    repo_dir=workspace,
                    prompt="review",
                    effort="medium",
                    read_only=True,
                    timeout_seconds=2,
                )

    def test_turn_completion_accepts_matching_completed_dict_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            class Client:
                def turn_start(
                    self,
                    _thread_id: str,
                    _items: list,
                    params: dict | None = None,
                ) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, turn_id: str) -> SimpleNamespace:
                    return SimpleNamespace(
                        method="turn/completed",
                        payload={
                            "turn": {
                                "id": turn_id,
                                "status": "completed",
                                "error": None,
                                "durationMs": 321,
                            },
                        },
                    )

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    return None

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()

            metrics = server.run_turn(
                thread_id="thread-1",
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=2,
            )

        self.assertEqual(metrics.duration_ms, 321)

    def test_turn_completion_ignores_completed_notification_for_another_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            requested_turns: list[str] = []

            class Client:
                def turn_start(
                    self,
                    _thread_id: str,
                    _items: list,
                    params: dict | None = None,
                ) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, turn_id: str) -> SimpleNamespace:
                    requested_turns.append(turn_id)
                    reported_turn_id = "turn-other" if len(requested_turns) == 1 else turn_id
                    return SimpleNamespace(
                        method="turn/completed",
                        payload={
                            "turn": {
                                "id": reported_turn_id,
                                "status": "completed",
                                "error": None,
                            },
                        },
                    )

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    return None

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()

            server.run_turn(
                thread_id="thread-1",
                repo_dir=workspace,
                prompt="review",
                effort="medium",
                read_only=True,
                timeout_seconds=2,
            )

        self.assertEqual(requested_turns, ["turn-1", "turn-1"])

    def test_turn_notification_stream_preserves_empty_eof_error_with_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            class Client:
                def turn_start(
                    self,
                    _thread_id: str,
                    _items: list,
                    params: dict | None = None,
                ) -> SimpleNamespace:
                    del params
                    return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

                def next_turn_notification(self, _turn_id: str) -> SimpleNamespace:
                    raise EOFError()

                def unregister_turn_notifications(self, _turn_id: str) -> None:
                    return None

            server = CodexSdkClient("", {}, workspace, workspace / "events.jsonl")
            server._client = Client()

            with self.assertRaises(EOFError) as raised:
                server.run_turn(
                    thread_id="thread-1",
                    repo_dir=workspace,
                    prompt="review",
                    effort="medium",
                    read_only=True,
                    timeout_seconds=2,
                )

        self.assertIn("EOFError", str(raised.exception))
        self.assertIn("notification", str(raised.exception).lower())

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
                        payload=SimpleNamespace(
                            turn=SimpleNamespace(id=turn_id, status="completed", error=None),
                        ),
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
