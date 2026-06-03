from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from pullwise_worker.main import (
    Worker,
    WorkerConfig,
    checkout_dir_for_job,
    cleanup_checkouts,
    clone_repository,
    codex_ready_check,
    parse_findings,
    redact_secrets,
    result_checksum,
    run_codex_review,
    run_doctor,
    safe_job_id,
    safe_rmtree,
    service_action,
    summarize,
    uninstall_worker,
    update_worker,
    worker_readiness_checks,
    write_scan_summary,
)


def config() -> WorkerConfig:
    namespace = Namespace(
        server_url="http://server.test",
        worker_token="worker-token",
        worker_id="wk_1",
        max_concurrent_jobs=2,
        poll_seconds=1,
        work_dir=tempfile.mkdtemp(),
        checkout_root=None,
        log_dir=tempfile.mkdtemp(),
        provider="codex",
        codex_command="codex",
        codex_timeout_seconds=60,
    )
    return WorkerConfig(namespace)


class WorkerMainTest(unittest.TestCase):
    def test_parse_findings_accepts_object_payload(self) -> None:
        findings = parse_findings('{"findings":[{"title":"Bug","severity":"high"}]}')

        self.assertEqual(findings, [{"title": "Bug", "severity": "high"}])
        self.assertEqual(summarize(findings)["high"], 1)

    def test_parse_findings_skips_codex_json_event_stream(self) -> None:
        findings = parse_findings(
            '{"event":"review_progress","findings":[]}\n'
            '{"findings":[{"title":"Bug","severity":"high"}]}'
        )

        self.assertEqual(findings, [{"title": "Bug", "severity": "high"}])

    def test_run_job_uploads_progress_result_and_cleans_checkout(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        checkout_dir = Path(worker.config.work_dir) / "job_1"

        with patch("pullwise_worker.main.clone_repository") as clone_repository, \
            patch("pullwise_worker.main.run_codex_review") as run_codex_review, \
            patch("pullwise_worker.main.shutil.rmtree") as rmtree:
            run_codex_review.return_value = (
                [{"title": "Bug", "severity": "high"}],
                {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "review ok",
            )

            worker.run_job({"job_id": "job_1", "attempt": 2, "repo": "acme/api"})

        clone_repository.assert_called_once()
        self.assertEqual(clone_repository.call_args.args[1], checkout_dir)
        run_codex_review.assert_called_once()
        worker.client.result.assert_called_once()
        result_payload = worker.client.result.call_args.args[1]
        self.assertEqual(result_payload["status"], "done")
        self.assertEqual(result_payload["attempt_id"], "wk_1-2")
        self.assertEqual(result_payload["summary"]["high"], 1)
        self.assertEqual(result_payload["result_checksum"], result_checksum({k: v for k, v in result_payload.items() if k != "result_checksum"}))
        self.assertGreaterEqual(worker.client.progress.call_count, 3)
        rmtree.assert_called_with(checkout_dir, ignore_errors=True)

    def test_done_result_upload_timeout_retries_same_payload_without_failed_result(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.result.side_effect = [requests.Timeout("timed out"), None]

        with patch("pullwise_worker.main.clone_repository"), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=([], {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ), \
            patch("pullwise_worker.main.time.sleep"), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job({"job_id": "job_retry", "attempt": 1, "repo": "acme/api"})

        self.assertEqual(worker.client.result.call_count, 2)
        first_payload = worker.client.result.call_args_list[0].args[1]
        second_payload = worker.client.result.call_args_list[1].args[1]
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(second_payload["status"], "done")
        self.assertIsNone(worker.last_error)

    def test_done_result_upload_exhaustion_does_not_submit_failed_result(self) -> None:
        worker = Worker(config())
        worker.config.result_upload_attempts = 2
        worker.client = Mock()
        worker.client.result.side_effect = requests.Timeout("timed out")

        with patch("pullwise_worker.main.clone_repository"), \
            patch(
                "pullwise_worker.main.run_codex_review",
                return_value=([], {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "review ok"),
            ), \
            patch("pullwise_worker.main.time.sleep"), \
            patch("pullwise_worker.main.shutil.rmtree"):
            worker.run_job({"job_id": "job_timeout", "attempt": 1, "repo": "acme/api"})

        self.assertEqual(worker.client.result.call_count, 2)
        statuses = [call.args[1]["status"] for call in worker.client.result.call_args_list]
        self.assertEqual(statuses, ["done", "done"])
        self.assertIn("result upload failed", worker.last_error)

    def test_poll_sleep_backs_off_empty_and_failed_polls_with_jitter(self) -> None:
        worker = Worker(config())
        worker.config.poll_seconds = 5
        worker.config.poll_jitter_seconds = 0
        worker.config.max_backoff_seconds = 20

        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False), 5)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False), 10)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=False), 20)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=1, loop_error=False), 5)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=True), 5)
        self.assertEqual(worker.next_poll_sleep(claimed_jobs=0, loop_error=True), 10)

    def test_once_loop_reports_heartbeat_error_without_crashing(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        worker.client.heartbeat.side_effect = requests.ConnectionError("server down")

        with patch.object(worker, "refresh_readiness_if_due", return_value=True), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.heartbeat.assert_called_once()
        worker.client.claim_many.assert_not_called()
        sleep.assert_not_called()
        self.assertIn("heartbeat failed", worker.last_error)

    def test_once_loop_does_not_claim_when_readiness_checks_fail(self) -> None:
        worker = Worker(config())
        worker.client = Mock()
        checks = [
            ("git", False, "not found"),
            ("codex", True, "codex ok"),
            ("codex_ready", True, "ready"),
        ]

        with patch("pullwise_worker.main.worker_readiness_checks", return_value=(checks, True)), \
            patch("pullwise_worker.main.time.sleep") as sleep:
            worker.run(once=True)

        worker.client.heartbeat.assert_called_once()
        heartbeat_kwargs = worker.client.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "degraded")
        self.assertTrue(heartbeat_kwargs["codex_ready"])
        worker.client.claim_many.assert_not_called()
        sleep.assert_not_called()
        self.assertIn("worker not ready: git: not found", worker.last_error)

    def test_worker_readiness_checks_cover_dependencies_paths_and_disk(self) -> None:
        cfg = config()

        with patch("pullwise_worker.main.command_ok", side_effect=[(False, "git missing"), (True, "codex ok")]), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(True, "ready")), \
            patch("pullwise_worker.main.shutil.disk_usage", return_value=Mock(free=2 * 1024 * 1024 * 1024)):
            checks, codex_ready = worker_readiness_checks(cfg)

        by_name = {name: (ok, detail) for name, ok, detail in checks}
        self.assertFalse(by_name["git"][0])
        self.assertTrue(by_name["codex"][0])
        self.assertTrue(by_name["codex_ready"][0])
        self.assertTrue(by_name["checkout_root"][0])
        self.assertTrue(by_name["log_dir"][0])
        self.assertTrue(by_name["disk_space"][0])
        self.assertTrue(codex_ready)

    def test_clone_repository_uses_short_lived_token(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            clone_repository(
                {
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "clone_url": "https://github.com/acme/api.git",
                    "clone_token": {"token": "short-token"},
                },
                Path("checkout"),
            )

        clone_command = run.call_args_list[0].args[0]
        clone_env = run.call_args_list[0].kwargs["env"]
        self.assertEqual(clone_command[:4], ["git", "clone", "--depth", "1"])
        self.assertEqual(clone_command[-2], "https://github.com/acme/api.git")
        self.assertNotIn("short-token", " ".join(clone_command))
        self.assertNotIn("short-token", " ".join(str(value) for value in clone_env.values()))
        self.assertEqual(clone_env["GIT_CONFIG_KEY_0"], "http.extraHeader")

    def test_clone_repository_reports_git_stderr_on_failure(self) -> None:
        error = subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            output="",
            stderr="remote: Repository not found.\nfatal: Authentication failed for 'https://github.com/acme/api.git/'",
        )
        with patch("pullwise_worker.main.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "git clone failed: remote: Repository not found"):
                clone_repository(
                    {
                        "repo": "acme/api",
                        "branch": "main",
                        "commit": "pending",
                        "clone_url": "https://github.com/acme/api.git",
                    },
                    Path("checkout"),
                )

    def test_run_codex_review_invokes_codex_exec_and_parses_findings(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> Mock:
            schema_path = Path(command[command.index("--output-schema") + 1])
            output_path = Path(command[command.index("--output-last-message") + 1])
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["findings"]["maxItems"], 25)
            output_path.write_text('{"findings":[{"title":"Bug","severity":"medium"}]}', encoding="utf-8")
            return Mock(returncode=0, stdout='{"findings":[]}', stderr="")

        with patch("pullwise_worker.main.subprocess.run", side_effect=fake_run) as run:
            findings, summary, _logs = run_codex_review(config(), {"repo": "acme/api"}, Path("checkout"))

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--config") + 1], 'model_reasoning_effort="xhigh"')
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", command)
        self.assertIn("--output-last-message", command)
        self.assertEqual(findings[0]["title"], "Bug")
        self.assertEqual(summary["medium"], 1)

    def test_job_checkout_dir_refuses_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "work"

            self.assertEqual(checkout_dir_for_job(work_dir, "job_1"), (work_dir / "job_1").resolve())
            for job_id in ("../outside", "nested/job", "nested\\job", ".", "..", ""):
                with self.subTest(job_id=job_id):
                    with self.assertRaises(ValueError):
                        safe_job_id(job_id)
                    with self.assertRaises(ValueError):
                        checkout_dir_for_job(work_dir, job_id)

    def test_redact_secrets_removes_worker_and_clone_tokens(self) -> None:
        cfg = config()
        text = "token worker-token clone https://x-access-token:short-token@github.com/acme/api.git"

        redacted = redact_secrets(text, cfg)

        self.assertNotIn("worker-token", redacted)
        self.assertNotIn("short-token", redacted)
        self.assertIn("[redacted]", redacted)
        self.assertIn("x-access-token:[redacted]@github.com", redacted)

    def test_run_doctor_checks_dependencies_capacity_paths_and_heartbeat(self) -> None:
        cfg = config()

        with patch(
                "pullwise_worker.main.command_ok",
                side_effect=[(True, "git ok"), (True, "codex ok"), (True, "active")],
            ), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(False, "not logged in")), \
            patch("pullwise_worker.main.PullwiseClient") as client_class:
            client_class.return_value.heartbeat.return_value = None
            ok = run_doctor(cfg)

        self.assertFalse(ok)
        client_class.return_value.heartbeat.assert_called_once()
        heartbeat_kwargs = client_class.return_value.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "degraded")
        self.assertFalse(heartbeat_kwargs["codex_ready"])
        self.assertTrue(heartbeat_kwargs["systemd_active"])

    def test_run_doctor_reports_ready_when_codex_probe_succeeds(self) -> None:
        cfg = config()

        with patch("pullwise_worker.main.command_ok", side_effect=[(True, "git ok"), (True, "codex ok"), (True, "active")]), \
            patch("pullwise_worker.main.codex_ready_check", return_value=(True, "ready")), \
            patch("pullwise_worker.main.PullwiseClient") as client_class:
            client_class.return_value.heartbeat.return_value = None
            ok = run_doctor(cfg)

        self.assertTrue(ok)
        heartbeat_kwargs = client_class.return_value.heartbeat.call_args.kwargs
        self.assertEqual(heartbeat_kwargs["doctor_status"], "ok")
        self.assertTrue(heartbeat_kwargs["codex_ready"])

    def test_codex_ready_check_identifies_login_failure(self) -> None:
        cfg = config()
        completed = Mock(returncode=1, stdout="", stderr="not authenticated; run codex login")

        with patch("pullwise_worker.main.subprocess.run", return_value=completed):
            ok, detail = codex_ready_check(cfg)

        self.assertFalse(ok)
        self.assertEqual(detail, "not logged in")

    def test_cleanup_checkouts_removes_expired_failed_retention(self) -> None:
        cfg = config()
        cfg.max_checkout_bytes = 1024 * 1024
        retained = Path(cfg.work_dir) / "retained"
        expired = Path(cfg.work_dir) / "expired"
        retained.mkdir(parents=True)
        expired.mkdir(parents=True)
        (retained / "big.txt").write_text("xx", encoding="utf-8")
        (expired / "file.txt").write_text("x", encoding="utf-8")
        retained.with_suffix(".failed-retain").write_text("9999999999", encoding="utf-8")
        expired.with_suffix(".failed-retain").write_text("1", encoding="utf-8")

        cleanup_checkouts(cfg)

        self.assertFalse(expired.exists())
        self.assertFalse(expired.with_suffix(".failed-retain").exists())
        self.assertTrue(retained.exists())
        self.assertTrue(Path(cfg.work_dir).exists())

    def test_lifecycle_uninstall_dry_run_does_not_remove_files(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            code = uninstall_worker(remove_config=True, remove_logs=True, dry_run=True)

        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_update_dry_run_backs_up_env_and_does_not_run_commands(self) -> None:
        with patch("pullwise_worker.main.subprocess.run") as run:
            code = update_worker(config(), dry_run=True)

        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_update_restores_existing_env_when_upgrade_fails(self) -> None:
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "worker.env"
            backup_file = Path(tmp) / "worker.env.bak"
            env_file.write_text("PULLWISE_WORKER_TOKEN=worker-token\n", encoding="utf-8")
            failed = Mock(returncode=1)
            ok = Mock(returncode=0)

            with patch.dict(
                    "os.environ",
                    {
                        "PULLWISE_WORKER_ENV_FILE": str(env_file),
                        "PULLWISE_WORKER_ENV_BACKUP_FILE": str(backup_file),
                    },
                    clear=False,
                ), \
                patch("pullwise_worker.main.subprocess.run", side_effect=[ok, failed, ok]) as run:
                code = update_worker(cfg)

            self.assertEqual(code, 1)
            self.assertEqual(
                run.call_args_list[1].args[0],
                [
                    "python3",
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "https://github.com/GoPullwise/pullwise-worker/releases/download/v0.1.0/pullwise_worker-0.1.0-py3-none-any.whl",
                ],
            )
            self.assertEqual(env_file.read_text(encoding="utf-8"), "PULLWISE_WORKER_TOKEN=worker-token\n")
            self.assertEqual(backup_file.read_text(encoding="utf-8"), "PULLWISE_WORKER_TOKEN=worker-token\n")

    def test_service_action_supports_systemd_start_stop_status_restart(self) -> None:
        for action in ("start", "stop", "status", "restart"):
            with self.subTest(action=action):
                with patch("pullwise_worker.main.subprocess.run") as run:
                    self.assertEqual(service_action(action, dry_run=True), 0)
                run.assert_not_called()

    def test_write_scan_summary_redacts_tokens(self) -> None:
        cfg = config()
        write_scan_summary(cfg, "job_1", "failed", 12, "worker-token https://x-access-token:repo-token@github.com/acme/api.git")

        summary_log = Path(cfg.log_dir) / "scan-summary.log"
        content = summary_log.read_text(encoding="utf-8")
        self.assertNotIn("worker-token", content)
        self.assertNotIn("repo-token", content)
        self.assertIn("[redacted]", content)

    def test_safe_rmtree_refuses_non_worker_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            allowed = Path(tmp) / "allowed"
            target.mkdir()
            allowed.mkdir()

            with self.assertRaises(ValueError):
                safe_rmtree(target, allowed)
            self.assertTrue(target.exists())

    def test_deploy_assets_cover_install_systemd_logrotate_and_lifecycle(self) -> None:
        deploy_root = Path(__file__).resolve().parents[1] / "deploy"
        expected = [
            "install-worker.sh",
            "worker.env.template",
            "pullwise-worker.service",
            "logrotate.conf",
            "cleanup-checkouts.sh",
            "update-worker.sh",
            "restart-worker.sh",
            "uninstall-worker.sh",
        ]

        for name in expected:
            self.assertTrue((deploy_root / name).exists(), name)
        install_script = (deploy_root / "install-worker.sh").read_text(encoding="utf-8")
        service = (deploy_root / "pullwise-worker.service").read_text(encoding="utf-8")
        self.assertIn("PULLWISE_WORKER_PACKAGE", install_script)
        self.assertIn("https://github.com/GoPullwise/pullwise-worker/releases/download/v0.1.0/pullwise_worker-0.1.0-py3-none-any.whl", install_script)
        self.assertNotIn("pullwise-worker==0.1.0", install_script)
        self.assertIn("PULLWISE_CODEX_PACKAGE", install_script)
        self.assertIn("@openai/codex@0.135.0", install_script)
        self.assertIn("--codex-package", install_script)
        self.assertIn("uname -s", install_script)
        self.assertIn("uname -m", install_script)
        self.assertIn("need_cmd python3", install_script)
        self.assertIn("Python 3.9 or newer", install_script)
        self.assertIn("need_cmd git", install_script)
        self.assertIn("codex login", install_script)
        self.assertIn("PULLWISE_WORKER_TOKEN", install_script)
        self.assertIn("--worker-token-file", install_script)
        self.assertNotIn("--worker-token) WORKER_TOKEN", install_script)
        self.assertNotIn("$(dirname \"$0\")", install_script)
        self.assertNotIn("cp \"$(dirname", install_script)
        self.assertNotIn("pww_", install_script)
        self.assertIn("ReadWritePaths=/var/lib/pullwise-worker /var/log/pullwise-worker", service)


if __name__ == "__main__":
    unittest.main()
