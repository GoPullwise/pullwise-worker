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
    def test_policy_rejects_real_path_options_when_they_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation-repo"
            validation_repo.mkdir()
            outside = root / "outside"
            commands = (
                ["go", "test", f"-coverprofile={outside / 'coverage.out'}"],
                ["cargo", "test", "--target-dir=../outside/cargo"],
                ["pytest", "--basetemp=../outside/pytest"],
                ["pytest", "-c", "../outside/pytest.ini"],
                ["make", "-C../outside", "test"],
                ["make", "-C", "../outside", "test"],
                ["make", "--directory=../outside", "test"],
                ["make", "--directory", "../outside", "test"],
                ["pytest", "--basetemp=.."],
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

    def test_policy_rejects_bare_symlink_path_option_values_that_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            validation_repo = root / "validation-repo"
            validation_repo.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (validation_repo / "escape-link").symlink_to(outside, target_is_directory=True)
            commands = (
                ["pytest", "escape-link"],
                ["make", "-Cescape-link", "test"],
                ["make", "-C", "escape-link", "test"],
                ["make", "--directory=escape-link", "test"],
                ["pytest", "--basetemp=escape-link"],
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

    def test_policy_allows_contained_paths_including_nested_parent_operands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir) / "validation-repo"
            nested_cwd = validation_repo / "packages" / "api"
            nested_cwd.mkdir(parents=True)
            commands = (
                ["go", "test", f"-coverprofile={validation_repo / 'build' / 'coverage.out'}"],
                ["cargo", "test", "--target-dir=../../build/cargo"],
                ["pytest", "--basetemp=.."],
                ["make", "-C..", "test"],
                ["pytest", "--color=auto"],
            )

            for command in commands:
                with self.subTest(command=command):
                    allowed, _reason = intent_test_command_policy(
                        command,
                        nested_cwd,
                        validation_repo,
                    )

                    self.assertTrue(allowed)

    def test_policy_preserves_go_subtest_selector_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir) / "validation-repo"
            validation_repo.mkdir()
            commands = (
                ["go", "test", "-run=/Subtest"],
                ["go", "test", "-run", "/Subtest"],
            )

            for command in commands:
                with self.subTest(command=command):
                    allowed, reason = intent_test_command_policy(
                        command,
                        validation_repo,
                        validation_repo,
                    )

                    self.assertTrue(allowed, reason)

    def test_policy_rejects_urls_in_embedded_separate_and_attached_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir) / "validation-repo"
            validation_repo.mkdir()
            commands = (
                ["pytest", "--basetemp=https://example.invalid/output"],
                ["pytest", "--basetemp", "https://example.invalid/output"],
                ["make", "-Chttps://example.invalid/project", "test"],
            )

            for command in commands:
                with self.subTest(command=command):
                    allowed, reason = intent_test_command_policy(
                        command,
                        validation_repo,
                        validation_repo,
                    )

                    self.assertFalse(allowed)
                    self.assertIn("network URLs", reason)

    def test_double_dash_positionals_still_receive_containment_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validation_repo = Path(tmp_dir) / "validation-repo"
            validation_repo.mkdir()

            escaped, _reason = intent_test_command_policy(
                ["pytest", "--", "../outside/test_behavior.py"],
                validation_repo,
                validation_repo,
            )
            contained, contained_reason = intent_test_command_policy(
                ["pytest", "--", "tests/test_behavior.py"],
                validation_repo,
                validation_repo,
            )

        self.assertFalse(escaped)
        self.assertTrue(contained, contained_reason)

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
