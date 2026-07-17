from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pullwise_worker.review_worker_v1 import (
    intent_execution_preflight,
    intent_test_command_policy,
)


class IntentCommandOperandContainmentTest(unittest.TestCase):
    def test_policy_rejects_paths_embedded_in_options_when_they_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation-repo"
            validation_repo.mkdir()
            outside = root / "outside"
            commands = (
                ["go", "test", f"-coverprofile={outside / 'coverage.out'}"],
                ["pytest", f"--basetemp={outside / 'pytest'}"],
                ["cargo", "test", "--target-dir=../outside/cargo"],
            )

            for command in commands:
                with self.subTest(command=command):
                    allowed, reason = intent_test_command_policy(
                        command,
                        validation_repo,
                        validation_repo,
                    )

                    self.assertFalse(allowed)
                    self.assertIn("outside the validation workspace", reason)

    def test_policy_allows_an_embedded_output_path_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir) / "validation-repo"
            validation_repo.mkdir()
            command = [
                "go",
                "test",
                f"-coverprofile={validation_repo / 'build' / 'coverage.out'}",
            ]

            allowed, _reason = intent_test_command_policy(
                command,
                validation_repo,
                validation_repo,
            )

        self.assertTrue(allowed)

    def test_preflight_rejects_embedded_path_escape_before_runtime_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation-repo"
            validation_repo.mkdir()
            command = ["go", "test", f"-coverprofile={root / 'outside' / 'coverage.out'}"]

            with patch(
                "pullwise_worker.review_worker_v1.intent_command_is_runnable_for_repo",
                return_value=(True, "runnable"),
            ) as runtime_probe:
                diagnostic = intent_execution_preflight(
                    command,
                    validation_repo,
                    validation_repo,
                )

        self.assertEqual(diagnostic["status"], "blocked")
        self.assertEqual(diagnostic["reason_code"], "command_policy_denied")
        runtime_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
