from __future__ import annotations

import concurrent.futures
import gzip
import io
import json
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codereview.utils.process import ProcessCancelled, ProcessResult, clear_process_cancel_event, process_cancel_requested, run_process, set_process_cancel_event

worker_main = importlib.import_module("pullwise_worker.main")


def config_for(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        service_home=str(tmp / "home"),
        worker_token="secret-token",
        codex_command="codex",
        codex_model="gpt-5",
        codex_reasoning_effort="high",
        codex_doctor_timeout_seconds=60,
    )


class GraphVerifiedWorkerTest(unittest.TestCase):
    def test_package_module_entrypoint_shows_cli_help(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "pullwise_worker", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Run the Pullwise pull worker.", completed.stdout)

    def test_worker_config_tolerates_bad_numeric_environment_values(self) -> None:
        env = {
            "PULLWISE_WORKER_POLL_SECONDS": "bad",
            "PULLWISE_WORKER_POLL_JITTER_SECONDS": "bad",
            "PULLWISE_WORKER_MAX_BACKOFF_SECONDS": "bad",
            "PULLWISE_WATCHER_POLL_SECONDS": "bad",
            "PULLWISE_CODEX_TIMEOUT_SECONDS": "bad",
            "PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS": "bad",
            "PULLWISE_CODEX_AUTH_FAILURE_COOLDOWN_SECONDS": "bad",
            "PULLWISE_READINESS_CHECK_SECONDS": "bad",
            "PULLWISE_RESULT_UPLOAD_ATTEMPTS": "bad",
            "PULLWISE_RESULT_UPLOAD_COMPRESS_MIN_BYTES": "bad",
            "PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS": "bad",
            "PULLWISE_MAX_CHECKOUT_BYTES": "bad",
            "PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS": "bad",
            "PULLWISE_LOG_RETENTION_SECONDS": "bad",
            "PULLWISE_MAX_LOG_BYTES": "bad",
            "PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES": "bad",
        }
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_test",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )

        with patch.dict(worker_main.os.environ, env, clear=False):
            config = worker_main.WorkerConfig(args, require_worker_token=False)

        self.assertEqual(config.poll_seconds, 5)
        self.assertEqual(config.poll_jitter_seconds, 2)
        self.assertEqual(config.max_backoff_seconds, 60)
        self.assertEqual(config.watcher_poll_seconds, 5)
        self.assertEqual(config.codex_timeout_seconds, 1800)
        self.assertEqual(config.codex_doctor_timeout_seconds, 60)
        self.assertEqual(config.result_upload_attempts, 5)
        self.assertEqual(config.failed_checkout_retention_seconds, 0)
        self.assertEqual(config.scan_summary_log_max_bytes, 10 * 1024 * 1024)

    def test_worker_config_bounds_resource_environment_values(self) -> None:
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_test",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )
        env = {
            "PULLWISE_RESULT_UPLOAD_ATTEMPTS": "100000",
            "PULLWISE_MAX_CHECKOUT_BYTES": str(10**15),
            "PULLWISE_MAX_LOG_BYTES": str(10**15),
            "PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES": str(10**15),
        }

        with patch.dict(worker_main.os.environ, env, clear=False):
            config = worker_main.WorkerConfig(args, require_worker_token=False)

        self.assertEqual(config.result_upload_attempts, 20)
        self.assertEqual(config.max_checkout_bytes, 100 * 1024 * 1024 * 1024)
        self.assertEqual(config.max_log_bytes, 10 * 1024 * 1024 * 1024)
        self.assertEqual(config.scan_summary_log_max_bytes, 100 * 1024 * 1024)

    def test_worker_config_rejects_unsafe_worker_id(self) -> None:
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_bad\nother",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )

        with self.assertRaisesRegex(ValueError, "PULLWISE_WORKER_ID"):
            worker_main.WorkerConfig(args, require_worker_token=False)

    def test_worker_config_rejects_unsafe_service_names_before_deriving_paths(self) -> None:
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_test",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )

        with patch.dict(worker_main.os.environ, {"PULLWISE_SERVICE_NAME": "pullwise-worker/../../evil"}, clear=False):
            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.WorkerConfig(args, require_worker_token=False)

        with patch.dict(
            worker_main.os.environ,
            {"PULLWISE_SERVICE_NAME": "pullwise-worker-wk_1", "PULLWISE_WATCHER_SERVICE_NAME": "pullwise-worker/watcher"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.WorkerConfig(args, require_worker_token=False)

    def test_worker_config_uses_valid_service_names_for_derived_paths(self) -> None:
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_test",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )

        with patch.dict(worker_main.os.environ, {"PULLWISE_SERVICE_NAME": "pullwise-worker-wk_1"}, clear=False):
            config = worker_main.WorkerConfig(args, require_worker_token=False)

        self.assertEqual(config.service_name, "pullwise-worker-wk_1")
        self.assertEqual(config.service_file, "/etc/systemd/system/pullwise-worker-wk_1.service")
        self.assertEqual(config.logrotate_file, "/etc/logrotate.d/pullwise-worker-wk_1")
        self.assertEqual(config.uninstall_marker_file, "/run/pullwise-worker-wk_1/uninstall-requested")
        self.assertEqual(config.watcher_service_name, "pullwise-worker-wk_1-watcher")

    def test_worker_config_rejects_unsafe_service_home_and_path(self) -> None:
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_test",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )

        with patch.dict(worker_main.os.environ, {"PULLWISE_SERVICE_HOME": "relative/home"}, clear=False):
            with self.assertRaisesRegex(ValueError, "PULLWISE_SERVICE_HOME"):
                worker_main.WorkerConfig(args, require_worker_token=False)

        with patch.dict(worker_main.os.environ, {"PULLWISE_SERVICE_PATH": "/usr/bin\n/tmp/bin"}, clear=False):
            with self.assertRaisesRegex(ValueError, "PULLWISE_SERVICE_PATH"):
                worker_main.WorkerConfig(args, require_worker_token=False)

        with patch.dict(worker_main.os.environ, {"PULLWISE_SERVICE_PATH": "/usr/bin:relative/bin"}, clear=False):
            with self.assertRaisesRegex(ValueError, "PULLWISE_SERVICE_PATH"):
                worker_main.WorkerConfig(args, require_worker_token=False)

    def test_worker_config_accepts_windows_service_path_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows service PATH parsing is only meaningful on Windows.")
        args = SimpleNamespace(
            server_url="http://localhost:8080",
            worker_token="",
            worker_id="wk_test",
            provider="codex",
            poll_seconds=None,
            checkout_root=None,
            work_dir=None,
            log_dir=None,
            codex_command=None,
            codex_timeout_seconds=None,
        )
        raw_path = r"C:\Pullwise\bin;D:\Codex\bin;C:\Pullwise\bin"

        with patch.dict(
            worker_main.os.environ,
            {
                "PULLWISE_SERVICE_HOME": r"C:\Pullwise\worker",
                "PULLWISE_SERVICE_PATH": raw_path,
            },
            clear=False,
        ):
            config = worker_main.WorkerConfig(args, require_worker_token=False)

        self.assertEqual(config.service_path, r"C:\Pullwise\bin;D:\Codex\bin")
        self.assertIn(os.pathsep, worker_main.provider_tool_path(config))

    def test_safe_service_path_rejects_windows_drive_paths_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX path validation is only meaningful off Windows.")

        with self.assertRaisesRegex(ValueError, "PULLWISE_SERVICE_PATH"):
            worker_main.safe_service_path(r"C:\Pullwise\bin")

    def test_provider_process_env_rejects_unsafe_service_home_on_direct_config(self) -> None:
        cfg = SimpleNamespace(service_home="relative/home", service_path="/usr/bin")

        with self.assertRaisesRegex(ValueError, "PULLWISE_SERVICE_HOME"):
            worker_main.provider_process_env(cfg)

    def git(self, repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return (completed.stdout or "").strip()

    def make_git_repo(self, root: Path) -> tuple[Path, str, str]:
        repo = root / "source"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.email", "test@example.com")
        self.git(repo, "config", "user.name", "Test User")
        (repo / "app.txt").write_text("one\n", encoding="utf-8")
        self.git(repo, "add", "app.txt")
        self.git(repo, "commit", "-m", "one")
        first = self.git(repo, "rev-parse", "HEAD")
        (repo / "app.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "commit", "-am", "two")
        second = self.git(repo, "rev-parse", "HEAD")
        return repo, first, second

    def test_result_upload_uses_gzip_json_body(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            result_upload_compress_min_bytes=1,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.result("job_gzip", {"status": "done", "debug": "x" * 2048})

        request = captured["request"]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get("content-encoding"), "gzip")
        decoded = json.loads(gzip.decompress(request.data).decode("utf-8"))
        self.assertEqual(decoded["status"], "done")
        self.assertEqual(decoded["debug"], "x" * 2048)

    def test_client_encodes_dynamic_url_path_segments(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        urls: list[str] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            del timeout
            urls.append(request.full_url)
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.progress("job/a?b", "ai", 80)
            client.result("job/a?b", {"status": "done"})
            client.command_status("cmd/a?b", "succeeded")

        self.assertEqual(
            urls,
            [
                "https://pullwise.example/worker/jobs/job%2Fa%3Fb/progress",
                "https://pullwise.example/worker/jobs/job%2Fa%3Fb/result",
                "https://pullwise.example/worker/commands/cmd%2Fa%3Fb/status",
            ],
        )

    def test_client_rejects_invalid_url_path_segments(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)

        with self.assertRaisesRegex(worker_main.PullwiseRequestError, "URL path segment is invalid"):
            client.progress("job\nbad", "ai", 80)

        with self.assertRaisesRegex(worker_main.PullwiseRequestError, "URL path segment is invalid"):
            client.log_stream_lines("s" * 129, [])

    def test_result_upload_defers_retry_without_sleeping_in_job_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=2,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            payload = {"status": "done", "result_checksum": "checksum-deferred"}

            with patch.object(
                worker.client,
                "result",
                side_effect=worker_main.PullwiseRequestError("offline"),
            ), patch.object(worker, "schedule_pending_result_upload") as schedule_upload, patch.object(
                worker_main.time,
                "sleep",
            ) as sleep:
                uploaded = worker.upload_result_once_or_defer("job_deferred", payload)

            self.assertFalse(uploaded)
            sleep.assert_not_called()
            pending_path = worker_main.result_upload_file(work_dir, "job_deferred")
            self.assertTrue(pending_path.exists())
            record = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(record["job_id"], "job_deferred")
            self.assertEqual(record["payload"], payload)
            schedule_upload.assert_called_once_with("job_deferred", pending_path)

    def test_permanent_result_upload_failure_removes_pending_payload_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_bad_request")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(
                json.dumps({"job_id": "job_bad_request", "payload": {"debugMarkdown": "sensitive report"}}),
                encoding="utf-8",
            )
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_exception(worker_main.PullwiseHTTPError("HTTP 400: bad request", 400))
            worker._pending_result_uploads["job_bad_request"] = (future, pending_path)

            worker.collect_result_uploads()

            self.assertFalse(pending_path.exists())
            self.assertFalse(pending_path.with_suffix(".failed.json").exists())
            self.assertIn("permanently failed", worker.last_error or "")

    def test_invalid_pending_result_upload_record_is_not_retried_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_corrupt")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text("{not json", encoding="utf-8")

            try:
                worker.upload_pending_result_file(pending_path)
            except worker_main.PendingResultUploadRecordError as exc:
                record_error = exc
            else:
                raise AssertionError("expected invalid pending record to fail permanently")

            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_exception(record_error)
            worker._pending_result_uploads["job_corrupt"] = (future, pending_path)

            worker.collect_result_uploads()

            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(pending_path.exists())
            self.assertIn("permanently invalid", worker.last_error or "")

    def test_oversized_pending_result_upload_record_is_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_huge")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_bytes(b"{" + (b"x" * (worker_main._PENDING_RESULT_UPLOAD_RECORD_MAX_BYTES + 1)))

            with patch.object(worker.client, "result") as upload:
                worker.load_pending_result_uploads()

            upload.assert_not_called()
            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(pending_path.exists())
            self.assertIn("text file too large", worker.last_error or "")

    def test_pending_result_upload_rejects_filename_job_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_a")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(
                json.dumps({"job_id": "job_b", "payload": {"status": "done"}}),
                encoding="utf-8",
            )

            with patch.object(worker.client, "result") as upload:
                worker.load_pending_result_uploads()

            upload.assert_not_called()
            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(pending_path.exists())
            self.assertIn("filename does not match job_id", worker.last_error or "")

    def test_pending_result_upload_rejects_symlink_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            outside_record = work_dir / "outside.json"
            outside_record.write_text(
                json.dumps({"job_id": "job_symlink", "payload": {"status": "done"}}),
                encoding="utf-8",
            )
            pending_path = worker_main.result_upload_file(work_dir, "job_symlink")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.symlink_to(outside_record)

            with patch.object(worker.client, "result") as upload:
                worker.load_pending_result_uploads()

            upload.assert_not_called()
            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(pending_path.exists())
            self.assertTrue(outside_record.exists())
            self.assertIn("must not be a symlink", worker.last_error or "")

    def test_pending_result_upload_read_rejects_symlink_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            outside_record = work_dir / "outside.json"
            outside_record.write_text(
                json.dumps({"job_id": "job_race", "payload": {"status": "done"}}),
                encoding="utf-8",
            )
            pending_path = worker_main.result_upload_file(work_dir, "job_race")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.symlink_to(outside_record)

            with patch.object(type(pending_path), "is_symlink", return_value=False):
                with self.assertRaisesRegex(worker_main.PendingResultUploadRecordError, "unreadable"):
                    worker.pending_result_upload_record(pending_path)

            self.assertEqual(outside_record.read_text(encoding="utf-8"), json.dumps({"job_id": "job_race", "payload": {"status": "done"}}))

    def test_read_no_follow_text_file_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "large.txt"
            path.write_text("abcdef", encoding="utf-8")

            with self.assertRaisesRegex(OSError, "text file too large"):
                worker_main.read_no_follow_text_file(path, max_bytes=5)

    def test_pending_result_upload_rejects_symlinked_spool_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            outside_spool = root / "outside-spool"
            outside_spool.mkdir()
            (work_dir / ".pullwise-result-uploads").symlink_to(outside_spool, target_is_directory=True)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with self.assertRaisesRegex(RuntimeError, "symlinked directory"):
                worker.defer_result_upload("job_spool", {"status": "done"})

            self.assertEqual(list(outside_spool.iterdir()), [])

    def test_load_pending_result_uploads_rejects_symlinked_spool_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            outside_spool = root / "outside-spool"
            outside_spool.mkdir()
            (outside_spool / "job_spool.json").write_text(
                json.dumps({"job_id": "job_spool", "payload": {"status": "done"}}),
                encoding="utf-8",
            )
            (work_dir / ".pullwise-result-uploads").symlink_to(outside_spool, target_is_directory=True)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker.client, "result") as upload:
                worker.load_pending_result_uploads()

            upload.assert_not_called()
            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertTrue((outside_spool / "job_spool.json").exists())
            self.assertIn("directory must not be a symlink", worker.last_error or "")

    def test_pending_result_upload_record_rejects_symlinked_spool_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            outside_spool = root / "outside-spool"
            outside_spool.mkdir()
            (outside_spool / "job_spool.json").write_text(
                json.dumps({"job_id": "job_spool", "payload": {"status": "done"}}),
                encoding="utf-8",
            )
            (work_dir / ".pullwise-result-uploads").symlink_to(outside_spool, target_is_directory=True)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_spool")

            with self.assertRaisesRegex(
                worker_main.PendingResultUploadRecordError,
                "directory must not be a symlink",
            ):
                worker.pending_result_upload_record(pending_path)

    def test_done_pending_result_upload_still_renews_job_until_collected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_retry")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(json.dumps({"job_id": "job_retry", "payload": {"status": "done"}}), encoding="utf-8")
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_exception(worker_main.PullwiseRequestError("offline"))
            worker._pending_result_uploads["job_retry"] = (future, pending_path)

            self.assertEqual(worker.pending_result_job_ids(), ["job_retry"])

    def test_worker_job_slot_merges_pending_and_running_active_job_ids(self) -> None:
        slot = worker_main.WorkerJobSlot()
        slot.job = {"job_id": "job_running"}

        self.assertEqual(slot.active_job_ids(["job_pending"]), ["job_pending", "job_running"])
        self.assertEqual(slot.active_job_ids(["job_running"]), ["job_running"])

        slot.job = {"job_id": "../escape"}
        self.assertEqual(slot.active_job_ids(["job_pending"]), ["job_pending"])

    def test_successful_pending_result_upload_clears_matching_upload_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_retry")
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_result(None)
            worker._pending_result_uploads["job_retry"] = (future, pending_path)
            worker.last_error = "result upload retry failed for job_retry: offline"

            worker.collect_result_uploads()

            self.assertIsNone(worker.last_error)

    def test_successful_pending_result_upload_keeps_unrelated_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_retry")
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_result(None)
            worker._pending_result_uploads["job_retry"] = (future, pending_path)
            worker.last_error = "worker cleanup failed: disk busy"

            worker.collect_result_uploads()

            self.assertEqual(worker.last_error, "worker cleanup failed: disk busy")

    def test_deferred_result_upload_keeps_cancel_event_until_collected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_pending_cancel")
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            event = worker.job_cancel_event("job_pending_cancel")
            worker._pending_result_uploads["job_pending_cancel"] = (future, pending_path)

            worker.clear_job_cancel_event("job_pending_cancel", event)
            worker.cancel_server_jobs(["job_pending_cancel"])

            self.assertTrue(event.is_set())

            future.set_result(None)
            worker.collect_result_uploads()

            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(worker.job_cancel_requested("job_pending_cancel"))

    def test_cancelled_pending_result_upload_is_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.job_cancel_event("job_pending_cancel")
            worker.cancel_server_jobs(["job_pending_cancel"])

            with patch.object(worker.client, "result") as result:
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.upload_result_with_retry("job_pending_cancel", {"status": "done"})

            result.assert_not_called()

    def test_cancelled_pending_result_upload_is_not_sent_when_cancel_arrives_before_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.cancel_server_jobs(["job_pending_cancel"])

            with patch.object(worker.client, "result") as result:
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.upload_result_with_retry("job_pending_cancel", {"status": "done"})

            result.assert_not_called()

    def test_pending_result_upload_409_is_treated_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=1,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_pending_cancel")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(
                json.dumps({"job_id": "job_pending_cancel", "payload": {"status": "done"}}),
                encoding="utf-8",
            )

            with patch.object(
                worker.client,
                "result",
                side_effect=worker_main.PullwiseHTTPError("HTTP 409: conflict", 409),
            ):
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.upload_pending_result_file(pending_path)

            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_exception(worker_main.WorkerJobCancelled("job job_pending_cancel is no longer accepting worker updates"))
            worker._pending_result_uploads["job_pending_cancel"] = (future, pending_path)
            worker.job_cancel_event("job_pending_cancel")
            worker.cancel_server_jobs(["job_pending_cancel"])

            worker.collect_result_uploads()

            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(pending_path.exists())
            self.assertFalse(worker.job_cancel_requested("job_pending_cancel"))
            self.assertIn("no longer accepting worker updates", worker.last_error or "")
            self.assertNotIn("permanently failed", worker.last_error or "")

    def test_cancelled_pending_result_upload_is_removed_instead_of_rescheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                result_upload_compress_min_bytes=1,
                result_upload_attempts=2,
                work_dir=work_dir,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            pending_path = worker_main.result_upload_file(work_dir, "job_pending_cancel")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(json.dumps({"job_id": "job_pending_cancel", "payload": {"status": "done"}}), encoding="utf-8")
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_exception(worker_main.WorkerJobCancelled("job job_pending_cancel is no longer accepting worker updates"))
            worker._pending_result_uploads["job_pending_cancel"] = (future, pending_path)

            worker.collect_result_uploads()

            self.assertEqual(worker.pending_result_job_ids(), [])
            self.assertFalse(pending_path.exists())
            self.assertFalse(worker.job_cancel_requested("job_pending_cancel"))
            self.assertIn("no longer accepting worker updates", worker.last_error or "")

    def test_heartbeat_payload_does_not_report_capacity_or_free_slots(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.heartbeat(running_jobs=7, active_job_ids=["job_one"])

        self.assertEqual(captured["payload"]["running_jobs"], 1)
        self.assertEqual(captured["payload"]["active_job_ids"], ["job_one"])
        self.assertNotIn("max_concurrent_jobs", captured["payload"])
        self.assertNotIn("free_slots", captured["payload"])

    def test_heartbeat_payload_filters_invalid_active_job_ids(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.heartbeat(
                active_job_ids=[
                    "job_one",
                    "../escape",
                    "bad\njob",
                    ".",
                    "j" * (worker_main._MAX_JOB_ID_LENGTH + 1),
                    "",
                    None,
                    "job_one",
                    "job-two.3",
                ],
            )

        self.assertEqual(captured["payload"]["active_job_ids"], ["job_one", "job-two.3"])

    def test_safe_job_id_rejects_oversized_identifier(self) -> None:
        oversized = "j" * (worker_main._MAX_JOB_ID_LENGTH + 1)

        with self.assertRaisesRegex(ValueError, "unsafe path characters"):
            worker_main.safe_job_id(oversized)

        with self.assertRaisesRegex(ValueError, "unsafe path characters"):
            worker_main.result_upload_file(Path("/tmp/work"), oversized)

        with self.assertRaisesRegex(ValueError, "unsafe path characters"):
            worker_main.validate_claimed_job({"job_id": oversized})

    def test_validate_claimed_job_normalizes_and_rejects_attempts(self) -> None:
        job = {"job_id": "job_attempt", "attempt": "3"}

        self.assertIs(worker_main.validate_claimed_job(job), job)
        self.assertEqual(job["attempt"], 3)

        for attempt in (0, -1, True, "bad", 1_000_001):
            with self.subTest(attempt=attempt):
                with self.assertRaisesRegex(ValueError, "Worker job attempt"):
                    worker_main.validate_claimed_job({"job_id": "job_attempt", "attempt": attempt})

    def test_heartbeat_payload_filters_invalid_ready_providers(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.heartbeat(ready_providers=["CODEX", "unknown", "codex\nbad", "", None, "codex"])

        self.assertEqual(captured["payload"]["readyProviders"], ["codex"])

    def test_heartbeat_payload_redacts_and_bounds_error_text(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.heartbeat(last_error=f"first line secret-token\nsecond line {'x' * 1000}")

        last_error = captured["payload"]["last_error"]
        self.assertEqual(last_error, "first line [redacted]")
        self.assertNotIn("secret-token", last_error)
        self.assertNotIn("\n", last_error)

    def test_progress_payload_redacts_and_bounds_protocol_text(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.progress("job_1", "ai\nbad", 80, f"secret-token\n{'x' * 1000}", f"secret-token {'y' * 2000}")

        payload = captured["payload"]
        self.assertEqual(payload["phase"], "ai")
        self.assertEqual(payload["message"], "[redacted]")
        self.assertLessEqual(len(payload["logs_summary"]), 1000)
        self.assertNotIn("secret-token", payload["logs_summary"])

    def test_command_status_payload_redacts_error_text(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b"{}"

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=fake_urlopen):
            client.command_status("cmd_1", "failed\nignored", error="secret-token\nmore")

        self.assertEqual(captured["payload"]["status"], "failed")
        self.assertEqual(captured["payload"]["error"], "[redacted]")

    def test_claim_rejects_non_object_job_payload(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b'{"job": "not-an-object"}'

        with patch.object(worker_main.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(worker_main.PullwiseRequestError):
                client.claim()

    def test_client_rejects_non_object_json_response(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                return b'["not", "an", "object"]'

        with patch.object(worker_main.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(worker_main.PullwiseRequestError, "JSON response must be an object"):
                client.claim()

    def test_client_rejects_oversized_success_response_body(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1) -> bytes:
                captured["size"] = size
                return b"x" * (worker_main.WORKER_HTTP_RESPONSE_MAX_BYTES + 1)

        with patch.object(worker_main.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(worker_main.PullwiseRequestError, "response body too large"):
                client.claim()

        self.assertEqual(captured["size"], worker_main.WORKER_HTTP_RESPONSE_MAX_BYTES + 1)

    def test_client_rejects_unbounded_response_reader(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b"{}"

        with patch.object(worker_main.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(worker_main.PullwiseRequestError, "bounded reads"):
                client.claim()

    def test_client_http_error_includes_server_error_body(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        error = worker_main.urllib.error.HTTPError(
            "https://pullwise.example/worker/jobs/claim",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"job payload is malformed"}'),
        )

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(
                worker_main.PullwiseHTTPError,
                "HTTP 400: Bad Request: job payload is malformed",
            ):
                client.claim()

    def test_client_http_error_redacts_secrets_from_server_body(self) -> None:
        config = SimpleNamespace(
            server_url="https://pullwise.example",
            worker_token="secret-token",
            worker_id="wk_single",
            provider="codex",
            provider_chain=["codex"],
            result_upload_compress_min_bytes=1024,
        )
        client = worker_main.PullwiseClient(config)
        error = worker_main.urllib.error.HTTPError(
            "https://pullwise.example/worker/jobs/claim",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(
                b'{"error":"failed with secret-token and https://x-access-token:ghs_secret@example.com/owner/repo.git"}'
            ),
        )

        with patch.object(worker_main.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(worker_main.PullwiseHTTPError) as raised:
                client.claim()

        message = str(raised.exception)
        self.assertIn("HTTP 500: Internal Server Error", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("ghs_secret", message)

    def test_worker_once_does_not_start_non_object_claimed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_claim",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=10**9,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._doctor_status = "ok"

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}},
            ), patch.object(
                worker.client,
                "claim",
                side_effect=worker_main.PullwiseRequestError("claim response job must be an object"),
            ), patch.object(worker, "run_job") as run_job:
                worker.run(once=True)

            run_job.assert_not_called()
            self.assertIn("job claim failed", worker.last_error or "")

    def test_worker_once_heartbeats_before_expensive_readiness_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_readiness_order",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=0,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._set_readiness_snapshot(
                worker_main.WorkerReadinessSnapshot(
                    checked_at=111,
                    doctor_status="ok",
                    codex_ready=True,
                    ready_providers=("codex",),
                    ready_for_claim=True,
                )
            )
            calls: list[str] = []
            heartbeat_payload: dict[str, object] = {}

            def heartbeat(**kwargs: object) -> dict:
                heartbeat_payload.update(kwargs)
                calls.append("heartbeat")
                return {"worker": {"status": "idle"}}

            def readiness() -> bool:
                calls.append("readiness")
                worker._set_readiness_snapshot(
                    worker_main.WorkerReadinessSnapshot(
                        checked_at=222,
                        doctor_status="degraded",
                        codex_ready=False,
                        ready_providers=(),
                        ready_for_claim=False,
                    )
                )
                return True
            with patch.object(worker, "refresh_readiness_if_due", side_effect=readiness), patch.object(
                worker.client,
                "heartbeat",
                side_effect=heartbeat,
            ), patch.object(worker.client, "claim", return_value=None):
                worker.run(once=True)

            self.assertEqual(calls, ["heartbeat", "readiness"])
            self.assertEqual(heartbeat_payload["doctor_status"], "ok")
            self.assertIs(heartbeat_payload["codex_ready"], True)
            self.assertEqual(heartbeat_payload["ready_providers"], ["codex"])
            self.assertEqual(heartbeat_payload["doctor_checked_at"], 111)

    def test_worker_once_does_not_start_claimed_job_with_unsafe_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_claim",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=10**9,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._doctor_status = "ok"

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}},
            ), patch.object(worker.client, "claim", return_value={"job_id": "../job_escape"}), patch.object(
                worker, "run_job"
            ) as run_job:
                worker.run(once=True)

            run_job.assert_not_called()
            self.assertIn("job claim failed", worker.last_error or "")
            self.assertIn("unsafe path characters", worker.last_error or "")

    def test_worker_once_does_not_start_claimed_job_with_invalid_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_claim",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=10**9,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._doctor_status = "ok"

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}},
            ), patch.object(worker.client, "claim", return_value={"job_id": "job_bad_attempt", "attempt": "bad"}), patch.object(
                worker, "run_job"
            ) as run_job:
                worker.run(once=True)

            run_job.assert_not_called()
            self.assertIn("job claim failed", worker.last_error or "")
            self.assertIn("Worker job attempt", worker.last_error or "")

    def test_worker_once_does_not_start_claimed_job_with_invalid_git_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_claim_ref",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=10**9,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._doctor_status = "ok"

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}},
            ), patch.object(
                worker.client,
                "claim",
                return_value={
                    "job_id": "job_bad_ref",
                    "repo": "owner/repo",
                    "clone_url": "https://github.com/owner/repo.git",
                    "branch": "main:refs/heads/owned",
                    "commit": "pending",
                },
            ), patch.object(worker, "run_job") as run_job:
                worker.run(once=True)

            run_job.assert_not_called()
            self.assertIn("job claim failed", worker.last_error or "")
            self.assertIn("branch name is invalid", worker.last_error or "")

    def test_worker_once_does_not_start_claimed_job_with_mismatched_clone_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_claim_clone",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=10**9,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._doctor_status = "ok"

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}},
            ), patch.object(
                worker.client,
                "claim",
                return_value={
                    "job_id": "job_bad_clone",
                    "repo": "owner/repo",
                    "clone_url": "https://github.com/other/repo.git",
                    "branch": "main",
                    "commit": "pending",
                },
            ), patch.object(worker, "run_job") as run_job:
                worker.run(once=True)

            run_job.assert_not_called()
            self.assertIn("job claim failed", worker.last_error or "")
            self.assertIn("path does not match", worker.last_error or "")

    def test_worker_once_does_not_start_claimed_job_with_invalid_clone_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_claim_clone_token",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024,
                machine_metrics_interval_seconds=10**9,
                cleanup_interval_seconds=10**9,
                readiness_check_seconds=10**9,
                poll_seconds=0,
                max_backoff_seconds=1,
                poll_jitter_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker._doctor_status = "ok"

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}},
            ), patch.object(
                worker.client,
                "claim",
                return_value={
                    "job_id": "job_bad_clone_token",
                    "repo": "owner/repo",
                    "clone_url": "https://github.com/owner/repo.git",
                    "branch": "main",
                    "commit": "pending",
                    "clone_token": {"repo": "owner/repo", "token": "ghs_good\nHeader: injected"},
                },
            ), patch.object(worker, "run_job") as run_job:
                worker.run(once=True)

            run_job.assert_not_called()
            self.assertIn("job claim failed", worker.last_error or "")
            self.assertIn("Clone token is invalid", worker.last_error or "")

    def test_run_job_uploads_result_when_progress_updates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_progress",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_progress_flaky",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }

            with patch.object(
                worker.client,
                "progress",
                side_effect=worker_main.PullwiseRequestError("server restarting"),
            ), patch.object(
                worker_main,
                "clone_repository",
                return_value="abc123",
            ), patch.object(
                worker_main,
                "enforce_repository_limits",
            ), patch.object(
                worker_main,
                "collect_preflight_metadata",
                return_value={"summary": "preflight ok"},
            ), patch.object(
                worker_main,
                "run_graph_verified_review_payload",
                return_value={
                    "version": "graph-verified-code-review/1",
                    "runId": "gv_run",
                    "confirmedCount": 0,
                    "rejectedCount": 0,
                    "blockedCount": 0,
                    "debugMarkdown": "",
                    "finalJson": {"confirmed": []},
                },
            ), patch.object(
                worker_main,
                "graph_verified_summary_findings",
                return_value=[],
            ), patch.object(
                worker,
                "upload_result_once_or_defer",
                return_value=True,
            ) as upload:
                worker.run_job(job)

            upload.assert_called_once()
            summary_log = config.log_dir / "scan-summary.log"
            self.assertTrue(summary_log.is_file())
            summary_text = summary_log.read_text(encoding="utf-8")
            self.assertIn('"status": "progress"', summary_text)
            self.assertIn("Running GraphVerified review", summary_text)
            self.assertIn('"status": "done"', summary_text)

    def test_run_job_throttles_graph_verified_task_progress_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_progress",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_progress_throttle",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }
            progress_messages: list[str] = []

            def fake_progress(_job_id: str, _phase: str, _progress: int, message: str = "", logs_summary: str = "") -> None:
                del _job_id, _phase, _progress, logs_summary
                progress_messages.append(message)

            def fake_graph_verified(_config: object, _job: dict, _checkout_dir: Path, progress_callback=None) -> dict:
                del _config, _job, _checkout_dir
                for index in range(1, 6):
                    progress_callback(
                        {
                            "stage": "graph",
                            "message": f"Graph: mapping shards {index}/5",
                            "current": index,
                            "total": 5,
                        }
                    )
                return {
                    "version": "graph-verified-code-review/1",
                    "runId": "gv_run",
                    "confirmedCount": 0,
                    "rejectedCount": 0,
                    "blockedCount": 0,
                    "debugMarkdown": "",
                    "finalJson": {"confirmed": []},
                }

            with patch.object(worker.client, "progress", side_effect=fake_progress), patch.object(
                worker_main,
                "GRAPH_VERIFIED_PROGRESS_UPLOAD_MIN_SECONDS",
                999.0,
            ), patch.object(
                worker_main,
                "clone_repository",
                return_value="abc123",
            ), patch.object(worker_main, "enforce_repository_limits"), patch.object(
                worker_main,
                "collect_preflight_metadata",
                return_value={"summary": "preflight ok"},
            ), patch.object(
                worker_main,
                "run_graph_verified_review_payload",
                side_effect=fake_graph_verified,
            ), patch.object(worker_main, "graph_verified_summary_findings", return_value=[]), patch.object(
                worker,
                "upload_result_once_or_defer",
                return_value=True,
            ):
                worker.run_job(job)

            self.assertIn("Graph: mapping shards 1/5", progress_messages)
            self.assertIn("Graph: mapping shards 5/5", progress_messages)
            self.assertNotIn("Graph: mapping shards 2/5", progress_messages)
            self.assertNotIn("Graph: mapping shards 3/5", progress_messages)
            self.assertNotIn("Graph: mapping shards 4/5", progress_messages)
            summary_text = (config.log_dir / "scan-summary.log").read_text(encoding="utf-8")
            self.assertIn("Graph: mapping shards 4/5", summary_text)

    def test_run_job_uploads_deterministic_findings_with_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_static",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_static",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }
            static_finding = {
                "id": "static_secret_1",
                "severity": "high",
                "title": "Committed token",
                "file": "app.env",
                "line": 1,
                "verificationStatus": "static_proof",
                "affectedLocations": [{"file": "app.env", "startLine": 1, "endLine": 1}],
            }

            with patch.object(worker.client, "progress"), patch.object(
                worker_main,
                "clone_repository",
                return_value="abc123",
            ), patch.object(worker_main, "enforce_repository_limits"), patch.object(
                worker_main,
                "collect_preflight_metadata",
                return_value={"summary": "preflight ok"},
            ), patch.object(
                worker_main,
                "run_deterministic_repository_checks",
                return_value=[static_finding],
            ), patch.object(
                worker_main,
                "run_graph_verified_review_payload",
                return_value={
                    "version": "graph-verified-code-review/1",
                    "runId": "gv_run",
                    "confirmedCount": 0,
                    "rejectedCount": 0,
                    "blockedCount": 0,
                    "debugMarkdown": "",
                    "finalJson": {"confirmed": []},
                },
            ), patch.object(worker_main, "graph_verified_summary_findings", return_value=[]), patch.object(
                worker,
                "upload_result_once_or_defer",
                return_value=True,
            ) as upload:
                worker.run_job(job)

            payload = upload.call_args.args[1]
            self.assertEqual(payload["summary"]["high"], 1)
            self.assertEqual(payload["deterministicFindings"], [static_finding])
            self.assertEqual(payload["graphVerifiedReport"]["deterministicCount"], 1)
            self.assertEqual(payload["graphVerifiedReport"]["finalJson"]["deterministicFindings"], [static_finding])

    def test_run_job_cleanup_unlinks_checkout_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_cleanup",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_cleanup_symlink",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }
            target = root / "outside"
            target.mkdir()
            (target / "keep.txt").write_text("keep", encoding="utf-8")

            def fake_clone(_job: dict, checkout_dir: Path, **_kwargs: object) -> str:
                checkout_dir.parent.mkdir(parents=True, exist_ok=True)
                checkout_dir.symlink_to(target, target_is_directory=True)
                return "abc123"

            with patch.object(worker.client, "progress"), patch.object(
                worker_main,
                "clone_repository",
                side_effect=fake_clone,
            ), patch.object(worker_main, "enforce_repository_limits"), patch.object(
                worker_main,
                "collect_preflight_metadata",
                return_value={"summary": "preflight ok"},
            ), patch.object(
                worker_main,
                "run_graph_verified_review_payload",
                return_value={
                    "version": "graph-verified-code-review/1",
                    "runId": "gv_run",
                    "confirmedCount": 0,
                    "rejectedCount": 0,
                    "blockedCount": 0,
                    "debugMarkdown": "",
                    "finalJson": {"confirmed": []},
                },
            ), patch.object(worker_main, "graph_verified_summary_findings", return_value=[]), patch.object(
                worker,
                "upload_result_once_or_defer",
                return_value=True,
            ) as upload:
                worker.run_job(job)

            upload.assert_called_once()
            checkout = worker_main.checkout_dir_for_job(config.work_dir, "job_cleanup_symlink")
            self.assertFalse(checkout.exists())
            self.assertFalse(checkout.is_symlink())
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_run_job_stops_without_result_when_progress_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_cancelled",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_cancelled_progress",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }

            with patch.object(
                worker.client,
                "progress",
                side_effect=worker_main.PullwiseHTTPError("HTTP 409: conflict", 409),
            ), patch.object(worker_main, "clone_repository") as clone_repository, patch.object(
                worker,
                "upload_result_once_or_defer",
            ) as upload:
                worker.run_job(job)

            clone_repository.assert_not_called()
            upload.assert_not_called()
            self.assertIn("no longer accepting worker updates", worker.last_error or "")
            summary_text = (config.log_dir / "scan-summary.log").read_text(encoding="utf-8")
            self.assertIn('"status": "cancelled"', summary_text)
            self.assertNotIn('"status": "failed"', summary_text)

    def test_report_progress_stops_when_heartbeat_cancelled_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_cancel_event",
                work_dir=root / "work",
                log_dir=root / "logs",
                scan_summary_log_max_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.job_cancel_event("job_cancel_event")
            worker.cancel_server_jobs(["job_cancel_event"])

            with patch.object(worker.client, "progress") as progress:
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.report_progress("job_cancel_event", "ai", 80, "still running")

            progress.assert_not_called()

    def test_result_upload_stops_when_heartbeat_cancelled_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_cancel_upload",
                work_dir=root / "work",
                log_dir=root / "logs",
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.job_cancel_event("job_cancel_upload")
            worker.cancel_server_jobs(["job_cancel_upload"])

            with patch.object(worker.client, "result") as result, patch.object(worker, "defer_result_upload") as defer:
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.upload_result_once_or_defer("job_cancel_upload", {"status": "done"})

            result.assert_not_called()
            defer.assert_not_called()

    def test_result_upload_does_not_defer_when_cancelled_after_retryable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_cancel_upload_retry",
                work_dir=root / "work",
                log_dir=root / "logs",
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.job_cancel_event("job_cancel_upload_retry")

            def fail_then_cancel(job_id: str, payload: dict) -> None:
                del payload
                worker.cancel_server_jobs([job_id])
                raise worker_main.PullwiseHTTPError("HTTP 503: unavailable", 503)

            with patch.object(worker.client, "result", side_effect=fail_then_cancel), patch.object(worker, "defer_result_upload") as defer:
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.upload_result_once_or_defer("job_cancel_upload_retry", {"status": "done"})

            defer.assert_not_called()

    def test_result_upload_retry_wait_stops_when_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_cancel_retry_wait",
                work_dir=root / "work",
                log_dir=root / "logs",
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
                result_upload_attempts=2,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            event = worker.job_cancel_event("job_cancel_retry_wait")

            with patch.object(
                worker.client,
                "result",
                side_effect=worker_main.PullwiseRequestError("offline"),
            ) as result, patch.object(event, "wait", return_value=True) as wait:
                with self.assertRaises(worker_main.WorkerJobCancelled):
                    worker.upload_result_with_retry("job_cancel_retry_wait", {"status": "done"})

            self.assertEqual(result.call_count, 1)
            wait.assert_called_once_with(1)

    def test_process_runner_stops_when_cancel_event_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cancel_event = threading.Event()
            cancel_event.set()
            set_process_cancel_event(cancel_event)
            try:
                with self.assertRaises(ProcessCancelled):
                    run_process(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        cwd=Path(tmp_dir),
                        timeout=60,
                    )
            finally:
                clear_process_cancel_event()

    def test_process_cancel_event_is_visible_to_worker_threads(self) -> None:
        cancel_event = threading.Event()
        set_process_cancel_event(cancel_event)
        try:
            cancel_event.set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                self.assertTrue(executor.submit(process_cancel_requested).result(timeout=2))
        finally:
            clear_process_cancel_event()

    def test_process_runner_kills_process_group_on_timeout(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group termination is POSIX-specific")

        class FakeProcess:
            pid = 12345
            returncode = None
            stdin = None

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(["fake"], timeout)
                self.returncode = -9
                return self.returncode

            def poll(self):
                return self.returncode

        fake_process = FakeProcess()

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("codereview.utils.process.subprocess.Popen", return_value=fake_process) as popen, patch(
                "codereview.utils.process.os.killpg"
            ) as killpg:
                result = run_process([sys.executable, "-c", "pass"], cwd=Path(tmp_dir), timeout=1)

        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 124)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], fake_process.pid)

    def test_process_runner_rejects_symlinked_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside_logs = root / "outside-logs"
            outside_logs.mkdir()
            log_dir = root / "process-logs"
            log_dir.symlink_to(outside_logs, target_is_directory=True)

            with self.assertRaisesRegex(OSError, "symlink"):
                run_process([sys.executable, "-c", "print('ok')"], cwd=root, log_dir=log_dir)

            self.assertEqual(list(outside_logs.iterdir()), [])

    def test_process_runner_kills_background_child_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            child_pid_file = root / "child.pid"
            result = run_process(
                [
                    "sh",
                    "-c",
                    f"sleep 30 & echo $! > {child_pid_file}; wait",
                ],
                cwd=root,
                timeout=1,
            )
            child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            time.sleep(0.2)

            self.assertTrue(result.timed_out)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_run_job_marks_all_blocked_graph_verified_report_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_blocked",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_all_blocked",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }
            blocked_report = {
                "version": "graph-verified-code-review/1",
                "runId": "20260619-115800",
                "confirmedCount": 0,
                "rejectedCount": 0,
                "blockedCount": 99,
                "debugMarkdown": "Finder blocked before producing candidates.",
                "finalJson": {"confirmed": []},
                "summary": {
                    "finder": {
                        "results": 99,
                        "blocked": 99,
                        "candidates": 0,
                        "blockedItems": [
                            {
                                "reason": (
                                    "finder codex turn failed with exit code 2: "
                                    "codex app-server request thread/start timed out"
                                )
                            }
                        ],
                    },
                    "candidates": {"valid": 0, "selectedForRepro": 0},
                    "reports": {"confirmed": 0, "rejected": 0, "blocked": 99},
                },
            }

            with patch.object(worker_main, "clone_repository", return_value="abc123"), patch.object(
                worker_main,
                "enforce_repository_limits",
            ), patch.object(
                worker_main,
                "collect_preflight_metadata",
                return_value={"summary": "preflight ok"},
            ), patch.object(
                worker_main,
                "run_graph_verified_review_payload",
                return_value=blocked_report,
            ), patch.object(
                worker,
                "upload_result_once_or_defer",
                return_value=True,
            ) as upload:
                worker.run_job(job)

            upload.assert_called_once()
            payload = upload.call_args.args[1]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error_code"], "GRAPH_VERIFIED_COMPLETION_FAILED")
            self.assertIn("GraphVerified finder pipeline blocked every finder task", payload["error"])
            self.assertIn("codex app-server request thread/start timed out", payload["error"])
            summary_text = (config.log_dir / "scan-summary.log").read_text(encoding="utf-8")
            self.assertIn('"status": "failed"', summary_text)
            self.assertIn("codex app-server request thread/start timed out", summary_text)

    def test_graph_verified_report_merges_deterministic_findings(self) -> None:
        report = {
            "confirmedCount": 2,
            "finalJson": {"confirmed": []},
            "summary": {"existing": True},
        }
        deterministic = [
            {"id": "static_1", "severity": "high", "message": "missing script"},
            {"id": "static_2", "severity": "low", "message": "missing source"},
        ]

        merged = worker_main.graph_verified_report_with_deterministic_findings(report, deterministic)
        summary = worker_main.deterministic_summary_findings(deterministic)

        self.assertEqual(merged["confirmedCount"], 4)
        self.assertEqual(merged["deterministicCount"], 2)
        self.assertEqual(merged["finalJson"]["deterministicFindings"], deterministic)
        self.assertEqual(merged["summary"]["deterministic"]["confirmed"], 2)
        self.assertEqual(summary, [{"id": "static_1", "severity": "high"}, {"id": "static_2", "severity": "low"}])

    def test_run_job_fails_when_confirmed_graph_verified_items_are_not_reportable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_unreportable",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_home=str(root / "home"),
                provider="codex",
                provider_chain=["codex"],
                codex_command="codex",
                codex_model="gpt-5",
                codex_reasoning_effort="high",
                failed_checkout_retention_seconds=0,
                scan_summary_log_max_bytes=1024 * 1024,
                result_upload_compress_min_bytes=1024 * 1024,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            job = {
                "job_id": "job_unreportable",
                "attempt": 1,
                "agentConfig": {
                    "provider": "codex",
                    "codex": {"model": "gpt-5", "reasoningEffort": "high"},
                    "graphVerified": {},
                },
                "repositoryLimits": {"maxFiles": 1000, "maxBytes": 1024 * 1024},
            }
            unreportable_report = {
                "version": "graph-verified-code-review/1",
                "runId": "20260619-115900",
                "confirmedCount": 1,
                "rejectedCount": 0,
                "blockedCount": 0,
                "debugMarkdown": "Confirmed item lacked graph evidence.",
                "finalJson": {"confirmed": [{"candidate": {"candidate_id": "c1", "severity": "high"}}]},
                "summary": {
                    "finder": {"results": 1, "blocked": 0, "candidates": 1},
                    "candidates": {"valid": 1, "selectedForRepro": 0},
                    "reports": {"confirmed": 1, "rejected": 0, "blocked": 0},
                },
            }

            with patch.object(worker_main, "clone_repository", return_value="abc123"), patch.object(
                worker_main,
                "enforce_repository_limits",
            ), patch.object(
                worker_main,
                "collect_preflight_metadata",
                return_value={"summary": "preflight ok"},
            ), patch.object(
                worker_main,
                "run_graph_verified_review_payload",
                return_value=unreportable_report,
            ), patch.object(
                worker,
                "upload_result_once_or_defer",
                return_value=True,
            ) as upload:
                worker.run_job(job)

            upload.assert_called_once()
            payload = upload.call_args.args[1]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["summary"], {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0})
            self.assertIn("none were safe to show", payload["error"])

    def test_worker_run_once_claims_and_runs_one_job_without_capacity_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                work_dir=Path(tmp_dir),
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=3600,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            self.assertFalse(hasattr(worker, "effective_max_concurrent_jobs"))

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(worker.client, "heartbeat", return_value={"worker": {"status": "idle"}}), patch.object(
                worker.client, "claim", return_value={"job_id": "job_inline"}
            ), patch.object(
                worker, "run_job"
            ) as run_job:
                worker.run(once=True)

        run_job.assert_called_once_with({"job_id": "job_inline"})

    def test_worker_run_once_ignores_invalid_lifecycle_command_when_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                work_dir=Path(tmp_dir),
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=3600,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}, "command": {"id": "cmd_bad", "command": "unknown"}},
            ), patch.object(worker.client, "claim", return_value={"job_id": "job_inline"}), patch.object(
                worker, "run_job"
            ) as run_job:
                worker.run(once=True)

        run_job.assert_called_once_with({"job_id": "job_inline"})

    def test_worker_run_once_ignores_lifecycle_command_with_invalid_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                work_dir=Path(tmp_dir),
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=3600,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(
                worker.client,
                "heartbeat",
                return_value={"worker": {"status": "idle"}, "command": {"id": "cmd_bad\nnext", "command": "stop"}},
            ), patch.object(worker.client, "command_status") as command_status, patch.object(
                worker.client,
                "claim",
                return_value={"job_id": "job_inline"},
            ), patch.object(
                worker,
                "run_job",
            ) as run_job:
                worker.run(once=True)

        command_status.assert_not_called()
        run_job.assert_called_once_with({"job_id": "job_inline"})

    def test_lifecycle_command_parts_rejects_unsafe_command_ids(self) -> None:
        self.assertIsNone(worker_main.lifecycle_command_parts({"id": "cmd_bad\nnext", "command": "stop"}))
        self.assertIsNone(worker_main.lifecycle_command_parts({"id": "cmd_bad\x00next", "command": "stop"}))
        self.assertIsNone(worker_main.lifecycle_command_parts({"id": "x" * 129, "command": "stop"}))
        self.assertEqual(
            worker_main.lifecycle_command_parts({"id": "cmd/a?b", "command": " STOP "}),
            ("cmd/a?b", "stop"),
        )

    def test_worker_run_once_schedules_cleanup_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                work_dir=Path(tmp_dir),
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(worker.client, "heartbeat", return_value={"worker": {"status": "idle"}}), patch.object(
                worker.client, "claim", return_value=None
            ), patch.object(worker_main, "cleanup_worker_resources") as cleanup:
                worker.run(once=True)

        cleanup.assert_called_once()

    def test_worker_run_once_does_not_schedule_cleanup_after_loop_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                work_dir=Path(tmp_dir),
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=0,
                lifecycle_watcher_enabled=False,
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker, "refresh_readiness_if_due", return_value=True), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(
                worker.client,
                "heartbeat",
                side_effect=worker_main.PullwiseRequestError("offline"),
            ), patch.object(worker_main, "cleanup_worker_resources") as cleanup:
                worker.run(once=True)

        cleanup.assert_not_called()

    def test_worker_run_uploads_heartbeat_log_session_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_1",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_name="pullwise-worker-wk_1",
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=3600,
            )
            config.log_dir.mkdir()
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker, "refresh_readiness_if_due", return_value=False), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(
                worker.client,
                "heartbeat",
                return_value={
                    "worker": {"status": "idle"},
                    "logSession": {"id": "log_1", "created_at": 1781200000},
                },
            ), patch.object(worker_main.WorkerJournalLogTailer, "collect", return_value=([], "")), patch.object(
                worker.client,
                "log_stream_lines",
                return_value={"ok": True, "accepted": True},
            ) as upload:
                worker.run(once=True)

        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], "log_1")
        lines = upload.call_args.args[1]
        self.assertEqual(lines[0]["stream"], "diagnostic")
        self.assertIn("log stream connected", lines[0]["line"])
        self.assertIn("pullwise-worker-wk_1", lines[0]["line"])

    def test_worker_run_leaves_log_session_to_lifecycle_watcher_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_1",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_name="pullwise-worker-wk_1",
                poll_seconds=1,
                poll_jitter_seconds=0,
                max_backoff_seconds=1,
                machine_metrics_interval_seconds=3600,
                cleanup_interval_seconds=3600,
                lifecycle_watcher_enabled=True,
            )
            config.log_dir.mkdir()
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)

            with patch.object(worker, "refresh_readiness_if_due", return_value=False), patch.object(
                worker, "machine_metrics_if_due", return_value=None
            ), patch.object(
                worker.client,
                "heartbeat",
                return_value={
                    "worker": {"status": "idle"},
                    "logSession": {"id": "log_1", "created_at": 1781200000},
                },
            ), patch.object(worker, "handle_log_session") as handle_log_session:
                worker.run(once=True)

        handle_log_session.assert_not_called()

    def test_worker_log_session_collection_error_does_not_crash_loop(self) -> None:
        class BrokenTailer:
            def collect(self):
                raise RuntimeError("tailer broke")

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_1",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_name="pullwise-worker-wk_1",
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.log_tailers["log_1"] = BrokenTailer()

            with patch.object(worker.client, "log_stream_lines") as upload:
                worker.handle_log_session({"id": "log_1"})

            upload.assert_not_called()
            self.assertIn("log stream collection failed", worker.last_error or "")
            self.assertIn("tailer broke", worker.last_error or "")

    def test_worker_log_session_rejects_invalid_session_id_before_collection(self) -> None:
        class BrokenTailer:
            def collect(self):
                raise AssertionError("invalid session id should not collect logs")

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_1",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_name="pullwise-worker-wk_1",
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            worker.log_tailers["log_1"] = BrokenTailer()

            with patch.object(worker.client, "log_stream_lines") as upload:
                worker.handle_log_session({"id": "log_1\nbad"})

            upload.assert_not_called()
            self.assertEqual(worker.log_tailers, {})

    def test_log_stream_session_id_rejects_invalid_url_segments(self) -> None:
        self.assertEqual(worker_main.log_stream_session_id({"id": "log_1"}), "log_1")
        self.assertEqual(worker_main.log_stream_session_id({"id": "log_1\nbad"}), "")
        self.assertEqual(worker_main.log_stream_session_id({"id": "x" * 129}), "")

    def test_worker_log_session_uploads_all_entries_before_checkpoint(self) -> None:
        class FakeTailer:
            def __init__(self) -> None:
                self.committed = None

            def collect(self):
                entries = [
                    {"source": "worker", "stream": "journal", "timestamp": 1781200000 + index, "line": f"line {index}"}
                    for index in range(1201)
                ]
                return entries, {"journal_cursor": "cursor-final", "summary_offset": 1201}

            def commit(self, state):
                self.committed = state

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config = SimpleNamespace(
                server_url="https://pullwise.example",
                worker_token="secret-token",
                worker_id="wk_1",
                work_dir=root / "work",
                log_dir=root / "logs",
                service_name="pullwise-worker-wk_1",
            )
            worker = worker_main.Worker(config)
            self.addCleanup(worker._result_upload_executor.shutdown, wait=False, cancel_futures=True)
            self.addCleanup(worker._cleanup_executor.shutdown, wait=False, cancel_futures=True)
            tailer = FakeTailer()
            worker.log_tailers["log_1"] = tailer

            with patch.object(worker.client, "log_stream_lines", return_value={"ok": True}) as upload:
                worker.handle_log_session({"id": "log_1"})

            batch_sizes = [len(call.args[1]) for call in upload.call_args_list]
            self.assertEqual(batch_sizes, [500, 500, 201])
            self.assertEqual(tailer.committed, {"journal_cursor": "cursor-final", "summary_offset": 1201})

    def test_file_log_tailer_drops_partial_after_truncate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "scan-summary.log"
            path.write_text("old partial", encoding="utf-8")
            tailer = worker_main.WorkerFileLogTailer(path)
            tailer.partial = "stale partial "
            tailer.offset = 999
            path.write_text("new line\n", encoding="utf-8")

            entries, offset, partial = tailer.collect()

        self.assertEqual([entry["line"] for entry in entries], ["new line"])
        self.assertEqual(offset, len("new line\n"))
        self.assertEqual(partial, "")

    def test_file_log_tailer_does_not_follow_symlinked_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside-summary.log"
            outside.write_text("outside\n", encoding="utf-8")
            path = root / "scan-summary.log"
            path.symlink_to(outside)
            tailer = worker_main.WorkerFileLogTailer(path)

            entries, offset, partial = tailer.collect()

        self.assertEqual(entries, [])
        self.assertEqual(offset, 0)
        self.assertEqual(partial, "")

    def test_file_log_tailer_does_not_read_through_symlinked_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside_logs = root / "outside-logs"
            outside_logs.mkdir()
            (outside_logs / "scan-summary.log").write_text("outside secret\n", encoding="utf-8")
            log_dir = root / "logs"
            log_dir.symlink_to(outside_logs, target_is_directory=True)
            tailer = worker_main.WorkerFileLogTailer(log_dir / "scan-summary.log")

            entries, offset, partial = tailer.collect()

        self.assertEqual(entries, [])
        self.assertEqual(offset, 0)
        self.assertEqual(partial, "")

    def test_file_log_tailer_read_does_not_follow_symlink_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / "scan-summary.log"
            path.write_text("inside\n", encoding="utf-8")
            tailer = worker_main.WorkerFileLogTailer(path)
            outside = root / "outside-summary.log"
            outside.write_text("outside\n", encoding="utf-8")
            path.unlink()
            path.symlink_to(outside)

            with patch.object(worker_main, "regular_log_file", return_value=True):
                entries, offset, partial = tailer.collect()

            self.assertEqual(entries, [])
            self.assertEqual(offset, tailer.offset)
            self.assertEqual(partial, tailer.partial)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_trim_file_to_last_bytes_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside-summary.log"
            outside.write_text("0123456789", encoding="utf-8")
            path = root / "scan-summary.log"
            path.symlink_to(outside)

            worker_main.trim_file_to_last_bytes(path, 3)

            self.assertEqual(outside.read_text(encoding="utf-8"), "0123456789")

    def test_trim_file_to_last_bytes_does_not_follow_symlink_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside-summary.log"
            outside.write_text("0123456789", encoding="utf-8")
            path = root / "scan-summary.log"
            path.symlink_to(outside)

            with patch.object(worker_main, "regular_log_file", return_value=True):
                worker_main.trim_file_to_last_bytes(path, 3)

            self.assertEqual(outside.read_text(encoding="utf-8"), "0123456789")

    def test_remove_checkout_dir_does_not_chmod_symlink_target_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "checkout"
            locked = checkout / "locked"
            locked.mkdir(parents=True)
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            outside.chmod(0o600)
            link = locked / "link-to-outside"
            link.symlink_to(outside)
            locked.chmod(0o500)
            before_mode = stat.S_IMODE(outside.stat().st_mode)

            worker_main.remove_checkout_dir(checkout)

            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before_mode)
            self.assertFalse(checkout.exists())

    def test_clone_repository_uses_shallow_mirror_cache_for_commit_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source, first_commit, second_commit = self.make_git_repo(root)
            work_dir = root / "work"
            first_checkout = work_dir / "job_1"
            second_checkout = work_dir / "job_2"
            job = {
                "repo": "owner/repo",
                "clone_url": str(source),
                "branch": "master",
                "commit": first_commit,
            }

            with patch.dict("os.environ", {"PULLWISE_ALLOW_LOCAL_CLONE_URLS": "1"}), patch.object(
                worker_main, "run_git_command", wraps=worker_main.run_git_command
            ) as run_git:
                resolved_first = worker_main.clone_repository(job, first_checkout)
                resolved_second = worker_main.clone_repository({**job, "commit": second_commit}, second_checkout)

            self.assertEqual(resolved_first, first_commit)
            self.assertEqual(resolved_second, second_commit)
            self.assertEqual((first_checkout / "app.txt").read_text(encoding="utf-8"), "one\n")
            self.assertEqual((second_checkout / "app.txt").read_text(encoding="utf-8"), "two\n")
            mirror_dirs = list((work_dir / ".pullwise-repo-cache").glob("*.git"))
            self.assertEqual(len(mirror_dirs), 1)
            self.assertTrue((mirror_dirs[0] / "shallow").is_file())
            commands = [call.args[0] for call in run_git.call_args_list]
            first_ref = f"refs/pullwise/commits/{hashlib.sha256(first_commit.encode('utf-8')).hexdigest()[:24]}"
            self.assertIn(["git", "clone", "--shared", "--no-checkout", str(mirror_dirs[0]), str(first_checkout)], commands)
            self.assertIn(
                ["git", "-C", str(mirror_dirs[0]), "fetch", "--depth", "1", "origin", f"{first_commit}:{first_ref}"],
                commands,
            )
            self.assertNotIn(["git", "clone", str(source), str(first_checkout)], commands)

    def test_clone_repository_rejects_symlinked_mirror_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source, first_commit, _second_commit = self.make_git_repo(root)
            work_dir = root / "work"
            work_dir.mkdir()
            outside_cache = root / "outside-cache"
            outside_cache.mkdir()
            (work_dir / ".pullwise-repo-cache").symlink_to(outside_cache, target_is_directory=True)
            checkout = work_dir / "job_1"
            job = {
                "repo": "owner/repo",
                "clone_url": str(source),
                "branch": "master",
                "commit": first_commit,
            }

            with patch.dict("os.environ", {"PULLWISE_ALLOW_LOCAL_CLONE_URLS": "1"}), patch.object(
                worker_main, "run_git_command"
            ) as run_git:
                with self.assertRaisesRegex(RuntimeError, "cache root must not be a symlink"):
                    worker_main.clone_repository(job, checkout)

            run_git.assert_not_called()
            self.assertEqual(list(outside_cache.iterdir()), [])

    def test_clone_repository_rejects_symlinked_mirror_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source, first_commit, _second_commit = self.make_git_repo(root)
            work_dir = root / "work"
            work_dir.mkdir()
            checkout = work_dir / "job_1"
            job = {
                "repo": "owner/repo",
                "clone_url": str(source),
                "branch": "master",
                "commit": first_commit,
            }
            outside_mirror = root / "outside-mirror.git"
            outside_mirror.mkdir()
            mirror_dir = worker_main.repository_mirror_dir(work_dir, job, str(source))
            mirror_dir.parent.mkdir()
            mirror_dir.symlink_to(outside_mirror, target_is_directory=True)

            with patch.dict("os.environ", {"PULLWISE_ALLOW_LOCAL_CLONE_URLS": "1"}), patch.object(
                worker_main, "run_git_command"
            ) as run_git:
                with self.assertRaisesRegex(RuntimeError, "mirror directory must not be a symlink"):
                    worker_main.clone_repository(job, checkout)

            run_git.assert_not_called()
            self.assertEqual(list(outside_mirror.iterdir()), [])

    def test_clone_repository_rejects_untrusted_clone_urls_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "work" / "job_1"
            local_job = {
                "repo": "owner/repo",
                "clone_url": str(root / "source"),
                "branch": "main",
                "commit": "pending",
            }
            mismatched_repo_job = {
                "repo": "owner/repo",
                "clone_url": "https://github.com/other/repo.git",
                "branch": "main",
                "commit": "pending",
            }

            with patch.object(worker_main, "run_git_command") as run_git:
                with self.assertRaisesRegex(RuntimeError, "HTTP\\(S\\) GitHub URL"):
                    worker_main.clone_repository(local_job, checkout)
                with self.assertRaisesRegex(RuntimeError, "path does not match"):
                    worker_main.clone_repository(mismatched_repo_job, checkout)

            run_git.assert_not_called()

    def test_clone_repository_rejects_http_clone_url_by_default_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir) / "work" / "job_1"
            job = {
                "repo": "owner/repo",
                "clone_url": "http://github.com/owner/repo.git",
                "branch": "main",
                "commit": "pending",
                "clone_token": {"repo": "owner/repo", "token": "ghs_secret"},
            }

            with patch.object(worker_main, "run_git_command") as run_git:
                with self.assertRaisesRegex(RuntimeError, "must use HTTPS"):
                    worker_main.clone_repository(job, checkout)

            run_git.assert_not_called()

    def test_clone_repository_rejects_invalid_git_ref_inputs_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "work" / "job_1"
            invalid_branch_job = {
                "repo": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "branch": "main:refs/heads/owned",
                "commit": "pending",
            }
            invalid_commit_job = {
                "repo": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
                "branch": "main",
                "commit": "not-a-sha",
            }

            with patch.object(worker_main, "run_git_command") as run_git:
                with self.assertRaisesRegex(RuntimeError, "branch name is invalid"):
                    worker_main.clone_repository(invalid_branch_job, checkout)
                with self.assertRaisesRegex(RuntimeError, "40-character SHA"):
                    worker_main.clone_repository(invalid_commit_job, checkout)

            run_git.assert_not_called()

    def test_clone_repository_checks_git_tree_limits_before_materializing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source, first_commit, _second_commit = self.make_git_repo(root)
            checkout = root / "work" / "job_1"
            job = {
                "repo": "owner/repo",
                "clone_url": str(source),
                "branch": "master",
                "commit": first_commit,
            }
            config = SimpleNamespace(max_repo_files=0, max_repo_bytes=1, provider="codex")

            with patch.dict("os.environ", {"PULLWISE_ALLOW_LOCAL_CLONE_URLS": "1"}), patch.object(
                worker_main,
                "clone_checkout_from_mirror",
            ) as checkout_from_mirror:
                with self.assertRaises(worker_main.RepositoryTooLargeError):
                    worker_main.clone_repository(job, checkout, limits_config=config)

            checkout_from_mirror.assert_not_called()
            self.assertFalse(checkout.exists())

    def test_git_logging_redacts_url_credentials(self) -> None:
        self.assertEqual(
            worker_main.git_log_safe_arg("https://user:secret@example.com/owner/repo.git?token=ignored"),
            "https://example.com/owner/repo.git",
        )
        self.assertNotIn(
            "secret",
            worker_main.git_log_safe_arg("fatal: https://user:secret@example.com/owner/repo.git failed"),
        )
        self.assertNotIn(
            "token=ignored",
            worker_main.git_log_safe_arg("fatal: https://user:secret@example.com/owner/repo.git?token=ignored failed"),
        )
        command = worker_main.git_log_command(
            ["git", "remote", "set-url", "origin", "https://x-access-token:ghs_secret@example.com/owner/repo.git"]
        )
        self.assertNotIn("ghs_secret", command)
        self.assertIn("https://example.com/owner/repo.git", command)

    def test_git_logging_bounds_and_single_lines_arguments(self) -> None:
        text = worker_main.git_log_safe_arg(
            f"first line https://x-access-token:ghs_secret@example.com/owner/repo.git\nsecond line {'x' * 2000}"
        )

        self.assertNotIn("\n", text)
        self.assertNotIn("ghs_secret", text)
        self.assertLessEqual(len(text), 1000)

    def test_git_auth_env_rejects_multiline_clone_token(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Clone token is invalid"):
            worker_main.git_auth_env(
                {"repo": "owner/repo", "token": "ghs_good\r\nHeader: injected"},
                "https://github.com/owner/repo.git",
                "owner/repo",
            )

    def test_resolve_git_head_uses_logged_git_capture(self) -> None:
        checkout = Path("/tmp/pullwise-checkout")
        stdout = "ABCDEFabcdef1234567890abcdefABCDEF123456\n"

        with patch.object(worker_main, "run_git_capture", return_value=stdout) as capture:
            commit = worker_main.resolve_git_head(checkout)

        self.assertEqual(commit, "abcdefabcdef1234567890abcdefabcdef123456")
        capture.assert_called_once_with(["git", "-C", str(checkout), "rev-parse", "HEAD"], phase="resolve-head")

    def test_run_git_capture_bounds_stdout_without_pipe(self) -> None:
        git_stdout = "ABCDEFabcdef1234567890abcdefABCDEF123456\n"

        def fake_run(_command: list[str], **kwargs: object) -> worker_main.subprocess.CompletedProcess:
            stdout_file = kwargs["stdout"]
            stdout_file.write(git_stdout.encode("utf-8"))
            stdout_file.write(b"x" * (2 * 1024 * 1024))
            stdout_file.flush()
            return worker_main.subprocess.CompletedProcess(["git"], 0)

        with patch.object(worker_main.subprocess, "run", side_effect=fake_run) as run, patch.dict(
            worker_main.os.environ,
            {"PULLWISE_GIT_OUTPUT_MAX_BYTES": "1024"},
            clear=False,
        ):
            output = worker_main.run_git_capture(["git", "rev-parse", "HEAD"], phase="resolve-head")

        self.assertTrue(output.startswith(git_stdout))
        self.assertEqual(len(output.encode("utf-8")), 1024)
        self.assertIn("stdout", run.call_args.kwargs)
        self.assertIn("stderr", run.call_args.kwargs)
        self.assertIsNot(run.call_args.kwargs["stdout"], worker_main.subprocess.PIPE)
        self.assertIsNot(run.call_args.kwargs["stderr"], worker_main.subprocess.PIPE)
        self.assertNotIn("text", run.call_args.kwargs)

    def test_run_git_capture_bounds_failure_output(self) -> None:
        def fake_run(_command: list[str], **kwargs: object) -> worker_main.subprocess.CompletedProcess:
            stderr_file = kwargs["stderr"]
            stderr_file.write(b"fatal: first line\n")
            stderr_file.write(b"x" * (2 * 1024 * 1024))
            stderr_file.flush()
            return worker_main.subprocess.CompletedProcess(["git"], 128)

        with patch.object(worker_main.subprocess, "run", side_effect=fake_run), patch.dict(
            worker_main.os.environ,
            {"PULLWISE_GIT_OUTPUT_MAX_BYTES": "1024"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "git fetch failed: fatal: first line") as raised:
                worker_main.run_git_capture(["git", "fetch"], phase="fetch")

        self.assertLessEqual(len(str(raised.exception)), 420)

    def test_worker_logs_dry_run_prints_journal_and_scan_summary_commands(self) -> None:
        config = SimpleNamespace(
            service_name="pullwise-worker-wk_1",
            log_dir=Path("/var/log/pullwise-worker/wk_1"),
        )
        output = io.StringIO()

        with patch("sys.stdout", output):
            code = worker_main.worker_logs(config, lines=5, dry_run=True)

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("journalctl -u pullwise-worker-wk_1 -n 5 --no-pager", text)
        self.assertIn("tail -n 5", text)
        self.assertIn("pullwise-worker", text)
        self.assertIn("scan-summary.log", text)

    def test_worker_logs_rejects_unsafe_service_name_before_journalctl(self) -> None:
        config = SimpleNamespace(
            service_name="pullwise-worker/../../evil",
            log_dir=Path("/var/log/pullwise-worker/wk_1"),
        )

        with patch.object(worker_main.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.worker_logs(config, lines=5)

        run.assert_not_called()

    def test_service_action_rejects_unsafe_service_name_before_dependency_check(self) -> None:
        config = SimpleNamespace(service_name="pullwise-worker/../../evil")

        with patch.object(worker_main, "install_ubuntu_2204_dependencies") as install:
            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.service_action("restart", config=config)

        install.assert_not_called()

    def test_install_nodesource_streams_key_without_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_dir = root / "etc" / "apt" / "sources.list.d"
            source_dir.mkdir(parents=True)
            real_path = Path
            captured = {}

            def mapped_path(value: object) -> Path:
                text = str(value)
                if text == "/etc/apt/keyrings":
                    return root / "etc" / "apt" / "keyrings"
                if text == "/etc/apt/sources.list.d/nodesource.list":
                    return source_dir / "nodesource.list"
                return real_path(text)

            def fake_run(command: list[str], **kwargs: object) -> worker_main.subprocess.CompletedProcess:
                if command[0] == "curl":
                    stdout_file = kwargs["stdout"]
                    stdout_file.write(b"nodesource-key")
                    stdout_file.flush()
                    return worker_main.subprocess.CompletedProcess(command, 0)
                if command[0] == "gpg":
                    stdin_file = kwargs["stdin"]
                    captured["gpg_input"] = stdin_file.read()
                    real_path(command[command.index("-o") + 1]).write_bytes(b"dearmored")
                    return worker_main.subprocess.CompletedProcess(command, 0)
                if command[0] == "apt-get":
                    return worker_main.subprocess.CompletedProcess(command, 0)
                raise AssertionError(command)

            with patch.object(worker_main, "Path", side_effect=mapped_path), patch.object(
                worker_main.subprocess,
                "run",
                side_effect=fake_run,
            ) as run, patch.object(worker_main, "node20_available", return_value=True), patch.object(
                worker_main,
                "npm_available",
                return_value=True,
            ):
                ok, detail = worker_main.install_nodesource_nodejs()

        self.assertTrue(ok)
        self.assertEqual(detail, "installed NodeSource Node.js 22.x")
        self.assertEqual(captured["gpg_input"], b"nodesource-key")
        curl_call = next(call for call in run.call_args_list if call.args[0][0] == "curl")
        gpg_call = next(call for call in run.call_args_list if call.args[0][0] == "gpg")
        self.assertIsNot(curl_call.kwargs["stdout"], worker_main.subprocess.PIPE)
        self.assertIn("stdin", gpg_call.kwargs)
        self.assertNotIn("input", gpg_call.kwargs)

    def test_install_nodesource_rejects_oversized_key_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_dir = root / "etc" / "apt" / "sources.list.d"
            source_dir.mkdir(parents=True)
            real_path = Path

            def mapped_path(value: object) -> Path:
                text = str(value)
                if text == "/etc/apt/keyrings":
                    return root / "etc" / "apt" / "keyrings"
                if text == "/etc/apt/sources.list.d/nodesource.list":
                    return source_dir / "nodesource.list"
                return real_path(text)

            def fake_run(command: list[str], **kwargs: object) -> worker_main.subprocess.CompletedProcess:
                if command[0] == "curl":
                    stdout_file = kwargs["stdout"]
                    stdout_file.write(b"x" * 2048)
                    stdout_file.flush()
                    return worker_main.subprocess.CompletedProcess(command, 0)
                raise AssertionError("gpg should not run for oversized key")

            with patch.object(worker_main, "Path", side_effect=mapped_path), patch.object(
                worker_main.subprocess,
                "run",
                side_effect=fake_run,
            ), patch.dict(
                worker_main.os.environ,
                {"PULLWISE_NODESOURCE_KEY_MAX_BYTES": "1024"},
                clear=False,
            ):
                ok, detail = worker_main.install_nodesource_nodejs()

        self.assertFalse(ok)
        self.assertEqual(detail, "NodeSource key response too large")

    def test_scan_summary_write_rejects_symlinked_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir = root / "logs"
            log_dir.mkdir()
            outside = root / "outside-summary.log"
            outside.write_text("outside\n", encoding="utf-8")
            (log_dir / "scan-summary.log").symlink_to(outside)
            config = SimpleNamespace(log_dir=log_dir, scan_summary_log_max_bytes=1024, worker_token="secret-token")

            with self.assertRaises(OSError):
                worker_main.write_scan_progress_summary(config, "job_1", "ai", 80, "msg")

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_worker_logs_does_not_read_symlinked_scan_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir = root / "logs"
            log_dir.mkdir()
            outside = root / "outside-summary.log"
            outside.write_text("outside secret\n", encoding="utf-8")
            (log_dir / "scan-summary.log").symlink_to(outside)
            config = SimpleNamespace(service_name="pullwise-worker-wk_1", log_dir=log_dir)
            output = io.StringIO()

            with patch.object(worker_main.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch("sys.stdout", output):
                code = worker_main.worker_logs(config, lines=5)

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("scan summary log not found or empty", text)
            self.assertNotIn("outside secret", text)

    def test_worker_logs_does_not_read_through_symlinked_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside_logs = root / "outside-logs"
            outside_logs.mkdir()
            (outside_logs / "scan-summary.log").write_text("outside secret\n", encoding="utf-8")
            log_dir = root / "logs"
            log_dir.symlink_to(outside_logs, target_is_directory=True)
            config = SimpleNamespace(service_name="pullwise-worker-wk_1", log_dir=log_dir)
            output = io.StringIO()

            with patch.object(worker_main.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch("sys.stdout", output):
                code = worker_main.worker_logs(config, lines=5)

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("scan summary log not found or empty", text)
            self.assertNotIn("outside secret", text)

    def test_tail_text_lines_bounds_scan_summary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "scan-summary.log"
            path.write_text("old secret\n" + ("x" * (2 * 1024 * 1024)) + "\nlast one\nlast two\n", encoding="utf-8")

            with patch.dict(
                worker_main.os.environ,
                {"PULLWISE_WORKER_LOG_TAIL_MAX_BYTES": "4096"},
                clear=False,
            ):
                lines = worker_main.tail_text_lines(path, 2)

        self.assertEqual(lines, ["last one", "last two"])

    def test_lifecycle_watcher_uploads_active_log_session_lines(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def log_stream_lines(self, session_id, lines):
                self.calls.append((session_id, lines))
                return {"ok": True, "accepted": True}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            summary = log_dir / "scan-summary.log"
            summary.write_text("old summary\n", encoding="utf-8")
            config = SimpleNamespace(
                worker_id="wk_1",
                worker_token="pwk_test",
                server_url="https://api.example.com",
                service_name="pullwise-worker-wk_1",
                log_dir=log_dir,
            )
            watcher = worker_main.WorkerLifecycleWatcher(config)
            watcher.client = FakeClient()
            session = {"id": "log_1", "created_at": 1781200000}

            with patch.object(
                worker_main.WorkerJournalLogTailer,
                "collect",
                side_effect=[
                    ([{"source": "worker", "stream": "journal", "timestamp": 1781200001, "line": "journal line"}], "cursor-1"),
                    ([], "cursor-1"),
                ],
            ):
                watcher.handle_log_session(session)
                summary.write_text("old summary\nnew summary\n", encoding="utf-8")
                watcher.handle_log_session(session)

            self.assertEqual(watcher.client.calls[0][0], "log_1")
            self.assertEqual(watcher.client.calls[0][1][0]["line"], "journal line")
            self.assertEqual(watcher.client.calls[1][1][0]["stream"], "scan-summary")
            self.assertEqual(watcher.client.calls[1][1][0]["line"], "new summary")
            self.assertEqual(watcher.log_tailers["log_1"].journal.cursor, "cursor-1")

    def test_lifecycle_watcher_log_session_collection_error_does_not_crash(self) -> None:
        class FakeClient:
            def log_stream_lines(self, session_id, lines):
                del session_id, lines
                raise AssertionError("upload should not run")

        class BrokenTailer:
            def collect(self):
                raise RuntimeError("tailer broke")

        config = SimpleNamespace(
            worker_id="wk_1",
            worker_token="pwk_test",
            server_url="https://api.example.com",
            service_name="pullwise-worker-wk_1",
            log_dir=Path("/tmp"),
        )
        watcher = worker_main.WorkerLifecycleWatcher(config)
        watcher.client = FakeClient()
        watcher.log_tailers["log_1"] = BrokenTailer()

        watcher.handle_log_session({"id": "log_1"})

        self.assertIn("log stream collection failed", watcher.last_error or "")
        self.assertIn("tailer broke", watcher.last_error or "")

    def test_cleanup_logs_ignores_symlinked_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir = root / "logs"
            log_dir.mkdir()
            outside = root / "outside.log"
            outside.write_text("outside", encoding="utf-8")
            symlinked = log_dir / "linked.log"
            symlinked.symlink_to(outside)
            expired = log_dir / "expired.log"
            expired.write_text("expired", encoding="utf-8")
            old_ts = time.time() - 3600
            os.utime(expired, (old_ts, old_ts))
            config = SimpleNamespace(log_dir=log_dir, log_retention_seconds=1, max_log_bytes=1024 * 1024)

            worker_main.cleanup_logs(config)

            self.assertFalse(expired.exists())
            self.assertTrue(symlinked.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_cleanup_logs_rejects_symlinked_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside_logs = root / "outside-logs"
            outside_logs.mkdir()
            expired = outside_logs / "expired.log"
            expired.write_text("expired", encoding="utf-8")
            old_ts = time.time() - 3600
            os.utime(expired, (old_ts, old_ts))
            log_dir = root / "logs"
            log_dir.symlink_to(outside_logs, target_is_directory=True)
            config = SimpleNamespace(log_dir=log_dir, log_retention_seconds=1, max_log_bytes=1)

            worker_main.cleanup_logs(config)

            self.assertTrue(expired.exists())
            self.assertEqual(expired.read_text(encoding="utf-8"), "expired")

    def test_journal_log_tailer_reports_unavailable_once_and_backs_off(self) -> None:
        tailer = worker_main.WorkerJournalLogTailer("pullwise-worker-wk_1", since_timestamp=1781200000)

        with patch.object(
            worker_main.subprocess,
            "run",
            side_effect=worker_main.subprocess.TimeoutExpired(["journalctl"], 15),
        ) as run, patch.dict(
            worker_main.os.environ,
            {
                "PULLWISE_LOG_STREAM_JOURNAL_TIMEOUT_SECONDS": "15",
                "PULLWISE_LOG_STREAM_JOURNAL_RETRY_SECONDS": "60",
            },
            clear=False,
        ), patch.object(
            worker_main.time,
            "time",
            side_effect=[1781200001, 1781200001, 1781200002, 1781200003],
        ):
            first_entries, first_cursor = tailer.collect()
            second_entries, second_cursor = tailer.collect()

        self.assertEqual(first_cursor, "")
        self.assertEqual(second_cursor, "")
        self.assertEqual(len(first_entries), 1)
        self.assertIn("journalctl unavailable", first_entries[0]["line"])
        self.assertEqual(second_entries, [])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_journal_log_tailer_bounds_journalctl_lines(self) -> None:
        tailer = worker_main.WorkerJournalLogTailer("pullwise-worker-wk_1", since_timestamp=1781200000)

        with patch.object(
            worker_main.subprocess,
            "run",
            return_value=worker_main.subprocess.CompletedProcess(["journalctl"], 0, stdout="", stderr=""),
        ) as run, patch.dict(
            worker_main.os.environ,
            {"PULLWISE_LOG_STREAM_JOURNAL_MAX_LINES": "999999"},
            clear=False,
        ):
            entries, cursor = tailer.collect()

        self.assertEqual(entries, [])
        self.assertEqual(cursor, "")
        command = run.call_args.args[0]
        self.assertIn("-n", command)
        self.assertEqual(command[command.index("-n") + 1], "5000")
        self.assertIn("stdout", run.call_args.kwargs)
        self.assertIn("stderr", run.call_args.kwargs)
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("text", run.call_args.kwargs)

    def test_journal_log_tailer_bounds_journalctl_output_bytes(self) -> None:
        tailer = worker_main.WorkerJournalLogTailer("pullwise-worker-wk_1", since_timestamp=1781200000)
        journal_entry = json.dumps({"MESSAGE": "ok", "__CURSOR": "cursor_1"}) + "\n"

        def fake_run(_command: list[str], **kwargs: object) -> worker_main.subprocess.CompletedProcess:
            stdout_file = kwargs["stdout"]
            stdout_file.write(journal_entry.encode("utf-8"))
            stdout_file.write(b"x" * (2 * 1024 * 1024))
            stdout_file.flush()
            return worker_main.subprocess.CompletedProcess(["journalctl"], 0)

        with patch.object(worker_main.subprocess, "run", side_effect=fake_run), patch.dict(
            worker_main.os.environ,
            {"PULLWISE_LOG_STREAM_JOURNAL_MAX_BYTES": "1024"},
            clear=False,
        ):
            entries, cursor = tailer.collect()

        self.assertEqual(cursor, "cursor_1")
        self.assertEqual([entry["line"] for entry in entries], ["ok"])

    def test_graph_verified_review_is_the_only_review_path(self) -> None:
        self.assertFalse(hasattr(worker_main, "run_codex_review"))
        self.assertFalse(hasattr(worker_main, "build_repository_graph_bundle"))
        self.assertFalse(hasattr(worker_main, "apply_review_calibration_decisions"))
        self.assertFalse(hasattr(worker_main, "apply_convergence_gate"))
        self.assertFalse(hasattr(worker_main, "convergence_context_for_job"))
        self.assertFalse(hasattr(worker_main, "reportability_rejection_reason"))

    def test_write_graph_verified_codereview_config_uses_plan_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            cfg.codex_reasoning_effort = "medium"

            worker_main.write_graph_verified_codereview_config(
                cfg,
                root,
                {
                    "contextTimeoutSeconds": 240,
                    "finderMaxParallel": 7,
                    "finderTurnParallel": 4,
                    "finderMaxTurnsPerScan": 5,
                    "finderMaxJobsPerSubagent": 24,
                    "finderTimeoutSeconds": 300,
                    "reproMaxParallel": 3,
                    "reproTimeoutSeconds": 600,
                    "maxRepro": 20,
                    "requireRedGreen": True,
                    "minScoreForRepro": 9,
                },
                "deep",
            )

            payload = json.loads((root / ".codereview" / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["mode"], "deep")
        self.assertNotIn("codegraph", payload)
        self.assertEqual(payload["graph"]["target_shards"], 12)
        self.assertEqual(payload["graph"]["mapper_subagent_limit"], 6)
        self.assertIs(payload["graph"]["codex_tool_extractor"], True)
        self.assertEqual(payload["graph"]["tool_extractor_max_rounds"], 3)
        self.assertEqual(payload["graph"]["tool_extractor_timeout_seconds"], 180)
        self.assertIs(payload["graph"]["codex_census"], False)
        self.assertIs(payload["graph"]["codex_mappers"], False)
        self.assertIs(payload["graph"]["codex_linker"], False)
        self.assertIs(payload["graph"]["codex_graph_audit"], False)
        self.assertEqual(payload["graph"]["map_parallel"], 2)
        self.assertEqual(payload["graph"]["graph_timeout_seconds"], 960)
        self.assertEqual(payload["codex"]["reasoning_effort"], "medium")
        self.assertEqual(payload["codex"]["env"]["CODEX_SQLITE_HOME"], str(root / "home" / ".codex-sqlite"))
        self.assertTrue(payload["context"]["enabled"])
        self.assertEqual(payload["context"]["timeout_seconds"], 240)
        self.assertEqual(payload["finders"]["max_workers"], 6)
        self.assertEqual(payload["finders"]["turn_parallel"], 4)
        self.assertEqual(payload["finders"]["max_turns_per_scan"], 5)
        self.assertEqual(payload["finders"]["max_jobs_per_subagent"], 24)
        self.assertEqual(payload["finders"]["timeout_seconds"], 300)
        self.assertEqual(payload["repro"]["max_workers"], 3)
        self.assertEqual(payload["repro"]["timeout_seconds"], 600)
        self.assertEqual(payload["repro"]["max_repro"], 20)
        self.assertTrue(payload["repro"]["require_red_green"])
        self.assertEqual(payload["candidates"]["max_total_for_reproduction"], 20)
        self.assertEqual(payload["scoring"]["min_score_for_repro"], 9)
        self.assertEqual(payload["scoring"]["always_repro_severities"], ["critical", "high"])

    def test_write_graph_verified_codereview_config_does_not_inherit_repo_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            config_dir = root / ".codereview"
            config_dir.mkdir()
            (config_dir / "config.json").write_text(
                json.dumps(
                    {
                        "codegraph": {"enabled": True},
                        "impact": {"enabled": True},
                        "codex": {"dangerous": "repo-value"},
                        "context": {"repoInjected": True},
                        "finders": {"repoInjected": True},
                        "repro": {"repoInjected": True},
                        "scan": {"repoInjected": True},
                        "scope": {"repoInjected": True},
                        "scoring": {"repoInjected": True},
                    }
                ),
                encoding="utf-8",
            )

            worker_main.write_graph_verified_codereview_config(cfg, root, {"maxRepro": 0}, "standard")

            payload = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))

        self.assertNotIn("codegraph", payload)
        self.assertNotIn("impact", payload)
        for section in ("codex", "context", "finders", "repro", "scan", "scope", "scoring"):
            self.assertNotIn("repoInjected", payload[section])
        self.assertNotIn("dangerous", payload["codex"])

    def test_write_graph_verified_codereview_config_uses_standard_repro_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)

            worker_main.write_graph_verified_codereview_config(cfg, root, {"maxRepro": 0}, "standard")

            payload = json.loads((root / ".codereview" / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["repro"]["max_repro"], 20)
        self.assertEqual(payload["candidates"]["max_total_for_reproduction"], 20)

    def test_write_graph_verified_codereview_config_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            config_dir = root / ".codereview"
            config_dir.mkdir()
            outside = root / "outside-config.json"
            outside.write_text('{"mode": "outside"}\n', encoding="utf-8")
            config_path = config_dir / "config.json"
            config_path.symlink_to(outside)

            worker_main.write_graph_verified_codereview_config(cfg, root, {"maxRepro": 0}, "fast")

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            outside_text = outside.read_text(encoding="utf-8")

            self.assertFalse(config_path.is_symlink())
            self.assertEqual(payload["mode"], "fast")
            self.assertEqual(outside_text, '{"mode": "outside"}\n')

    def test_repository_limit_helpers_tolerate_bad_numeric_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "app.py").write_text("print('ok')\n", encoding="utf-8")
            config = SimpleNamespace(max_repo_files="not-a-number", max_repo_bytes=-5)

            limits = worker_main.repository_limits_metadata(config)
            stats = worker_main.repository_resource_stats(
                checkout,
                limits={"maxFiles": "bad", "maxBytes": object()},
            )
            exceeded = worker_main.repository_limit_exceeded(
                {"fileCount": "bad", "totalBytes": object()},
                {"maxFiles": "bad", "maxBytes": object()},
            )

        self.assertEqual(limits["maxFiles"], worker_main._DEFAULT_MAX_REPO_FILES)
        self.assertEqual(limits["maxBytes"], 1)
        self.assertEqual(stats["fileCount"], 1)
        self.assertEqual(exceeded, [])

    def test_repository_limits_are_bounded_locally(self) -> None:
        base = SimpleNamespace(
            provider="codex",
            provider_chain=["codex"],
            codex_model="gpt-5",
            codex_reasoning_effort="high",
            max_repo_files=10**12,
            max_repo_bytes=10**18,
        )
        job = {
            "job_id": "job_limits",
            "agentConfig": {
                "provider": "codex",
                "codex": {"model": "gpt-5", "reasoningEffort": "high"},
            },
            "repositoryLimits": {"maxFiles": 10**12, "maxBytes": 10**18},
        }

        job_config = worker_main.worker_config_for_job(base, job)
        limits = worker_main.repository_limits_metadata(base)

        self.assertEqual(job_config.max_repo_files, worker_main._MAX_REPO_LIMIT_FILES)
        self.assertEqual(job_config.max_repo_bytes, worker_main._MAX_REPO_LIMIT_BYTES)
        self.assertEqual(limits["maxFiles"], worker_main._MAX_REPO_LIMIT_FILES)
        self.assertEqual(limits["maxBytes"], worker_main._MAX_REPO_LIMIT_BYTES)

    def test_preflight_ignores_symlinked_repository_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            checkout = root / "checkout"
            outside = root / "outside"
            checkout.mkdir()
            outside.mkdir()
            (outside / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite"}, "packageManager": "npm@10.0.0"}),
                encoding="utf-8",
            )
            (outside / "Dockerfile").write_text("COPY missing-file /app/\n", encoding="utf-8")
            (checkout / "README.md").write_text("Run npm run missing-script\n", encoding="utf-8")
            (checkout / "package.json").symlink_to(outside / "package.json")
            (checkout / "Dockerfile").symlink_to(outside / "Dockerfile")

            metadata = worker_main.repository_preflight_metadata(checkout)
            findings = worker_main.run_deterministic_repository_checks({"job_id": "job_symlink"}, checkout)

        self.assertEqual(metadata["manifests"], [])
        self.assertEqual(metadata["packageManagers"], [])
        self.assertEqual(findings, [])

    def test_safe_tool_version_bounds_command_output_without_pipe(self) -> None:
        def fake_run(_command: list[str], **kwargs: object) -> worker_main.subprocess.CompletedProcess:
            stdout_file = kwargs["stdout"]
            stdout_file.write(b"tool 1.0\n")
            stdout_file.write(b"x" * (2 * 1024 * 1024))
            stdout_file.flush()
            return worker_main.subprocess.CompletedProcess(["tool"], 0)

        with patch.object(worker_main.subprocess, "run", side_effect=fake_run) as run:
            result = worker_main.safe_tool_version("tool", ["tool", "--version"])

        self.assertTrue(result["available"])
        self.assertEqual(result["exitCode"], 0)
        self.assertTrue(result["output"].startswith("tool 1.0"))
        self.assertLessEqual(len(result["output"]), 200)
        self.assertIsNot(run.call_args.kwargs["stdout"], worker_main.subprocess.PIPE)
        self.assertIsNot(run.call_args.kwargs["stderr"], worker_main.subprocess.PIPE)
        self.assertNotIn("text", run.call_args.kwargs)

    def test_git_inventory_capture_bounds_output_without_pipe(self) -> None:
        from codereview.inventory import git_inventory as git_inventory_module

        captured = {}

        def fake_run(_command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            captured.update(kwargs)
            stdout_file = kwargs["stdout"]
            stdout_file.write(b"app.py\x00")
            stdout_file.flush()
            return subprocess.CompletedProcess(["git"], 0)

        with patch.object(git_inventory_module.subprocess, "run", side_effect=fake_run):
            output = git_inventory_module._run_git_capture(["git", "ls-files", "-z"], cwd=Path("/tmp"), timeout=30)

        self.assertEqual(output, "app.py\x00")
        self.assertIsNot(captured["stdout"], subprocess.PIPE)
        self.assertIsNot(captured["stderr"], subprocess.PIPE)
        self.assertNotIn("text", captured)

    def test_git_inventory_capture_rejects_oversized_output(self) -> None:
        from codereview.inventory import git_inventory as git_inventory_module

        def fake_run(_command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            stdout_file = kwargs["stdout"]
            stdout_file.write(b"x" * (git_inventory_module.GIT_CAPTURE_MAX_BYTES + 1))
            stdout_file.flush()
            return subprocess.CompletedProcess(["git"], 0)

        with patch.object(git_inventory_module.subprocess, "run", side_effect=fake_run):
            output = git_inventory_module._run_git_capture(["git", "ls-files", "-z"], cwd=Path("/tmp"), timeout=30)

        self.assertIsNone(output)

    def test_preflight_reads_do_not_follow_symlink_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside"
            outside.mkdir()

            package_checkout = root / "package-checkout"
            package_checkout.mkdir()
            outside_package = outside / "package.json"
            outside_package.write_text(
                json.dumps({"scripts": {"build": "vite"}, "packageManager": "npm@10.0.0"}),
                encoding="utf-8",
            )
            package_link = package_checkout / "package.json"
            package_link.symlink_to(outside_package)

            readme_checkout = root / "readme-checkout"
            readme_checkout.mkdir()
            (readme_checkout / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite"}}),
                encoding="utf-8",
            )
            outside_readme = outside / "README.md"
            outside_readme.write_text("Run npm run missing-script\n", encoding="utf-8")
            readme_link = readme_checkout / "README.md"
            readme_link.symlink_to(outside_readme)

            workflow_checkout = root / "workflow-checkout"
            workflows_dir = workflow_checkout / ".github" / "workflows"
            workflows_dir.mkdir(parents=True)
            (workflow_checkout / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite"}}),
                encoding="utf-8",
            )
            outside_workflow = outside / "ci.yml"
            outside_workflow.write_text("run: npm run missing-script\n", encoding="utf-8")
            workflow_link = workflows_dir / "ci.yml"
            workflow_link.symlink_to(outside_workflow)

            docker_checkout = root / "docker-checkout"
            docker_checkout.mkdir()
            outside_dockerfile = outside / "Dockerfile"
            outside_dockerfile.write_text("COPY missing-file /app/\n", encoding="utf-8")
            docker_link = docker_checkout / "Dockerfile"
            docker_link.symlink_to(outside_dockerfile)

            secret_checkout = root / "secret-checkout"
            secret_checkout.mkdir()
            outside_secret = outside / ".env"
            outside_secret.write_text("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234567890\n", encoding="utf-8")
            secret_link = secret_checkout / ".env"
            secret_link.symlink_to(outside_secret)

            original_regular_file = worker_main.repository_regular_file

            def regular_file_after_check(path: Path) -> bool:
                path = Path(path)
                if path in {package_link, readme_link, workflow_link, docker_link, secret_link}:
                    return True
                return original_regular_file(path)

            with patch.object(worker_main, "repository_regular_file", side_effect=regular_file_after_check), patch.object(
                worker_main,
                "first_existing_file",
                return_value=readme_link,
            ), patch.object(
                worker_main,
                "iter_secret_scan_files",
                return_value=[secret_link],
            ):
                package_data = worker_main.read_package_json(package_link)
                package_line = worker_main.package_script_line(package_checkout, "build")
                readme_findings = worker_main.readme_missing_package_script_findings(
                    {"job_id": "job_readme_race"},
                    readme_checkout,
                )
                workflow_findings = worker_main.workflow_missing_package_script_findings(
                    {"job_id": "job_workflow_race"},
                    workflow_checkout,
                )
                docker_findings = worker_main.dockerfile_missing_source_findings(
                    {"job_id": "job_docker_race"},
                    docker_checkout,
                )
                secret_findings = worker_main.committed_secret_findings(
                    {"job_id": "job_secret_race"},
                    secret_checkout,
                )

        self.assertEqual(package_data, {})
        self.assertEqual(package_line, 1)
        self.assertEqual(readme_findings, [])
        self.assertEqual(workflow_findings, [])
        self.assertEqual(docker_findings, [])
        self.assertEqual(secret_findings, [])

    def test_repository_text_reads_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            oversized = root / "README.md"
            oversized.write_text("x" * 11, encoding="utf-8")

            text = worker_main.read_repository_text_file(oversized, max_bytes=10)
            package_data = worker_main.read_package_json(oversized)

        self.assertIsNone(text)
        self.assertEqual(package_data, {})

    def test_dockerfile_scan_skips_ignored_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout = Path(tmp_dir)
            app_dir = checkout / "app"
            ignored_dir = checkout / "node_modules" / "pkg"
            app_dir.mkdir()
            ignored_dir.mkdir(parents=True)
            (app_dir / "Dockerfile").write_text("COPY missing-file /app/\n", encoding="utf-8")
            (ignored_dir / "Dockerfile").write_text("COPY ignored-missing /app/\n", encoding="utf-8")

            dockerfiles = [path.relative_to(checkout).as_posix() for path in worker_main.iter_dockerfiles(checkout)]
            findings = worker_main.dockerfile_missing_source_findings({"job_id": "job_docker_skip"}, checkout)

        self.assertEqual(dockerfiles, ["app/Dockerfile"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["affectedLocations"][0]["file"], "app/Dockerfile")

    def test_worker_wrapper_exports_codex_sqlite_home(self) -> None:
        script = worker_main.worker_wrapper_script(Path("/etc/pullwise-worker/wk/worker.env"))

        self.assertIn('export CODEX_SQLITE_HOME="$SERVICE_HOME/.codex-sqlite"', script)

    def test_provider_process_env_ignores_global_codex_sqlite_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            outside = root / "outside-sqlite"

            with patch.dict(worker_main.os.environ, {"PULLWISE_CODEX_SQLITE_HOME": str(outside)}):
                env = worker_main.provider_process_env(cfg)

        self.assertEqual(env["CODEX_SQLITE_HOME"], str(root / "home" / ".codex-sqlite"))
        self.assertNotEqual(env["CODEX_SQLITE_HOME"], str(outside))

    def test_write_worker_wrapper_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bin_path = root / "pullwise-worker"
            env_path = root / "worker.env"
            outside = root / "outside-wrapper"
            outside.write_text("outside", encoding="utf-8")
            bin_path.symlink_to(outside)

            worker_main.write_worker_wrapper(bin_path, env_path)

            self.assertFalse(bin_path.is_symlink())
            self.assertIn("python3.10", bin_path.read_text(encoding="utf-8"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_worker_wrapper_target_path_is_service_scoped(self) -> None:
        service_name = "pullwise-worker-test"

        self.assertEqual(
            worker_main.worker_wrapper_target_path(Path("/usr/local/bin/pullwise-worker-test"), service_name),
            Path("/usr/local/bin/pullwise-worker-test"),
        )
        with self.assertRaisesRegex(ValueError, "unexpected worker wrapper path"):
            worker_main.worker_wrapper_target_path(Path("/tmp/pullwise-worker-test"), service_name)
        with self.assertRaisesRegex(ValueError, "unexpected worker wrapper path"):
            worker_main.worker_wrapper_target_path(Path("/usr/local/bin/other-worker"), service_name)

    def test_worker_service_unit_target_path_is_service_scoped(self) -> None:
        service_name = "pullwise-worker-test-watcher"

        self.assertEqual(
            worker_main.worker_service_unit_target_path(
                Path("/etc/systemd/system/pullwise-worker-test-watcher.service"),
                service_name,
            ),
            Path("/etc/systemd/system/pullwise-worker-test-watcher.service"),
        )
        with self.assertRaisesRegex(ValueError, "unexpected worker service unit path"):
            worker_main.worker_service_unit_target_path(Path("/tmp/pullwise-worker-test-watcher.service"), service_name)
        with self.assertRaisesRegex(ValueError, "unexpected worker service unit path"):
            worker_main.worker_service_unit_target_path(Path("/etc/systemd/system/other.service"), service_name)

    def test_worker_env_target_paths_are_config_scoped(self) -> None:
        env_path = Path("/etc/pullwise-worker/worker.env")
        backup_path = Path("/etc/pullwise-worker/worker.env.bak")

        self.assertEqual(worker_main.worker_env_target_paths(env_path, backup_path), (env_path, backup_path))
        with self.assertRaisesRegex(ValueError, "outside /etc/pullwise-worker"):
            worker_main.worker_env_target_paths(Path("/tmp/worker.env"), Path("/tmp/worker.env.bak"))
        with self.assertRaisesRegex(ValueError, "backup path"):
            worker_main.worker_env_target_paths(env_path, Path("/etc/pullwise-worker/other.bak"))

    def test_append_missing_env_values_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env_path = root / "worker.env"
            outside = root / "outside.env"
            outside.write_text("PULLWISE_EXISTING=1\n", encoding="utf-8")
            env_path.symlink_to(outside)

            with self.assertRaises(OSError):
                worker_main.append_missing_env_values(env_path, {"PULLWISE_NEW": "1"})

            self.assertTrue(env_path.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "PULLWISE_EXISTING=1\n")

    def test_append_missing_env_values_rejects_invalid_key_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "worker.env"
            env_path.write_text("PULLWISE_EXISTING=1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "environment key is invalid"):
                worker_main.append_missing_env_values(env_path, {"PULLWISE_BAD\nINJECT": "1"})

            self.assertEqual(env_path.read_text(encoding="utf-8"), "PULLWISE_EXISTING=1\n")

    def test_append_missing_env_values_rejects_multiline_value_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "worker.env"
            env_path.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be single-line"):
                worker_main.append_missing_env_values(env_path, {"PULLWISE_NEW": "1\nPULLWISE_OTHER=2"}, dry_run=True)

            self.assertEqual(env_path.read_text(encoding="utf-8"), "")

    def test_copy_text_file_no_follow_rejects_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside.env"
            outside.write_text("PULLWISE_SECRET=1\n", encoding="utf-8")
            source = root / "worker.env"
            source.symlink_to(outside)
            destination = root / "backup.env"

            with self.assertRaises(OSError):
                worker_main.copy_text_file_no_follow(source, destination)

            self.assertFalse(destination.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "PULLWISE_SECRET=1\n")

    def test_copy_text_file_no_follow_replaces_symlink_destination_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "worker.env"
            source.write_text("PULLWISE_EXISTING=1\n", encoding="utf-8")
            outside = root / "outside-backup.env"
            outside.write_text("outside\n", encoding="utf-8")
            destination = root / "backup.env"
            destination.symlink_to(outside)

            worker_main.copy_text_file_no_follow(source, destination)

            self.assertFalse(destination.is_symlink())
            self.assertEqual(destination.read_text(encoding="utf-8"), "PULLWISE_EXISTING=1\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_update_worker_backup_does_not_follow_symlinked_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside.env"
            outside.write_text("PULLWISE_SECRET=1\n", encoding="utf-8")
            env_path = root / "worker.env"
            env_path.symlink_to(outside)
            backup_path = root / "worker.env.bak"
            bin_path = root / "pullwise-worker"
            config = SimpleNamespace(
                service_name="pullwise-worker-test",
                service_user="pw-worker-test",
                service_home=str(root / "home"),
                service_path="/usr/bin",
                worker_env_file=str(env_path),
                worker_env_backup_file=str(backup_path),
                worker_bin_path=str(bin_path),
            )

            with patch.object(worker_main, "install_ubuntu_2204_dependencies", return_value=(True, "ok")), patch.object(
                worker_main,
                "worker_env_target_paths",
                return_value=(env_path, backup_path),
            ), patch.object(
                worker_main,
                "worker_wrapper_target_path",
                return_value=bin_path,
            ), patch.object(worker_main.subprocess, "run") as run:
                status = worker_main.update_worker(config)

            self.assertEqual(status, 1)
            run.assert_not_called()
            self.assertFalse(backup_path.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "PULLWISE_SECRET=1\n")

    def test_update_worker_restarts_service_when_backup_restore_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env_path = root / "worker.env"
            env_path.write_text("PULLWISE_EXISTING=1\n", encoding="utf-8")
            outside_backup = root / "outside-backup.env"
            outside_backup.write_text("PULLWISE_EXISTING=old\n", encoding="utf-8")
            backup_path = root / "worker.env.bak"
            bin_path = root / "pullwise-worker"
            config = SimpleNamespace(
                service_name="pullwise-worker-test",
                service_user="pw-worker-test",
                service_home=str(root / "home"),
                service_path="/usr/bin",
                worker_env_file=str(env_path),
                worker_env_backup_file=str(backup_path),
                worker_bin_path=str(bin_path),
            )
            run_calls = []

            def fake_run(command):
                run_calls.append(command)
                if command == ["systemctl", "stop", "pullwise-worker-test"]:
                    backup_path.unlink()
                    backup_path.symlink_to(outside_backup)
                    return SimpleNamespace(returncode=7)
                return SimpleNamespace(returncode=0)

            with patch.object(worker_main, "install_ubuntu_2204_dependencies", return_value=(True, "ok")), patch.object(
                worker_main,
                "worker_env_target_paths",
                return_value=(env_path, backup_path),
            ), patch.object(
                worker_main,
                "worker_wrapper_target_path",
                return_value=bin_path,
            ), patch.object(worker_main.subprocess, "run", side_effect=fake_run):
                status = worker_main.update_worker(config)

            self.assertEqual(status, 7)
            self.assertIn(["systemctl", "restart", "pullwise-worker-test"], run_calls)
            self.assertTrue(backup_path.is_symlink())
            self.assertEqual(outside_backup.read_text(encoding="utf-8"), "PULLWISE_EXISTING=old\n")

    def test_update_worker_rejects_unexpected_env_path_before_system_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env_path = root / "worker.env"
            env_path.write_text("PULLWISE_EXISTING=1\n", encoding="utf-8")
            config = SimpleNamespace(
                service_name="pullwise-worker-test",
                service_user="pw-worker-test",
                service_home=str(root / "home"),
                service_path="/usr/bin",
                worker_env_file=str(env_path),
                worker_env_backup_file=str(root / "worker.env.bak"),
                worker_bin_path="/usr/local/bin/pullwise-worker-test",
            )

            with patch.object(worker_main, "install_ubuntu_2204_dependencies", return_value=(True, "ok")), patch.object(
                worker_main.subprocess,
                "run",
            ) as run:
                status = worker_main.update_worker(config)

            self.assertEqual(status, 2)
            run.assert_not_called()

    def test_update_worker_rejects_unexpected_wrapper_path_before_system_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env_path = root / "worker.env"
            env_path.write_text("PULLWISE_EXISTING=1\n", encoding="utf-8")
            config = SimpleNamespace(
                service_name="pullwise-worker-test",
                service_user="pw-worker-test",
                service_home=str(root / "home"),
                service_path="/usr/bin",
                worker_env_file=str(env_path),
                worker_env_backup_file=str(root / "worker.env.bak"),
                worker_bin_path=str(root / "not-worker"),
            )

            with patch.object(worker_main, "install_ubuntu_2204_dependencies", return_value=(True, "ok")), patch.object(
                worker_main,
                "worker_env_target_paths",
                return_value=(env_path, Path(config.worker_env_backup_file)),
            ), patch.object(
                worker_main.subprocess,
                "run",
            ) as run:
                status = worker_main.update_worker(config)

            self.assertEqual(status, 2)
            run.assert_not_called()
            self.assertFalse((root / "not-worker").exists())

    def test_ensure_lifecycle_watcher_service_write_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env_path = root / "worker.env"
            env_path.write_text("", encoding="utf-8")
            bin_path = root / "pullwise-worker"
            bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
            service_file = root / "pullwise-worker-watch.service"
            outside = root / "outside.service"
            outside.write_text("outside", encoding="utf-8")
            service_file.symlink_to(outside)
            config = SimpleNamespace(
                worker_env_file=str(env_path),
                worker_bin_path=str(bin_path),
                watcher_service_name="pullwise-worker-watch",
                watcher_service_file=str(service_file),
                watcher_poll_seconds=5,
                service_name="pullwise-worker-test",
            )

            def fake_run(command):
                return SimpleNamespace(returncode=0, args=command)

            with patch.object(worker_main, "install_ubuntu_2204_dependencies", return_value=(True, "ok")), patch.object(
                worker_main,
                "worker_service_unit_target_path",
                return_value=service_file,
            ), patch.object(worker_main.subprocess, "run", side_effect=fake_run):
                status = worker_main.ensure_lifecycle_watcher(config, env_path=env_path, bin_path=bin_path)

            self.assertEqual(status, 0)
            self.assertFalse(service_file.is_symlink())
            self.assertIn("ExecStart=", service_file.read_text(encoding="utf-8"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_ensure_lifecycle_watcher_rejects_unexpected_service_unit_path_before_dependency_check(self) -> None:
        config = SimpleNamespace(
            worker_env_file="/etc/pullwise-worker/worker.env",
            worker_bin_path="/usr/local/bin/pullwise-worker-watch",
            watcher_service_name="pullwise-worker-watch",
            watcher_service_file="/tmp/pullwise-worker-watch.service",
            watcher_poll_seconds=5,
            service_name="pullwise-worker-test",
        )

        with patch.object(worker_main, "install_ubuntu_2204_dependencies") as install:
            status = worker_main.ensure_lifecycle_watcher(config)

        self.assertEqual(status, 2)
        install.assert_not_called()

    def test_watcher_service_unit_rejects_unsafe_service_names(self) -> None:
        config = SimpleNamespace(
            worker_env_file="/etc/pullwise-worker/worker.env",
            worker_bin_path="/usr/local/bin/pullwise-worker",
            watcher_service_name="pullwise-worker/../../watcher",
            service_name="pullwise-worker-test",
        )

        with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
            worker_main.watcher_service_unit(config)

    def test_watcher_service_unit_rejects_unsafe_unit_paths(self) -> None:
        config = SimpleNamespace(
            worker_env_file="/etc/pullwise-worker/worker.env\nExecStart=/bin/sh",
            worker_bin_path="/usr/local/bin/pullwise-worker",
            watcher_service_name="pullwise-worker-watcher",
            service_name="pullwise-worker-test",
        )

        with self.assertRaisesRegex(ValueError, "worker env file path must be single-line"):
            worker_main.watcher_service_unit(config)

        config.worker_env_file = "relative.env"
        with self.assertRaisesRegex(ValueError, "worker env file path must be an absolute non-root path"):
            worker_main.watcher_service_unit(config)

        config.worker_env_file = "/etc/pullwise-worker/worker.env"
        config.worker_bin_path = "/usr/local/bin/pullwise-worker\nEnvironment=BAD=1"
        with self.assertRaisesRegex(ValueError, "worker binary path must be single-line"):
            worker_main.watcher_service_unit(config)

    def test_watcher_service_unit_accepts_absolute_unit_paths(self) -> None:
        config = SimpleNamespace(
            worker_env_file="/etc/pullwise-worker/worker.env",
            worker_bin_path="/usr/local/bin/pullwise-worker",
            watcher_service_name="pullwise-worker-watcher",
            service_name="pullwise-worker-test",
        )

        unit = worker_main.watcher_service_unit(config)

        self.assertIn("EnvironmentFile=/etc/pullwise-worker/worker.env", unit)
        self.assertIn("ExecStart=/usr/local/bin/pullwise-worker watch", unit)

    def test_ensure_lifecycle_watcher_rejects_unsafe_service_name_before_dependency_check(self) -> None:
        config = SimpleNamespace(
            worker_env_file="/etc/pullwise-worker/worker.env",
            worker_bin_path="/usr/local/bin/pullwise-worker",
            watcher_service_name="pullwise-worker/../../watcher",
            watcher_service_file="/etc/systemd/system/pullwise-worker-watch.service",
            watcher_poll_seconds=5,
            service_name="pullwise-worker-test",
        )

        with patch.object(worker_main, "install_ubuntu_2204_dependencies") as install:
            status = worker_main.ensure_lifecycle_watcher(config)

        self.assertEqual(status, 2)
        install.assert_not_called()

    def test_cleanup_expired_failed_checkout_unlinks_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            worker_main.checkout_root_sentinel(work_dir).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            target = root / "outside"
            target.mkdir()
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            checkout = work_dir / "job_old"
            checkout.symlink_to(target, target_is_directory=True)
            marker = worker_main.failed_checkout_marker(checkout)
            marker.write_text("0", encoding="utf-8")
            config = SimpleNamespace(work_dir=work_dir, max_checkout_bytes=1024 * 1024)

            worker_main.cleanup_checkouts(config)

            self.assertFalse(checkout.exists())
            self.assertFalse(checkout.is_symlink())
            self.assertFalse(marker.exists())
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_cleanup_checkouts_unlinks_broken_symlink_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            worker_main.checkout_root_sentinel(work_dir).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            (work_dir / "size.txt").write_text("x", encoding="utf-8")
            checkout = work_dir / "job_broken_link"
            checkout.symlink_to(root / "missing-target", target_is_directory=True)
            config = SimpleNamespace(work_dir=work_dir, max_checkout_bytes=0)

            worker_main.cleanup_checkouts(config)

            self.assertFalse(checkout.exists())
            self.assertFalse(checkout.is_symlink())

    def test_cleanup_checkouts_rejects_symlinked_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside_work = root / "outside-work"
            outside_work.mkdir()
            worker_main.checkout_root_sentinel(outside_work).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            checkout = outside_work / "job_old"
            checkout.mkdir()
            (checkout / "keep.txt").write_text("keep", encoding="utf-8")
            marker = worker_main.failed_checkout_marker(checkout)
            marker.write_text("0", encoding="utf-8")
            work_dir = root / "work"
            work_dir.symlink_to(outside_work, target_is_directory=True)
            config = SimpleNamespace(work_dir=work_dir, max_checkout_bytes=0)

            worker_main.cleanup_checkouts(config)

            self.assertTrue(work_dir.is_symlink())
            self.assertTrue(checkout.is_dir())
            self.assertEqual((checkout / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertTrue(marker.exists())

    def test_checkout_root_sentinel_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            outside = root / "outside-sentinel"
            outside.write_text("pullwise-worker checkout root\n", encoding="utf-8")
            worker_main.checkout_root_sentinel(work_dir).symlink_to(outside)

            owned = worker_main.checkout_root_is_owned(work_dir)

            self.assertFalse(owned)
            self.assertTrue(worker_main.checkout_root_sentinel(work_dir).is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "pullwise-worker checkout root\n")

    def test_checkout_root_is_owned_rejects_symlinked_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside_work = root / "outside-work"
            outside_work.mkdir()
            worker_main.checkout_root_sentinel(outside_work).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            work_dir = root / "work"
            work_dir.symlink_to(outside_work, target_is_directory=True)

            owned = worker_main.checkout_root_is_owned(work_dir)

            self.assertFalse(owned)
            self.assertTrue(work_dir.is_symlink())

    def test_failed_checkout_marker_write_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            outside = root / "outside-marker"
            outside.write_text("outside", encoding="utf-8")
            marker = work_dir / f"job_failed{worker_main._FAILED_CHECKOUT_MARKER_SUFFIX}"
            marker.symlink_to(outside)

            worker_main.write_failed_checkout_marker(marker, 12345)

            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "12345")
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_cleanup_failed_checkout_rejects_symlink_marker_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            worker_main.checkout_root_sentinel(work_dir).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            checkout = work_dir / "job_marker"
            checkout.mkdir()
            (checkout / "keep.txt").write_text("keep", encoding="utf-8")
            outside = root / "outside-marker"
            outside.write_text("0", encoding="utf-8")
            marker = worker_main.failed_checkout_marker(checkout)
            marker.symlink_to(outside)
            config = SimpleNamespace(work_dir=work_dir, max_checkout_bytes=1024 * 1024)

            worker_main.cleanup_checkouts(config)

            self.assertTrue(checkout.exists())
            self.assertFalse(marker.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "0")

    def test_cleanup_failed_checkout_marker_read_does_not_follow_symlink_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            worker_main.checkout_root_sentinel(work_dir).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            checkout = work_dir / "job_marker_race"
            checkout.mkdir()
            (checkout / "keep.txt").write_text("keep", encoding="utf-8")
            outside = root / "outside-marker"
            outside.write_text("0", encoding="utf-8")
            marker = worker_main.failed_checkout_marker(checkout)
            marker.symlink_to(outside)
            config = SimpleNamespace(work_dir=work_dir, max_checkout_bytes=1024 * 1024)

            with patch.object(type(marker), "is_symlink", return_value=False):
                worker_main.cleanup_checkouts(config)

            self.assertTrue(checkout.exists())
            self.assertTrue(marker.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "0")

    def test_cleanup_failed_checkout_ignores_unreadable_marker_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            work_dir = root / "work"
            work_dir.mkdir()
            worker_main.checkout_root_sentinel(work_dir).write_text("pullwise-worker checkout root\n", encoding="utf-8")
            bad_marker = work_dir / f"bad_marker{worker_main._FAILED_CHECKOUT_MARKER_SUFFIX}"
            bad_marker.mkdir()
            checkout = work_dir / "job_expired"
            checkout.mkdir()
            (checkout / "old.txt").write_text("old", encoding="utf-8")
            marker = worker_main.failed_checkout_marker(checkout)
            marker.write_text("0", encoding="utf-8")
            config = SimpleNamespace(work_dir=work_dir, max_checkout_bytes=1024 * 1024)

            worker_main.cleanup_checkouts(config)

            self.assertTrue(bad_marker.exists())
            self.assertFalse(checkout.exists())
            self.assertFalse(marker.exists())

    def test_remote_uninstall_marker_write_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service_home = root / "service-home"
            service_home.mkdir()
            marker = service_home / "uninstall.marker"
            outside = root / "outside-marker"
            outside.write_text("outside", encoding="utf-8")
            marker.symlink_to(outside)
            config = SimpleNamespace(
                service_name="pullwise-worker-wk_123",
                service_home=str(service_home),
                uninstall_marker_file=str(marker),
                worker_id="wk_123",
            )

            written = worker_main.write_remote_uninstall_marker(config)

            self.assertEqual(written, marker)
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "wk_123\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_remote_uninstall_marker_rejects_path_outside_worker_roots(self) -> None:
        config = SimpleNamespace(
            service_name="pullwise-worker-wk_123",
            service_home="/var/lib/pullwise-worker/wk_123",
            uninstall_marker_file="/etc/passwd",
            worker_id="wk_123",
        )

        with self.assertRaisesRegex(ValueError, "outside worker-owned paths"):
            worker_main.remote_uninstall_marker_path(config)

    def test_directory_size_does_not_follow_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            measured = root / "measured"
            outside = root / "outside"
            measured.mkdir()
            outside.mkdir()
            (measured / "small.log").write_text("small", encoding="utf-8")
            large = outside / "large.log"
            large.write_bytes(b"x" * 4096)
            (measured / "linked.log").symlink_to(large)

            size = worker_main.directory_size(measured)

        self.assertEqual(size, len("small"))

    def test_remote_service_home_target_rejects_broad_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            broad_home = root / "var-lib"
            work_dir = broad_home / "pullwise-worker" / "wk_1"
            work_dir.mkdir(parents=True)

            self.assertFalse(worker_main.safe_remote_service_home_target(broad_home, work_dir))

    def test_remote_service_home_target_allows_instance_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service_home = root / "wk_1"
            work_dir = service_home / "work"
            work_dir.mkdir(parents=True)

            self.assertTrue(worker_main.safe_remote_service_home_target(service_home, work_dir))

    def test_worker_instance_owned_path_rejects_unsafe_worker_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "evil"
            target.mkdir()
            config = SimpleNamespace(service_home="", worker_id="evil/../../root")

            self.assertFalse(worker_main.worker_instance_owned_path(target, config))
            self.assertFalse(worker_main.safe_worker_instance_log_target(target, config))

    def test_worker_instance_owned_path_rejects_relative_service_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            relative_home = Path("service-home")
            target = Path.cwd() / relative_home / "logs"
            config = SimpleNamespace(service_home=str(relative_home), worker_id="")

            self.assertFalse(worker_main.worker_instance_owned_path(target, config))

    def test_safe_unlink_rejects_service_name_path_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
            worker_main.safe_unlink(
                Path("/etc/systemd/system/evil.service"),
                service_name="pullwise-worker/../../evil",
            )

    def test_safe_worker_file_unlink_rejects_service_name_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "pullwise-worker"
            target.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.safe_worker_file_unlink(target, root, "pullwise-worker/../../evil")

            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_update_worker_rejects_unsafe_service_name_before_dependency_check(self) -> None:
        config = SimpleNamespace(service_name="pullwise-worker/../../evil")

        with patch.object(worker_main, "install_ubuntu_2204_dependencies") as install:
            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.update_worker(config)

        install.assert_not_called()

    def test_uninstall_worker_rejects_unsafe_service_name_before_dependency_check(self) -> None:
        config = SimpleNamespace(service_name="pullwise-worker/../../evil")

        with patch.object(worker_main, "install_ubuntu_2204_dependencies") as install:
            with self.assertRaisesRegex(ValueError, "unexpected worker service name"):
                worker_main.uninstall_worker(config)

        install.assert_not_called()

    def test_service_user_doctor_command_rejects_unsafe_service_user(self) -> None:
        cfg = SimpleNamespace(
            service_user="pw-worker-../../root",
            service_home="/var/lib/pullwise-worker/wk",
            service_path="/usr/bin",
        )

        with self.assertRaisesRegex(ValueError, "unexpected worker service user"):
            worker_main.service_user_doctor_command(cfg, Path("/usr/local/bin/pullwise-worker-wk"))

    def test_removable_service_user_rejects_unsafe_user(self) -> None:
        self.assertFalse(worker_main.removable_service_user("pw-worker-../../root"))
        self.assertFalse(worker_main.removable_service_user("root"))
        self.assertTrue(worker_main.removable_service_user("pw-worker-wk_1"))

    def test_service_user_doctor_command_exports_codex_sqlite_home(self) -> None:
        cfg = SimpleNamespace(service_user="pw-worker-wk", service_home="/var/lib/pullwise-worker/wk", service_path="/usr/bin")

        command = worker_main.service_user_doctor_command(cfg, Path("/usr/local/bin/pullwise-worker-wk"))

        self.assertIn("CODEX_SQLITE_HOME=/var/lib/pullwise-worker/wk/.codex-sqlite", command)

    def test_codex_login_command_exports_codex_sqlite_home(self) -> None:
        cfg = SimpleNamespace(
            service_user="pw-worker-wk",
            service_home="/var/lib/pullwise-worker/wk",
            service_path="/usr/bin",
            codex_command="/var/lib/pullwise-worker/wk/.codex/bin/codex",
        )

        command = worker_main.codex_login_command(cfg)

        self.assertIn("CODEX_SQLITE_HOME=/var/lib/pullwise-worker/wk/.codex-sqlite", command)

    def test_graph_verified_job_state_is_per_checkout_without_global_mcp_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            first_checkout = root / "checkout_1"
            second_checkout = root / "checkout_2"
            first_checkout.mkdir()
            second_checkout.mkdir()
            review_calls: list[tuple[Path, str, str]] = []
            codereview_main = importlib.import_module("codereview.main")

            def fake_run_review(checkout_dir: Path, *, mode: str, scan_mode: str = "") -> Path:
                review_calls.append((Path(checkout_dir), mode, scan_mode))
                reports = Path(checkout_dir) / ".codereview" / "runs" / f"run_{len(review_calls)}" / "reports"
                reports.mkdir(parents=True, exist_ok=True)
                final_md = reports / "final.md"
                final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
                (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
                (reports / "confirmed.json").write_text("[]", encoding="utf-8")
                (reports / "rejected.json").write_text("[]", encoding="utf-8")
                (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
                (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
                return final_md

            with patch.object(codereview_main, "run_review", side_effect=fake_run_review):
                first_payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    first_checkout,
                )
                second_payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "deep", "scanMode": "FULL-STRICT"}}},
                    second_checkout,
                )

            self.assertEqual(review_calls, [(first_checkout, "fast", "full-cached"), (second_checkout, "deep", "full-strict")])
            self.assertEqual(first_payload["mode"], "fast")
            self.assertEqual(second_payload["mode"], "deep")
            self.assertEqual(second_payload["scanMode"], "full-strict")
            first_config = json.loads((first_checkout / ".codereview" / "config.json").read_text(encoding="utf-8"))
            second_config = json.loads((second_checkout / ".codereview" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(first_config["mode"], "fast")
            self.assertEqual(second_config["mode"], "deep")
            self.assertEqual(second_config["scan"]["mode"], "full-strict")
            self.assertFalse(second_config["graph"]["incremental"])
            self.assertNotEqual(first_checkout, second_checkout)

    def test_readiness_state_marks_codex_ready_without_graph_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            cfg.server_url = "https://api.pullwise.test"
            cfg.allow_insecure_server_url = False
            cfg.provider = "codex"
            cfg.provider_chain = ["codex"]
            cfg.service_path = str(root / "bin")
            cfg.work_dir = root / "work"
            cfg.log_dir = root / "logs"
            cfg.work_dir.mkdir()
            cfg.log_dir.mkdir()
            cfg.codex_command = str(root / "home" / ".codex" / "bin" / "codex")

            with patch.object(
                worker_main,
                "worker_agent_configs_check",
                return_value=(True, "ok", [{"provider": "codex"}]),
            ), patch.object(
                worker_main,
                "subscription_plan_required_providers",
                return_value=["codex"],
            ), patch.object(
                worker_main,
                "provider_process_env",
                return_value={},
            ), patch.object(
                worker_main,
                "command_ok",
                return_value=(True, "ok"),
            ), patch.object(
                worker_main,
                "node_version_check",
                return_value=(True, "v22.0.0"),
            ), patch.object(
                worker_main,
                "codex_ready_check",
                return_value=(True, "ready"),
            ):
                checks, provider_ready, ready_providers = worker_main.worker_readiness_state(cfg)

        self.assertTrue(provider_ready)
        self.assertEqual(ready_providers, ["codex"])
        self.assertFalse(any(name == "graph_verified_mcp" for name, _ok, _detail in checks))

    def test_writable_path_check_rejects_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside"
            outside.mkdir()
            path = root / "work"
            path.symlink_to(outside, target_is_directory=True)

            ok, detail = worker_main.writable_path_check(path)

            self.assertFalse(ok)
            self.assertIn("must not be a symlink", detail)
            self.assertEqual(list(outside.iterdir()), [])

    def test_writable_path_check_does_not_follow_symlinked_test_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            outside = root / "outside"
            outside.write_text("outside", encoding="utf-8")
            test_file = root / ".pullwise-write-test-123"
            test_file.symlink_to(outside)

            with patch.object(worker_main.os, "getpid", return_value=123):
                ok, detail = worker_main.writable_path_check(root)

            self.assertFalse(ok)
            self.assertTrue(detail)
            self.assertTrue(test_file.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_command_ok_bounds_captured_output(self) -> None:
        ok, detail = worker_main.command_ok(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 20000)"]
        )

        self.assertTrue(ok)
        self.assertEqual(len(detail), 500)

    def test_command_ok_bounds_captured_stderr(self) -> None:
        ok, detail = worker_main.command_ok(
            [sys.executable, "-c", "import sys; sys.stderr.write('e' * 20000); raise SystemExit(7)"]
        )

        self.assertFalse(ok)
        self.assertEqual(len(detail), 500)

    def test_worker_home_isolation_rejects_normalized_default_home(self) -> None:
        cfg = SimpleNamespace(service_home=f"{worker_main.DEFAULT_SERVICE_HOME}/.")

        ok, detail = worker_main.worker_provider_home_isolation_check(cfg)

        self.assertFalse(ok)
        self.assertIn("worker-instance-specific", detail)

    def test_worker_home_isolation_allows_instance_subdirectory(self) -> None:
        cfg = SimpleNamespace(service_home=f"{worker_main.DEFAULT_SERVICE_HOME}/wk_1")

        ok, detail = worker_main.worker_provider_home_isolation_check(cfg)

        self.assertTrue(ok)
        self.assertEqual(detail, cfg.service_home)

    def test_codex_readiness_issue_kind_classifies_common_account_failures(self) -> None:
        cases = {
            "401 Unauthorized": "codex_auth_required",
            "access token expired or revoked": "codex_auth_expired",
            "403 - Unauthorized. Contact your ChatGPT administrator for access.": "codex_authorization_failed",
            "Your ChatGPT subscription has expired": "codex_subscription_inactive",
            "insufficient_quota: no credits remaining": "codex_quota_exhausted",
            "Usage limit reached; rate limit exceeded": "codex_quota_exhausted",
            "unknown subcommand 'app-server'": "codex_version_unsupported",
            "codex app-server: unrecognized option '--listen'": "codex_version_unsupported",
        }

        for detail, expected in cases.items():
            with self.subTest(detail=detail):
                self.assertEqual(worker_main.codex_readiness_issue_kind(detail), expected)

    def test_codex_readiness_issue_detail_redacts_worker_token(self) -> None:
        cfg = SimpleNamespace(worker_token="secret-token")

        detail = worker_main.codex_readiness_issue_detail(
            "insufficient_quota for token secret-token",
            cfg,
        )

        self.assertIn("codex_quota_exhausted", detail)
        self.assertIn("[redacted]", detail)
        self.assertNotIn("secret-token", detail)

    def test_codex_ready_check_uses_cached_classified_failure_without_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = config_for(Path(tmp_dir))
            cfg.codex_auth_failure_cooldown_seconds = 3600
            worker_main.clear_codex_auth_failure()
            worker_main.mark_codex_auth_failure(cfg, "insufficient_quota: no credits remaining")
            with patch.object(worker_main, "provider_command_scope_check", return_value=(True, "ok")), patch.object(
                worker_main,
                "provider_process_env",
            ) as provider_env, patch.object(
                worker_main,
                "run_codex_app_server_turn",
            ) as app_server_turn:
                ok, detail = worker_main.codex_ready_check(cfg)

        self.assertFalse(ok)
        self.assertIn("codex_quota_exhausted", detail)
        provider_env.assert_not_called()
        app_server_turn.assert_not_called()
        worker_main.clear_codex_auth_failure()
    def test_codex_ready_check_reports_quota_and_version_failures(self) -> None:
        failures = {
            "insufficient_quota: no credits remaining": "codex_quota_exhausted",
            "codex app-server: unknown subcommand 'app-server'": "codex_version_unsupported",
        }
        for stderr, expected in failures.items():
            with self.subTest(stderr=stderr), tempfile.TemporaryDirectory() as tmp_dir:
                cfg = config_for(Path(tmp_dir))
                with patch.object(worker_main, "provider_command_scope_check", return_value=(True, "ok")), patch.object(
                    worker_main,
                    "provider_process_env",
                    return_value={},
                ), patch.object(
                    worker_main,
                    "run_codex_app_server_turn",
                    return_value=ProcessResult(["codex", "app-server"], str(Path(tmp_dir)), 1, "", stderr, 1),
                ):
                    ok, detail = worker_main.codex_ready_check(cfg)

                self.assertFalse(ok)
                self.assertIn(expected, detail)

    def test_codex_ready_check_marks_expired_auth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = config_for(Path(tmp_dir))
            cfg.codex_auth_failure_cooldown_seconds = 3600
            worker_main.clear_codex_auth_failure()
            with patch.object(worker_main, "provider_command_scope_check", return_value=(True, "ok")), patch.object(
                worker_main,
                "provider_process_env",
                return_value={},
            ), patch.object(
                worker_main,
                "run_codex_app_server_turn",
                return_value=ProcessResult(["codex", "app-server"], str(Path(tmp_dir)), 1, "", "access token expired", 1),
            ):
                ok, detail = worker_main.codex_ready_check(cfg)

        self.assertFalse(ok)
        self.assertIn("codex_auth_expired", detail)
        self.assertGreater(worker_main._codex_auth_failure_until, 0)
    def test_codex_ready_check_clears_auth_failure_state_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            cfg.codex_auth_failure_cooldown_seconds = 0
            worker_main.mark_codex_auth_failure(cfg, "401 Unauthorized")
            self.assertIn("codex_auth_required", worker_main._codex_auth_failure_detail)

            def fake_app_server_turn(**kwargs):
                self.assertEqual(kwargs["prompt"], 'Return only JSON: {"ok": true}')
                kwargs["output_file"].write_text('{"ok": true}', encoding="utf-8")
                return ProcessResult(["codex", "app-server", "turn/start"], str(kwargs["cd"]), 0, '{"ok": true}\n', "", 1)

            with patch.object(worker_main, "provider_command_scope_check", return_value=(True, "ok")), patch.object(
                worker_main,
                "provider_process_env",
                return_value={},
            ), patch.object(worker_main, "run_codex_app_server_turn", side_effect=fake_app_server_turn):
                ok, detail = worker_main.codex_ready_check(cfg)

        self.assertTrue(ok)
        self.assertEqual(detail, "ready")
        self.assertEqual(worker_main._codex_auth_failure_until, 0.0)
        self.assertEqual(worker_main._codex_auth_failure_detail, "")

    def test_codex_ready_check_reports_app_server_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = config_for(Path(tmp_dir))
            with patch.object(worker_main, "provider_command_scope_check", return_value=(True, "ok")), patch.object(
                worker_main,
                "provider_process_env",
                return_value={},
            ), patch.object(
                worker_main,
                "run_codex_app_server_turn",
                return_value=ProcessResult(["codex", "app-server", "turn/start"], str(Path(tmp_dir)), 2, "", "boom", 1),
            ):
                ok, detail = worker_main.codex_ready_check(cfg)

        self.assertFalse(ok)
        self.assertIn("boom", detail)

    def test_refresh_readiness_reports_degraded_instead_of_crashing_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = config_for(Path(tmp_dir))
            cfg.readiness_check_seconds = 60
            worker = worker_main.Worker(cfg)

            with patch.object(worker_main, "worker_readiness_state", side_effect=NameError("missing helper")):
                ready = worker.refresh_readiness_if_due()

        self.assertFalse(ready)
        self.assertEqual(worker._doctor_status, "degraded")
        self.assertFalse(worker._codex_ready)
        self.assertEqual(worker._ready_providers, [])
        self.assertIn("readiness check failed", worker.last_error or "")
        self.assertIn("missing helper", worker.last_error or "")

    def test_run_graph_verified_review_payload_reads_confirmed_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").write_text(json.dumps([{"candidate": {"candidate_id": "c1"}}]), encoding="utf-8")
            (reports / "rejected.json").write_text(json.dumps([{"candidate_id": "r1"}]), encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": [{"candidate": {"candidate_id": "c1"}}]}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 2}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["version"], "graph-verified-code-review/1")
        self.assertEqual(payload["runId"], "run_1")
        self.assertEqual(payload["mode"], "fast")
        self.assertEqual(payload["scope"], "full-repository")
        self.assertNotIn("base", payload)
        self.assertNotIn("head", payload)
        self.assertEqual(payload["confirmedCount"], 1)
        self.assertEqual(payload["rejectedCount"], 1)
        self.assertEqual(payload["blockedCount"], 2)
        self.assertEqual(payload["finalJson"]["confirmed"][0]["candidate"]["candidate_id"], "c1")

    def test_run_graph_verified_review_payload_bounds_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            dirty_run_id = f"run_dirty\nignored-{'x' * 200}"
            reports = root / ".codereview" / "runs" / dirty_run_id / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").write_text("[]", encoding="utf-8")
            (reports / "rejected.json").write_text("[]", encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["runId"], "run_dirty")
        self.assertLessEqual(len(payload["runId"]), 128)

    def test_run_graph_verified_review_payload_bounds_markdown_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("f" * (worker_main.GRAPH_VERIFIED_FINAL_MARKDOWN_MAX_BYTES + 100), encoding="utf-8")
            (reports / "debug.md").write_text(
                "d" * (worker_main.GRAPH_VERIFIED_DEBUG_MARKDOWN_MAX_BYTES + 100),
                encoding="utf-8",
            )
            (reports / "confirmed.json").write_text("[]", encoding="utf-8")
            (reports / "rejected.json").write_text("[]", encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(len(payload["finalMarkdown"]), worker_main.GRAPH_VERIFIED_FINAL_MARKDOWN_MAX_BYTES)
        self.assertEqual(len(payload["debugMarkdown"]), worker_main.GRAPH_VERIFIED_DEBUG_MARKDOWN_MAX_BYTES)

    def test_run_graph_verified_review_payload_rejects_oversized_json_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").write_text("[]", encoding="utf-8")
            (reports / "rejected.json").write_text("[]", encoding="utf-8")
            (reports / "final.json").write_bytes(b'{"confirmed":[],"debug":"' + b"x" * worker_main.GRAPH_VERIFIED_JSON_ARTIFACT_MAX_BYTES + b'"}')
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertNotIn("runId", payload)
        self.assertEqual(payload["finalJson"], {"confirmed": []})
        self.assertIn("final.json", payload["debugMarkdown"])
        self.assertIn("too large", payload["debugMarkdown"])

    def test_run_graph_verified_review_payload_forwards_progress_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").write_text("[]", encoding="utf-8")
            (reports / "rejected.json").write_text("[]", encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")
            events: list[dict] = []

            def fake_run_review(checkout_dir: Path, *, mode: str, scan_mode: str = "", progress=None) -> Path:
                del checkout_dir, mode, scan_mode
                progress({"stage": "graph", "message": "Graph: mapping shards 1/2", "current": 1, "total": 2})
                return final_md

            with patch.object(codereview_main, "run_review", side_effect=fake_run_review):
                worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                    progress_callback=events.append,
                )

        self.assertEqual(events, [{"stage": "graph", "message": "Graph: mapping shards 1/2", "current": 1, "total": 2}])

    def test_run_graph_verified_review_payload_blocks_on_missing_report_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            final_md = reports / "final.md"
            final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
            (reports / "confirmed.json").write_text("[]", encoding="utf-8")
            (reports / "rejected.json").write_text("[]", encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["mode"], "fast")
        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertNotIn("runId", payload)
        self.assertIn("summary.json", payload["debugMarkdown"])
        self.assertIn("failed before confirmation", payload["debugMarkdown"])

    def test_run_graph_verified_review_payload_rejects_symlink_report_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            outside = root / "outside-confirmed.json"
            outside.write_text(json.dumps([{"candidate": {"candidate_id": "outside"}}]), encoding="utf-8")
            final_md = reports / "final.md"
            final_md.write_text("# Full-Repository Graph-Verified Code Review\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").symlink_to(outside)
            (reports / "rejected.json").write_text("[]", encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertIn("must not be a symlink: confirmed.json", payload["debugMarkdown"])

    def test_run_graph_verified_review_payload_read_does_not_follow_symlink_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            reports = root / ".codereview" / "runs" / "run_1" / "reports"
            reports.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            outside_final = outside / "final.md"
            outside_final.write_text("# Outside Final\n", encoding="utf-8")
            outside_debug = outside / "debug.md"
            outside_debug.write_text("# Outside Debug\n", encoding="utf-8")
            outside_confirmed = outside / "confirmed.json"
            outside_confirmed.write_text(json.dumps([{"candidate": {"candidate_id": "outside"}}]), encoding="utf-8")
            outside_rejected = outside / "rejected.json"
            outside_rejected.write_text(json.dumps([{"candidate_id": "outside-rejected"}]), encoding="utf-8")
            outside_final_json = outside / "final.json"
            outside_final_json.write_text(
                json.dumps({"confirmed": [{"candidate": {"candidate_id": "outside"}}]}),
                encoding="utf-8",
            )
            outside_summary = outside / "summary.json"
            outside_summary.write_text(json.dumps({"reports": {"blocked": 7}}), encoding="utf-8")
            final_md = reports / "final.md"
            final_md.symlink_to(outside_final)
            (reports / "debug.md").symlink_to(outside_debug)
            (reports / "confirmed.json").symlink_to(outside_confirmed)
            (reports / "rejected.json").symlink_to(outside_rejected)
            (reports / "final.json").symlink_to(outside_final_json)
            (reports / "summary.json").symlink_to(outside_summary)
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md), patch.object(
                worker_main,
                "graph_verified_report_artifact_error",
                return_value="",
            ), patch.object(worker_main, "graph_verified_regular_file", return_value=True):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["rejectedCount"], 0)
        self.assertEqual(payload["blockedCount"], 0)
        self.assertEqual(payload["finalMarkdown"], "")
        self.assertEqual(payload["debugMarkdown"], "")
        self.assertEqual(payload["finalJson"], {"confirmed": []})
        self.assertEqual(payload["summary"], {})

    def test_run_graph_verified_review_payload_rejects_report_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            outside_reports = root / "outside" / "reports"
            outside_reports.mkdir(parents=True)
            final_md = outside_reports / "final.md"
            final_md.write_text("# Outside Report\n", encoding="utf-8")
            (outside_reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (outside_reports / "confirmed.json").write_text(json.dumps([{"candidate": {"candidate_id": "outside"}}]), encoding="utf-8")
            (outside_reports / "rejected.json").write_text("[]", encoding="utf-8")
            (outside_reports / "final.json").write_text(json.dumps({"confirmed": [{"candidate": {"candidate_id": "outside"}}]}), encoding="utf-8")
            (outside_reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=final_md):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertNotIn("runId", payload)
        self.assertIn("outside the checkout run directory", payload["debugMarkdown"])

    def test_run_graph_verified_review_payload_rejects_symlinked_reports_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            outside_reports = root / "outside-reports"
            outside_reports.mkdir()
            final_md = outside_reports / "final.md"
            final_md.write_text("# Outside Report\n", encoding="utf-8")
            (outside_reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (outside_reports / "confirmed.json").write_text(json.dumps([{"candidate": {"candidate_id": "outside"}}]), encoding="utf-8")
            (outside_reports / "rejected.json").write_text("[]", encoding="utf-8")
            (outside_reports / "final.json").write_text(json.dumps({"confirmed": [{"candidate": {"candidate_id": "outside"}}]}), encoding="utf-8")
            (outside_reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
            run_dir = root / ".codereview" / "runs" / "run_1"
            run_dir.mkdir(parents=True)
            (run_dir / "reports").symlink_to(outside_reports, target_is_directory=True)
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(codereview_main, "run_review", return_value=run_dir / "reports" / "final.md"):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                )

        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertNotIn("runId", payload)
        self.assertIn("outside the checkout run directory", payload["debugMarkdown"])

    def test_run_graph_verified_review_payload_blocks_on_review_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            codereview_main = importlib.import_module("codereview.main")
            with patch.object(codereview_main, "run_review", side_effect=RuntimeError("failed with secret-token")):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "invalid"}}},
                    root,
                )

        self.assertEqual(payload["version"], "graph-verified-code-review/1")
        self.assertEqual(payload["mode"], "standard")
        self.assertEqual(payload["scope"], "full-repository")
        self.assertNotIn("base", payload)
        self.assertNotIn("head", payload)
        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertEqual(payload["finalJson"], {"confirmed": []})
        self.assertNotIn("secret-token", payload["debugMarkdown"])
        self.assertIn("[redacted]", payload["debugMarkdown"])


if __name__ == "__main__":
    unittest.main()
