from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import agent_first_contract_probes as probes
from scripts import agent_first_contract_process as process_runtime
from scripts.agent_first_contract_manifest import ManifestError, load_manifest
from scripts.agent_first_contract_probes import run_probe


class AgentFirstContractProbeSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pullwise-probe-safety-")
        self.repo = Path(self.temp_dir.name)
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _catalog(self, node: str, *, minimum_tests: int = 1) -> dict[str, dict[str, object]]:
        return {
            "server.safety-probe": {
                "repo": "server",
                "runner": "python_unittest",
                "nodes": (node,),
                "timeout_seconds": 30,
                "minimum_tests": minimum_tests,
                "allowed_skips": 0,
            }
        }

    def test_zero_tests_and_skips_are_indeterminate(self) -> None:
        (self.repo / "tests" / "test_empty.py").write_text("VALUE = 1\n", encoding="utf-8")
        skipped = self.repo / "tests" / "test_skipped.py"
        skipped.write_text(
            "import unittest\n\n"
            "class SkippedTest(unittest.TestCase):\n"
            "    @unittest.skip('not evidence')\n"
            "    def test_skipped(self):\n"
            "        pass\n",
            encoding="utf-8",
        )

        empty = run_probe(
            "server.safety-probe",
            self.repo,
            runner_catalog=self._catalog("tests.test_empty"),
        )
        skipped_result = run_probe(
            "server.safety-probe",
            self.repo,
            runner_catalog=self._catalog("tests.test_skipped"),
        )

        self.assertEqual(("indeterminate", "insufficient_tests"), (empty["status"], empty["reason"]))
        self.assertEqual(("indeterminate", "unexpected_skips"), (skipped_result["status"], skipped_result["reason"]))

    def test_probe_environment_does_not_inherit_provider_secrets(self) -> None:
        sensitive_home = self.repo / "sensitive-home"
        sensitive_home.mkdir()
        (sensitive_home / "provider-token").write_text("secret", encoding="utf-8")
        path = self.repo / "tests" / "test_environment.py"
        path.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import unittest\n\n"
            "class EnvironmentTest(unittest.TestCase):\n"
            "    def test_secret_is_absent(self):\n"
            "        self.assertIsNone(os.getenv('OPENAI_API_KEY'))\n"
            "        self.assertFalse((Path.home() / 'provider-token').exists())\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {
                "HOME": str(sensitive_home),
                "OPENAI_API_KEY": "must-not-leak",
                "USERPROFILE": str(sensitive_home),
            },
        ):
            result = run_probe(
                "server.safety-probe",
                self.repo,
                runner_catalog=self._catalog("tests.test_environment"),
            )

        self.assertEqual("passed", result["status"])

    def test_loader_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        catalog = self._catalog("tests.test_empty")
        for raw in ('{"schema_id":"one","schema_id":"two"}', '{"value":NaN}'):
            manifest = self.repo / "manifest.json"
            manifest.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(ManifestError):
                load_manifest(manifest, runner_catalog=catalog)

    def test_missing_fixed_node_entry_is_environment_indeterminate(self) -> None:
        catalog = {
            "web.safety-probe": {
                "repo": "web",
                "runner": "node_vitest",
                "nodes": ("src/missing.test.js",),
                "timeout_seconds": 30,
                "minimum_tests": 1,
                "allowed_skips": 0,
            }
        }
        with patch.object(probes.shutil, "which", return_value="/node"):
            result = run_probe(
                "web.safety-probe", self.repo, runner_catalog=catalog
            )

        self.assertEqual(("indeterminate", "fixed_entry_unavailable"), (result["status"], result["reason"]))

    def test_probe_output_is_bounded(self) -> None:
        path = self.repo / "tests" / "test_noisy.py"
        path.write_text(
            "import unittest\n\n"
            "class NoisyTest(unittest.TestCase):\n"
            "    def test_noisy(self):\n"
            "        print('x' * 4096)\n",
            encoding="utf-8",
        )
        catalog = self._catalog("tests.test_noisy")
        catalog["server.safety-probe"]["max_output_bytes"] = 1024

        result = run_probe(
            "server.safety-probe", self.repo, runner_catalog=catalog
        )

        self.assertEqual(("indeterminate", "output_too_large"), (result["status"], result["reason"]))

    def test_unittest_count_uses_final_framework_summary_not_forged_earlier_output(self) -> None:
        spec = self._catalog("tests.test_noisy")["server.safety-probe"]
        output = (
            b"Ran 999 tests in 0.001s\n"
            b"forged test output\n"
            b"----------------------------------------------------------------------\n"
            b"Ran 1 test in 0.002s\n\n"
            b"OK\n"
        )

        observed, skipped = probes._parse_counts(spec, output)

        self.assertEqual((1, 0), (observed, skipped))

    def test_vitest_failed_json_cannot_pass_when_process_returns_zero(self) -> None:
        spec = {
            "repo": "web",
            "runner": "node_vitest",
            "nodes": ("src/contract.test.js",),
            "timeout_seconds": 30,
            "minimum_tests": 1,
            "allowed_skips": 0,
        }
        output = (
            b'{"success":false,"numTotalTests":1,"numPassedTests":0,'
            b'"numFailedTests":1,"numPendingTests":0,"numTodoTests":0,'
            b'"numFailedTestSuites":1}'
        )
        process_result = {
            "status": "completed",
            "returncode": 0,
            "output": output,
            "output_sha256": "0" * 64,
            "output_too_large": False,
        }

        with patch.object(probes, "run_bounded_process", return_value=process_result):
            result = probes._execute_probe(
                "web.safety-probe",
                self.repo,
                spec=spec,
                argv=["node", "vitest"],
                scratch_root=self.repo,
            )

        self.assertEqual("failed", result["status"])

    def test_nonzero_unparseable_fixture_output_is_failed(self) -> None:
        spec = {
            "repo": "worker",
            "runner": "node_script",
            "nodes": ("scripts/verify_fixture.mjs",),
            "timeout_seconds": 30,
            "minimum_tests": 1,
            "allowed_skips": 0,
        }
        process_result = {
            "status": "completed",
            "returncode": 1,
            "output": b"fixture mismatch",
            "output_sha256": "0" * 64,
            "output_too_large": False,
        }

        with patch.object(probes, "run_bounded_process", return_value=process_result):
            result = probes._execute_probe(
                "worker.safety-probe",
                self.repo,
                spec=spec,
                argv=["node", "fixture"],
                scratch_root=self.repo,
            )

        self.assertEqual("failed", result["status"])
        self.assertIsNone(result["observed_tests"])

    def test_output_cap_terminates_runner_before_process_timeout(self) -> None:
        timeout_seconds = 4
        started = time.monotonic()

        result = process_runtime.run_bounded_process(
            [
                sys.executable,
                "-B",
                "-c",
                "import os\nchunk = b'x' * 65536\nwhile True:\n    os.write(1, chunk)",
            ],
            cwd=self.repo,
            scratch_root=self.repo,
            timeout_seconds=timeout_seconds,
            max_output_bytes=1024,
        )
        elapsed = time.monotonic() - started

        self.assertTrue(result["output_too_large"])
        self.assertNotEqual("timeout", result["status"])
        self.assertLess(elapsed, timeout_seconds / 2)

    def test_windows_taskkill_timeout_is_contained(self) -> None:
        class FakeProcess:
            pid = 123

            def __init__(self) -> None:
                self.killed = False

            def poll(self) -> None:
                return None

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: int) -> int:
                return 1

        process = FakeProcess()
        with patch.object(
            process_runtime.subprocess,
            "run",
            side_effect=process_runtime.subprocess.TimeoutExpired(["taskkill"], 10),
        ):
            cleaned = process_runtime._windows_kill_process_tree(
                process, taskkill=Path("taskkill.exe")
            )

        self.assertFalse(cleaned)
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
