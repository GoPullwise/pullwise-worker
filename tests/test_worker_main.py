from __future__ import annotations

import concurrent.futures
import gzip
import io
import json
import hashlib
import os
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

from codereview.utils.process import ProcessCancelled, ProcessResult, clear_process_cancel_event, run_process, set_process_cancel_event

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

            def read(self) -> bytes:
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

            def read(self) -> bytes:
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

            def read(self) -> bytes:
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

            def read(self) -> bytes:
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

            def read(self) -> bytes:
                return b'["not", "an", "object"]'

        with patch.object(worker_main.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(worker_main.PullwiseRequestError, "JSON response must be an object"):
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

            def fake_clone(_job: dict, checkout_dir: Path) -> str:
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
            ):
                worker.run_job(job)

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

    def test_resolve_git_head_uses_logged_git_capture(self) -> None:
        checkout = Path("/tmp/pullwise-checkout")
        stdout = "ABCDEFabcdef1234567890abcdefABCDEF123456\n"

        with patch.object(worker_main, "run_git_capture", return_value=stdout) as capture:
            commit = worker_main.resolve_git_head(checkout)

        self.assertEqual(commit, "abcdefabcdef1234567890abcdefabcdef123456")
        capture.assert_called_once_with(["git", "-C", str(checkout), "rev-parse", "HEAD"], phase="resolve-head")

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
        self.assertIs(payload["graph"]["codex_mappers"], False)
        self.assertEqual(payload["graph"]["map_parallel"], 2)
        self.assertEqual(payload["graph"]["graph_timeout_seconds"], 960)
        self.assertEqual(payload["codex"]["reasoning_effort"], "medium")
        self.assertEqual(payload["codex"]["env"]["CODEX_SQLITE_HOME"], str(root / "home" / ".codex-sqlite"))
        self.assertTrue(payload["context"]["enabled"])
        self.assertEqual(payload["context"]["timeout_seconds"], 240)
        self.assertEqual(payload["finders"]["max_workers"], 6)
        self.assertEqual(payload["finders"]["turn_parallel"], 4)
        self.assertEqual(payload["finders"]["timeout_seconds"], 300)
        self.assertEqual(payload["repro"]["max_workers"], 3)
        self.assertEqual(payload["repro"]["timeout_seconds"], 600)
        self.assertEqual(payload["repro"]["max_repro"], 20)
        self.assertTrue(payload["repro"]["require_red_green"])
        self.assertEqual(payload["candidates"]["max_total_for_reproduction"], 20)
        self.assertEqual(payload["scoring"]["min_score_for_repro"], 9)
        self.assertEqual(payload["scoring"]["always_repro_severities"], ["critical", "high"])

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

    def test_worker_wrapper_exports_codex_sqlite_home(self) -> None:
        script = worker_main.worker_wrapper_script(Path("/etc/pullwise-worker/wk/worker.env"))

        self.assertIn('export CODEX_SQLITE_HOME="$SERVICE_HOME/.codex-sqlite"', script)

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
                worker_main.subprocess,
                "run",
                side_effect=fake_run,
            ):
                status = worker_main.ensure_lifecycle_watcher(config, env_path=env_path, bin_path=bin_path)

            self.assertEqual(status, 0)
            self.assertFalse(service_file.is_symlink())
            self.assertIn("ExecStart=", service_file.read_text(encoding="utf-8"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

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
            marker = root / "uninstall.marker"
            outside = root / "outside-marker"
            outside.write_text("outside", encoding="utf-8")
            marker.symlink_to(outside)
            config = SimpleNamespace(uninstall_marker_file=str(marker), worker_id="wk_123")

            written = worker_main.write_remote_uninstall_marker(config)

            self.assertEqual(written, marker)
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "wk_123\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

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

    def test_service_user_doctor_command_exports_codex_sqlite_home(self) -> None:
        cfg = SimpleNamespace(service_user="pw-worker-wk", service_home="/var/lib/pullwise-worker/wk", service_path="/usr/bin")

        command = worker_main.service_user_doctor_command(cfg, Path("/usr/local/bin/pullwise-worker-wk"))

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

    def test_codex_ready_check_clears_auth_failure_state_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            cfg.codex_auth_failure_cooldown_seconds = 3600
            worker_main.mark_codex_auth_failure(cfg, "401 Unauthorized")
            self.assertGreater(worker_main._codex_auth_failure_until, 0)

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
