from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
