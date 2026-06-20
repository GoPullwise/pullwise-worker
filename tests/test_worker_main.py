from __future__ import annotations

import concurrent.futures
import gzip
import io
import json
import hashlib
import subprocess
import tempfile
import unittest
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
            self.assertIn('"status": "done"', summary_log.read_text(encoding="utf-8"))

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
                                    "finder codex exec failed with exit code 2: "
                                    "error: unexpected argument '--ask-for-approval' found"
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
            self.assertIn("unexpected argument '--ask-for-approval'", payload["error"])
            summary_text = (config.log_dir / "scan-summary.log").read_text(encoding="utf-8")
            self.assertIn('"status": "failed"', summary_text)
            self.assertIn("unexpected argument '--ask-for-approval'", summary_text)

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

            with patch.object(worker_main, "run_git_command", wraps=worker_main.run_git_command) as run_git:
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

    def test_graph_verified_review_is_the_only_review_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = config_for(Path(tmp_dir))

            self.assertTrue(worker_main.graph_verified_review_enabled(cfg, {"agentConfig": {}}))
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

            worker_main.write_graph_verified_codereview_config(
                cfg,
                root,
                {
                    "codegraphCommand": "codegraph",
                    "syncBeforeRun": True,
                    "forceIndexOnFailure": True,
                    "finderMaxParallel": 7,
                    "finderTimeoutSeconds": 240,
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
        self.assertEqual(payload["codegraph"]["command"], "codegraph")
        self.assertTrue(payload["codegraph"]["optional_sync"])
        self.assertTrue(payload["codegraph"]["reindex"])
        self.assertEqual(payload["finders"]["max_workers"], 7)
        self.assertEqual(payload["finders"]["timeout_seconds"], 240)
        self.assertEqual(payload["repro"]["max_workers"], 3)
        self.assertEqual(payload["repro"]["timeout_seconds"], 600)
        self.assertEqual(payload["repro"]["max_repro"], 20)
        self.assertTrue(payload["repro"]["require_red_green"])
        self.assertEqual(payload["scoring"]["min_score_for_repro"], 9)
        self.assertEqual(payload["scoring"]["always_repro_severities"], ["critical", "high"])

    def test_upsert_graph_verified_codex_mcp_config_replaces_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = Path(tmp_dir) / ".codex"
            config_path = codex_home / "config.toml"
            codex_home.mkdir(parents=True)
            config_path.write_text(
                "[mcp_servers.codegraph]\ncommand = \"old\"\nargs = [\"old\"]\n\n[agents]\nmax_threads = 6\n",
                encoding="utf-8",
            )

            worker_main.upsert_graph_verified_codex_mcp_config({"CODEX_HOME": str(codex_home)}, "codegraph")
            content = config_path.read_text(encoding="utf-8")

        self.assertIn("[mcp_servers.codegraph]\ncommand = \"codegraph\"\nargs = [\"serve\", \"--mcp\"]", content)
        self.assertEqual(content.count("[mcp_servers.codegraph]"), 1)
        self.assertIn("[agents]\nmax_threads = 6", content)

    def test_graph_verified_mcp_install_is_cached_but_job_state_is_per_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            first_checkout = root / "checkout_1"
            second_checkout = root / "checkout_2"
            first_checkout.mkdir()
            second_checkout.mkdir()
            review_calls: list[tuple[Path, str, str]] = []
            codereview_main = importlib.import_module("codereview.main")

            def fake_run_review(checkout_dir: Path, *, head_ref: str, mode: str) -> Path:
                review_calls.append((Path(checkout_dir), head_ref, mode))
                reports = Path(checkout_dir) / ".codereview" / "runs" / f"run_{len(review_calls)}" / "reports"
                reports.mkdir(parents=True, exist_ok=True)
                final_md = reports / "final.md"
                final_md.write_text("# Graph-Verified Code Review Report\n", encoding="utf-8")
                (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
                (reports / "confirmed.json").write_text("[]", encoding="utf-8")
                (reports / "rejected.json").write_text("[]", encoding="utf-8")
                (reports / "final.json").write_text(json.dumps({"confirmed": []}), encoding="utf-8")
                (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 0}}), encoding="utf-8")
                return final_md

            worker_main._GRAPH_VERIFIED_MCP_READY.clear()
            try:
                with patch.object(
                    worker_main.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="installed", stderr=""),
                ) as install_run, patch.object(
                    worker_main,
                    "upsert_graph_verified_codex_mcp_config",
                ) as upsert_mcp, patch.object(codereview_main, "run_review", side_effect=fake_run_review):
                    first_payload = worker_main.run_graph_verified_review_payload(
                        cfg,
                        {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                        first_checkout,
                        "HEAD",
                    )
                    second_payload = worker_main.run_graph_verified_review_payload(
                        cfg,
                        {"agentConfig": {"graphVerified": {"mode": "deep"}}},
                        second_checkout,
                        "HEAD",
                    )
            finally:
                worker_main._GRAPH_VERIFIED_MCP_READY.clear()

            self.assertEqual(install_run.call_count, 1)
            self.assertEqual(upsert_mcp.call_count, 1)
            self.assertEqual(review_calls, [(first_checkout, "HEAD", "fast"), (second_checkout, "HEAD", "deep")])
            self.assertEqual(first_payload["mode"], "fast")
            self.assertEqual(second_payload["mode"], "deep")
            first_config = json.loads((first_checkout / ".codereview" / "config.json").read_text(encoding="utf-8"))
            second_config = json.loads((second_checkout / ".codereview" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(first_config["mode"], "fast")
            self.assertEqual(second_config["mode"], "deep")
            self.assertNotEqual(first_checkout, second_checkout)

    def test_readiness_state_ensures_graph_verified_mcp_when_codex_is_ready(self) -> None:
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
            ), patch.object(
                worker_main,
                "ensure_graph_verified_codegraph_codex_mcp",
            ) as ensure_mcp:
                checks, provider_ready, ready_providers = worker_main.worker_readiness_state(cfg)

        ensure_mcp.assert_called_once_with(cfg, Path(cfg.service_home), {})
        self.assertTrue(provider_ready)
        self.assertEqual(ready_providers, ["codex"])
        self.assertIn(("graph_verified_mcp", True, "ready"), checks)

    def test_codex_ready_check_clears_auth_failure_state_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            cfg.codex_auth_failure_cooldown_seconds = 3600
            worker_main.mark_codex_auth_failure(cfg, "401 Unauthorized")
            self.assertGreater(worker_main._codex_auth_failure_until, 0)

            def fake_run(command, **kwargs):
                self.assertEqual(command[-1], "-")
                self.assertEqual(kwargs["input"], 'Return only JSON: {"ok": true}')
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text('{"ok": true}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}\n', stderr="")

            with patch.object(worker_main, "provider_command_scope_check", return_value=(True, "ok")), patch.object(
                worker_main,
                "provider_process_env",
                return_value={},
            ), patch.object(worker_main.subprocess, "run", side_effect=fake_run):
                ok, detail = worker_main.codex_ready_check(cfg)

        self.assertTrue(ok)
        self.assertEqual(detail, "ready")
        self.assertEqual(worker_main._codex_auth_failure_until, 0.0)
        self.assertEqual(worker_main._codex_auth_failure_detail, "")

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
            final_md.write_text("# Graph-Verified Code Review Report\n", encoding="utf-8")
            (reports / "debug.md").write_text("# Debug Report\n", encoding="utf-8")
            (reports / "confirmed.json").write_text(json.dumps([{"candidate": {"candidate_id": "c1"}}]), encoding="utf-8")
            (reports / "rejected.json").write_text(json.dumps([{"candidate_id": "r1"}]), encoding="utf-8")
            (reports / "final.json").write_text(json.dumps({"confirmed": [{"candidate": {"candidate_id": "c1"}}]}), encoding="utf-8")
            (reports / "summary.json").write_text(json.dumps({"reports": {"blocked": 2}}), encoding="utf-8")
            codereview_main = importlib.import_module("codereview.main")

            with patch.object(worker_main, "ensure_graph_verified_codegraph_codex_mcp"), patch.object(
                codereview_main,
                "run_review",
                return_value=final_md,
            ):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "fast"}}},
                    root,
                    "HEAD",
                )

        self.assertEqual(payload["version"], "graph-verified-code-review/1")
        self.assertEqual(payload["runId"], "run_1")
        self.assertEqual(payload["mode"], "fast")
        self.assertEqual(payload["scope"], "repository")
        self.assertNotIn("base", payload)
        self.assertEqual(payload["confirmedCount"], 1)
        self.assertEqual(payload["rejectedCount"], 1)
        self.assertEqual(payload["blockedCount"], 2)
        self.assertEqual(payload["finalJson"]["confirmed"][0]["candidate"]["candidate_id"], "c1")

    def test_run_graph_verified_review_payload_blocks_on_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = config_for(root)
            with patch.object(
                worker_main,
                "ensure_graph_verified_codegraph_codex_mcp",
                side_effect=RuntimeError("failed with secret-token"),
            ):
                payload = worker_main.run_graph_verified_review_payload(
                    cfg,
                    {"agentConfig": {"graphVerified": {"mode": "invalid"}}},
                    root,
                    "abc123",
                )

        self.assertEqual(payload["version"], "graph-verified-code-review/1")
        self.assertEqual(payload["mode"], "standard")
        self.assertEqual(payload["scope"], "repository")
        self.assertNotIn("base", payload)
        self.assertEqual(payload["head"], "abc123")
        self.assertEqual(payload["confirmedCount"], 0)
        self.assertEqual(payload["blockedCount"], 1)
        self.assertEqual(payload["finalJson"], {"confirmed": []})
        self.assertNotIn("secret-token", payload["debugMarkdown"])
        self.assertIn("[redacted]", payload["debugMarkdown"])


if __name__ == "__main__":
    unittest.main()
