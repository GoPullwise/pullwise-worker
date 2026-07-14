from __future__ import annotations

import argparse
import importlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pullwise_worker.main as worker_main
from pullwise_worker import _main_part_07_readiness_doctor as readiness
from pullwise_worker import _main_part_08_lifecycle_cleanup as lifecycle
from pullwise_worker._main_part_08_lifecycle_cleanup import command_worker_has_active_jobs


class WorkerMainContractsTest(unittest.TestCase):
    def test_cleanup_checkouts_loads_private_runtime_constants(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "checkouts"
            config = argparse.Namespace(
                work_dir=work_dir,
                max_checkout_bytes=1024,
                repo_cache_max_bytes=1024,
                repo_cache_ttl_seconds=60,
            )
            with patch.object(lifecycle, "cleanup_repository_mirror_cache"):
                lifecycle.cleanup_checkouts(config)

            self.assertTrue(work_dir.exists())
            self.assertEqual(
                (work_dir / ".pullwise-checkout-root").read_text(encoding="utf-8"),
                "pullwise-worker checkout root\n",
            )

    def test_scan_summary_append_uses_no_follow_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            config = argparse.Namespace(
                log_dir=log_dir,
                worker_token="pww_secret",
                scan_summary_log_max_bytes=1024 * 1024,
            )

            readiness.write_scan_summary(config, "job_1", "done", 10, "")
            readiness.write_scan_progress_summary(config, "job_1", "repo_map", 25)

            lines = (log_dir / "scan-summary.log").read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 2)
        self.assertIn('"status": "done"', lines[0])
        self.assertIn('"status": "progress"', lines[1])

    def test_main_module_does_not_import_retired_review_pipeline(self) -> None:
        imported = set(sys.modules)
        importlib.reload(worker_main)
        new_modules = set(sys.modules) - imported
        self.assertNotIn("pullwise_worker._main_part_04_" + "graph" + "_verified_review", new_modules)
        self.assertFalse(hasattr(worker_main, "run_" + "graph" + "_verified_review_payload"))
        self.assertEqual(worker_main.__all__, ["build_parser", "main"])

    def test_run_command_uses_review_worker_v1(self) -> None:
        calls: list[tuple[str, object]] = []

        class FakeConfig:
            worker_id = "wk_test"
            service_home = "/tmp/pullwise-worker-test"

            def __init__(self, args: argparse.Namespace, *, require_worker_token: bool, validate_server_url: bool) -> None:
                calls.append(("config", (args.command, require_worker_token, validate_server_url)))

        class FakeClient:
            def __init__(self, config: FakeConfig) -> None:
                calls.append(("client", config))

        class FakeWorker:
            def __init__(self, config: FakeConfig, client: FakeClient) -> None:
                calls.append(("worker", (config, client)))

            def run(self, *, once: bool = False) -> None:
                calls.append(("run", once))

        with patch.object(worker_main, "WorkerConfig", FakeConfig), patch.object(
            worker_main, "PullwiseClient", FakeClient
        ), patch.object(worker_main, "ReviewWorkerV1", FakeWorker), patch.object(
            sys, "argv", ["pullwise-worker", "run", "--once"]
        ):
            worker_main.main()

        self.assertEqual(calls[0], ("config", ("run", True, True)))
        self.assertEqual(calls[-1], ("run", True))

    def test_project_package_discovery_excludes_retired_review_package(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        self.assertIn('include = ["pullwise_worker"]', text)
        self.assertNotIn('"code' + 'review"', text)
        self.assertNotIn('"code' + 'review.*"', text)

    def test_lifecycle_watcher_active_job_check_uses_server_running_jobs_only(self) -> None:
        self.assertTrue(command_worker_has_active_jobs({"running_jobs": 1}))
        self.assertFalse(command_worker_has_active_jobs({"running_jobs": 0}))
        self.assertFalse(command_worker_has_active_jobs({"runningJobs": 1}))
        self.assertFalse(command_worker_has_active_jobs({"active_job_ids": ["job_1"]}))

    def test_lifecycle_watcher_reports_successful_uninstall_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            calls = []

            class FakeClient:
                def command_status(self, command_id: str, status: str, *, error: str | None = None) -> None:
                    calls.append((command_id, status, error))

            config = argparse.Namespace(work_dir=Path(tmpdir) / "checkouts", worker_root=Path(tmpdir), worker_id="wk_test", worker_token="pww_test")
            watcher = lifecycle.WorkerLifecycleWatcher.__new__(lifecycle.WorkerLifecycleWatcher)
            watcher.config = config
            watcher.client = FakeClient()
            watcher.last_error = None
            watcher.log_tailers = {}

            with patch.object(lifecycle, "execute_watcher_lifecycle_command", return_value=0):
                handled = watcher.handle_lifecycle_command(
                    {"id": "wcmd_1", "command": "uninstall"},
                    worker_state={"running_jobs": 0},
                )

        self.assertTrue(handled)
        self.assertEqual(calls, [("wcmd_1", "running", None), ("wcmd_1", "succeeded", None)])

    def test_lifecycle_watcher_retries_success_report_without_repeating_cleanup(self) -> None:
        calls = []

        class FakeClient:
            def __init__(self) -> None:
                self.poll_count = 0
                self.success_attempts = 0

            def command_poll(self) -> dict:
                self.poll_count += 1
                return {
                    "command": {"id": "wcmd_retry", "command": "uninstall"},
                    "worker": {"running_jobs": 0},
                }

            def command_status(self, command_id: str, status: str, *, error: str | None = None) -> None:
                calls.append((command_id, status, error))
                if status == "succeeded":
                    self.success_attempts += 1
                    if self.success_attempts == 1:
                        raise lifecycle.PullwiseRequestError("temporary status outage")

        config = argparse.Namespace(worker_id="wk_test", worker_token="pww_test", watcher_poll_seconds=1)
        watcher = lifecycle.WorkerLifecycleWatcher.__new__(lifecycle.WorkerLifecycleWatcher)
        watcher.config = config
        watcher.client = FakeClient()
        watcher.last_error = None
        watcher.log_tailers = {}

        with patch.object(lifecycle, "execute_watcher_lifecycle_command", return_value=0) as execute, patch.object(
            lifecycle.time, "sleep"
        ) as sleep:
            result = watcher.run()

        self.assertEqual(result, 0)
        self.assertEqual(watcher.client.poll_count, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(
            calls,
            [
                ("wcmd_retry", "running", None),
                ("wcmd_retry", "succeeded", None),
                ("wcmd_retry", "succeeded", None),
            ],
        )
        sleep.assert_called_once_with(1)

    def test_lifecycle_watcher_marks_uninstall_cleanup_failed_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            calls = []

            class FakeClient:
                def command_status(self, command_id: str, status: str, *, error: str | None = None) -> None:
                    calls.append((command_id, status, error))

            config = argparse.Namespace(work_dir=Path(tmpdir) / "checkouts", worker_root=Path(tmpdir), worker_id="wk_test", worker_token="pww_test")
            watcher = lifecycle.WorkerLifecycleWatcher.__new__(lifecycle.WorkerLifecycleWatcher)
            watcher.config = config
            watcher.client = FakeClient()
            watcher.last_error = None
            watcher.log_tailers = {}

            with patch.object(lifecycle, "execute_watcher_lifecycle_command", side_effect=RuntimeError("systemd cleanup exploded")):
                handled = watcher.handle_lifecycle_command(
                    {"id": "wcmd_2", "command": "uninstall"},
                    worker_state={"running_jobs": 0},
                )

        self.assertFalse(handled)
        self.assertEqual(calls[0], ("wcmd_2", "running", None))
        self.assertEqual(calls[1][0], "wcmd_2")
        self.assertEqual(calls[1][1], "failed")
        self.assertIn("RuntimeError", calls[1][2])
        self.assertIn("systemd cleanup exploded", calls[1][2])

    def test_watcher_service_unit_is_ordered_before_worker_service(self) -> None:
        config = argparse.Namespace(
            service_name="pullwise-worker-wk_test",
            watcher_service_name="pullwise-worker-wk_test-watcher",
            worker_env_file="/etc/pullwise-worker/wk_test/worker.env",
            worker_bin_path="/usr/local/bin/pullwise-worker-wk_test",
        )

        unit = lifecycle.watcher_service_unit(config)

        self.assertIn("Before=pullwise-worker-wk_test.service", unit)
        self.assertIn("ExecStart=/usr/local/bin/pullwise-worker-wk_test watch", unit)
        self.assertIn("RuntimeDirectory=pullwise-worker-wk_test-watcher", unit)

    def test_manual_uninstall_preserves_watcher_service_by_default(self) -> None:
        config = argparse.Namespace(
            service_name="pullwise-worker-wk_test",
            lifecycle_watcher_enabled=True,
            watcher_service_name="pullwise-worker-wk_test-watcher",
            service_file="/etc/systemd/system/pullwise-worker-wk_test.service",
            watcher_service_file="/etc/systemd/system/pullwise-worker-wk_test-watcher.service",
            worker_env_file="/etc/pullwise-worker/wk_test/worker.env",
            log_dir="/var/log/pullwise-worker/wk_test",
            service_home="/var/lib/pullwise-worker/wk_test",
            worker_bin_path="/usr/local/bin/pullwise-worker-wk_test",
            logrotate_file="/etc/logrotate.d/pullwise-worker-wk_test",
            work_dir="/var/lib/pullwise-worker/wk_test/checkouts",
            service_user="pw-worker-wk-test",
        )
        stdout = io.StringIO()

        with patch.object(lifecycle, "install_ubuntu_2204_dependencies", return_value=(True, "")), redirect_stdout(stdout):
            code = lifecycle.uninstall_worker(config, dry_run=True)

        output = stdout.getvalue()
        self.assertEqual(0, code)
        self.assertIn("systemctl disable pullwise-worker-wk_test", output)
        self.assertNotIn("systemctl disable pullwise-worker-wk_test-watcher", output)
        self.assertNotIn("pullwise-worker-wk_test-watcher.service", output)

    def test_remote_delete_finalizer_removes_watcher_service(self) -> None:
        config = argparse.Namespace(
            service_name="pullwise-worker-wk_test",
            lifecycle_watcher_enabled=True,
            watcher_service_name="pullwise-worker-wk_test-watcher",
            service_file="/etc/systemd/system/pullwise-worker-wk_test.service",
            watcher_service_file="/etc/systemd/system/pullwise-worker-wk_test-watcher.service",
            worker_env_file="/etc/pullwise-worker/wk_test/worker.env",
            log_dir="/var/log/pullwise-worker/wk_test",
            service_home="/var/lib/pullwise-worker/wk_test",
            worker_bin_path="/usr/local/bin/pullwise-worker-wk_test",
            logrotate_file="/etc/logrotate.d/pullwise-worker-wk_test",
            work_dir="/var/lib/pullwise-worker/wk_test/checkouts",
            service_user="pw-worker-wk-test",
        )
        stdout = io.StringIO()

        with patch.object(lifecycle, "install_ubuntu_2204_dependencies", return_value=(True, "")), redirect_stdout(stdout):
            code = lifecycle.uninstall_worker(config, remove_watcher=True, dry_run=True)

        output = stdout.getvalue()
        self.assertEqual(0, code)
        service_file = str(Path("/etc/systemd/system/pullwise-worker-wk_test.service"))
        service_home = str(Path("/var/lib/pullwise-worker/wk_test"))
        watcher_file = str(Path("/etc/systemd/system/pullwise-worker-wk_test-watcher.service"))
        watcher_disable = output.index("systemctl disable pullwise-worker-wk_test-watcher")
        self.assertLess(output.index(f"remove {service_file}"), watcher_disable)
        self.assertLess(output.index(f"remove {service_home}"), watcher_disable)
        self.assertLess(output.index("userdel pw-worker-wk-test"), watcher_disable)
        self.assertLess(watcher_disable, output.index(f"remove {watcher_file}"))

    def test_worker_update_refreshes_latest_scoped_codex_cli_before_python_package(self) -> None:
        worker_root = "/var/lib/pullwise-worker/wk_test/workers/wk_test"
        config = argparse.Namespace(
            service_name="pullwise-worker-wk_test",
            service_user="pw-worker-wk-test",
            service_home="/var/lib/pullwise-worker/wk_test",
            worker_id="wk_test",
            worker_root=worker_root,
            provider_chain=["codex"],
            codex_command=f"{worker_root}/.local/bin/codex",
            codex_home=f"{worker_root}/codex-home",
            codex_sqlite_home=f"{worker_root}/codex-sqlite",
            service_path="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
            worker_env_file="/etc/pullwise-worker/wk_test/worker.env",
            worker_env_backup_file="/etc/pullwise-worker/wk_test/worker.env.bak",
            worker_bin_path="/usr/local/bin/pullwise-worker-wk_test",
            watcher_service_name="pullwise-worker-wk_test-watcher",
            watcher_service_file="/etc/systemd/system/pullwise-worker-wk_test-watcher.service",
            watcher_poll_seconds=5,
        )
        stdout = io.StringIO()

        with patch.object(lifecycle, "install_ubuntu_2204_dependencies", return_value=(True, "")), patch.dict(
            lifecycle.os.environ,
            {
                "PULLWISE_CODEX_RELEASE": "latest",
                "PULLWISE_CODEX_INSTALLER_URL": "https://chatgpt.com/codex/install.sh",
            },
            clear=False,
        ), redirect_stdout(stdout):
            code = lifecycle.update_worker(config, dry_run=True)

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("https://chatgpt.com/codex/install.sh", output)
        self.assertIn(f"CODEX_INSTALL_DIR={worker_root}/.local/codex-versions/update-staged", output)
        self.assertIn("CODEX_RELEASE=latest", output)
        self.assertIn("--release latest", output)
        self.assertIn(f"append env PULLWISE_CODEX_COMMAND={worker_root}/.local/bin/codex", output)
        self.assertLess(output.index("CODEX_RELEASE=latest"), output.index("pip install"))
        self.assertIn("/.venvs/update-", output)

    def test_codex_cli_refresh_stages_before_activation_and_can_restore_previous_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker_root = Path(tmp_dir) / "worker"
            live_command = worker_root / ".local" / "bin" / "codex"
            live_command.parent.mkdir(parents=True)
            live_command.write_text("old-codex\n", encoding="utf-8")
            live_command.chmod(0o755)
            config = argparse.Namespace(
                worker_root=str(worker_root),
                codex_home=str(worker_root / "codex-home"),
                codex_sqlite_home=str(worker_root / "codex-sqlite"),
                service_user="pw-worker-wk-test",
                service_path="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
            )
            settings = {
                "command": str(live_command),
                "install_dir": str(live_command.parent),
                "release": "latest",
                "installer_url": "https://chatgpt.com/codex/install.sh",
            }

            def run(command: list[str], **_kwargs: object) -> argparse.Namespace:
                install_dir = next(
                    (part.split("=", 1)[1] for part in command if part.startswith("CODEX_INSTALL_DIR=")),
                    "",
                )
                if install_dir:
                    staged_command = Path(install_dir) / "codex"
                    staged_command.parent.mkdir(parents=True, exist_ok=True)
                    staged_command.write_text("new-codex\n", encoding="utf-8")
                    staged_command.chmod(0o755)
                return argparse.Namespace(returncode=0)

            with patch.object(lifecycle.subprocess, "run", side_effect=run):
                code = lifecycle.refresh_codex_cli(config, settings)

            backup_path = lifecycle.codex_cli_backup_path(settings)
            self.assertEqual(code, 0)
            self.assertEqual(live_command.read_text(encoding="utf-8"), "new-codex\n")
            self.assertEqual(backup_path.read_text(encoding="utf-8"), "old-codex\n")

            lifecycle.restore_codex_cli_backup(settings)

            self.assertEqual(live_command.read_text(encoding="utf-8"), "old-codex\n")

    def test_worker_package_failure_keeps_previous_python_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker_root = root / "worker"
            old_python = worker_root / ".venv" / "bin" / "python"
            old_python.parent.mkdir(parents=True)
            old_python.write_text("old-python\n", encoding="utf-8")
            env_path = root / "worker.env"
            backup_path = root / "worker.env.bak"
            env_path.write_text(f"PULLWISE_PYTHON_BIN={old_python}\n", encoding="utf-8")
            config = argparse.Namespace(
                service_name="pullwise-worker-wk_test",
                service_user="pw-worker-wk-test",
                service_home=str(root),
                worker_id="wk_test",
                worker_root=str(worker_root),
                provider_chain=[],
                codex_home=str(worker_root / "codex-home"),
                codex_sqlite_home=str(worker_root / "codex-sqlite"),
                service_path="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
                worker_env_file=str(env_path),
                worker_env_backup_file=str(backup_path),
                worker_bin_path=str(root / "pullwise-worker-wk_test"),
                watcher_service_name="pullwise-worker-wk_test-watcher",
                watcher_service_file=str(root / "pullwise-worker-wk_test-watcher.service"),
                watcher_poll_seconds=5,
            )
            calls: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> argparse.Namespace:
                calls.append(command)
                if "pip" in command:
                    return argparse.Namespace(returncode=9)
                return argparse.Namespace(returncode=0)

            with patch.object(lifecycle, "install_ubuntu_2204_dependencies", return_value=(True, "")), patch.object(
                lifecycle, "worker_env_target_paths", side_effect=lambda env, backup: (env, backup)
            ), patch.object(
                lifecycle, "worker_wrapper_target_path", side_effect=lambda path, _service: path
            ), patch.object(lifecycle.subprocess, "run", side_effect=run), patch.dict(
                lifecycle.os.environ,
                {"PULLWISE_PYTHON_BIN": str(old_python)},
                clear=False,
            ):
                code = lifecycle.update_worker(config)

            pip_command = next(command for command in calls if "pip" in command)
            self.assertEqual(code, 9)
            self.assertIn("/.venvs/update-", pip_command[0])
            self.assertNotEqual(pip_command[0], str(old_python))
            self.assertEqual(env_path.read_text(encoding="utf-8"), f"PULLWISE_PYTHON_BIN={old_python}\n")
            self.assertEqual(old_python.read_text(encoding="utf-8"), "old-python\n")

    def test_worker_doctor_failure_rolls_back_staged_python_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker_root = root / "worker"
            old_python = worker_root / ".venv" / "bin" / "python"
            old_python.parent.mkdir(parents=True)
            old_python.write_text("old-python\n", encoding="utf-8")
            env_path = root / "worker.env"
            backup_path = root / "worker.env.bak"
            env_path.write_text(f"PULLWISE_PYTHON_BIN={old_python}\n", encoding="utf-8")
            config = argparse.Namespace(
                service_name="pullwise-worker-wk_test",
                service_user="pw-worker-wk-test",
                service_home=str(root),
                worker_id="wk_test",
                worker_root=str(worker_root),
                provider_chain=[],
                codex_home=str(worker_root / "codex-home"),
                codex_sqlite_home=str(worker_root / "codex-sqlite"),
                service_path="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
                worker_env_file=str(env_path),
                worker_env_backup_file=str(backup_path),
                worker_bin_path=str(root / "pullwise-worker-wk_test"),
                watcher_service_name="pullwise-worker-wk_test-watcher",
                watcher_service_file=str(root / "pullwise-worker-wk_test-watcher.service"),
                watcher_poll_seconds=5,
            )

            def run(command: list[str], **_kwargs: object) -> argparse.Namespace:
                return argparse.Namespace(returncode=7 if command == ["doctor"] else 0)

            with patch.object(lifecycle, "install_ubuntu_2204_dependencies", return_value=(True, "")), patch.object(
                lifecycle, "worker_env_target_paths", side_effect=lambda env, backup: (env, backup)
            ), patch.object(
                lifecycle, "worker_wrapper_target_path", side_effect=lambda path, _service: path
            ), patch.object(lifecycle, "write_worker_wrapper"), patch.object(
                lifecycle, "ensure_lifecycle_watcher", return_value=0
            ), patch.object(lifecycle, "service_user_doctor_command", return_value=["doctor"]), patch.object(
                lifecycle.subprocess, "run", side_effect=run
            ), patch.dict(
                lifecycle.os.environ,
                {"PULLWISE_PYTHON_BIN": str(old_python)},
                clear=False,
            ):
                code = lifecycle.update_worker(config)

            self.assertEqual(code, 7)
            self.assertEqual(env_path.read_text(encoding="utf-8"), f"PULLWISE_PYTHON_BIN={old_python}\n")

    def test_codex_cli_update_settings_default_to_latest_worker_local_command(self) -> None:
        worker_root = "/var/lib/pullwise-worker/wk_test/workers/wk_test"
        config = argparse.Namespace(provider_chain=["codex"], worker_root=worker_root)

        with patch.dict(lifecycle.os.environ, {}, clear=True):
            settings = lifecycle.codex_cli_update_settings(config)

        self.assertIsNotNone(settings)
        self.assertEqual(settings["command"], f"{worker_root}/.local/bin/codex")
        self.assertEqual(settings["install_dir"], f"{worker_root}/.local/bin")
        self.assertEqual(settings["release"], "latest")
        self.assertEqual(settings["installer_url"], "https://chatgpt.com/codex/install.sh")

    def test_codex_cli_update_settings_reject_unscoped_command_and_insecure_installer(self) -> None:
        worker_root = "/var/lib/pullwise-worker/wk_test/workers/wk_test"
        config = argparse.Namespace(provider_chain=["codex"], worker_root=worker_root)

        with patch.dict(
            lifecycle.os.environ,
            {"PULLWISE_CODEX_COMMAND": "/usr/bin/codex"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "inside worker_root"):
                lifecycle.codex_cli_update_settings(config)

        with patch.dict(
            lifecycle.os.environ,
            {"PULLWISE_CODEX_INSTALLER_URL": "http://example.com/codex/install.sh"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                lifecycle.codex_cli_update_settings(config)

if __name__ == "__main__":
    unittest.main()
