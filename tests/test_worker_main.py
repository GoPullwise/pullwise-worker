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
from pullwise_worker import _main_part_08_lifecycle_cleanup as lifecycle
from pullwise_worker._main_part_08_lifecycle_cleanup import command_worker_has_active_jobs


class WorkerMainContractsTest(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
