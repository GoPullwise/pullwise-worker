from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pullwise_worker.main as worker_main


class WorkerMainContractsTest(unittest.TestCase):
    def test_main_module_does_not_import_replaced_graph_review_pipeline(self) -> None:
        imported = set(sys.modules)
        importlib.reload(worker_main)
        new_modules = set(sys.modules) - imported
        self.assertNotIn("pullwise_worker._main_part_04_" + "graph_verified_review", new_modules)
        self.assertFalse(hasattr(worker_main, "run_" + "graph_verified_review_payload"))
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

    def test_project_package_discovery_excludes_replaced_review_package(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        self.assertIn('include = ["pullwise_worker"]', text)
        self.assertNotIn('"code' + 'review"', text)
        self.assertNotIn('"code' + 'review.*"', text)


if __name__ == "__main__":
    unittest.main()