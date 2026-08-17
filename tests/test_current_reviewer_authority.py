from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

WORKER_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = WORKER_ROOT.parent
CHECKER_PATH = WORKER_ROOT / "scripts" / "check_current_reviewer_authority.py"
CHECKER_SPEC = importlib.util.spec_from_file_location(
    "_pullwise_worker_current_reviewer_authority", CHECKER_PATH
)
if CHECKER_SPEC is None or CHECKER_SPEC.loader is None:
    raise ImportError(f"unable to load Worker authority checker: {CHECKER_PATH}")
checker = importlib.util.module_from_spec(CHECKER_SPEC)
CHECKER_SPEC.loader.exec_module(checker)


class CurrentReviewerAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        for directory in checker.REPOSITORIES.values():
            repo = self.workspace / directory
            repo.mkdir()
            (repo / "AGENTS.md").write_text(
                checker.ROUTING_BLOCK + "\n\n# Existing repository rules\n",
                encoding="utf-8",
            )

    def _report(self) -> dict[str, object]:
        return checker.validate_workspace(self.workspace)

    def _errors(self, report: dict[str, object], repo: str) -> list[str]:
        repositories = report["repositories"]
        assert isinstance(repositories, list)
        item = next(entry for entry in repositories if entry["repo"] == repo)
        return item["errors"]

    def test_current_workspace_routes_through_current_authority(self) -> None:
        report = checker.validate_workspace(WORKSPACE_ROOT)

        self.assertEqual("PASS", report["status"], json.dumps(report, indent=2))

    def test_positive_fixture_has_one_identical_routing_block(self) -> None:
        report = self._report()

        self.assertEqual("PASS", report["status"])
        self.assertEqual(4, len(report["repositories"]))
        self.assertIsNotNone(report["routing_parity_sha256"])
        self.assertTrue(all(item["status"] == "PASS" for item in report["repositories"]))

    def test_missing_block_fails(self) -> None:
        path = self.workspace / "pullwise-web" / "AGENTS.md"
        path.write_text("# Existing repository rules\n", encoding="utf-8")

        report = self._report()

        self.assertEqual("FAIL", report["status"])
        self.assertIn("missing_current_authority_block", self._errors(report, "web"))

    def test_contradictory_block_fails(self) -> None:
        path = self.workspace / "pullwise-server" / "AGENTS.md"
        text = path.read_text(encoding="utf-8").replace(
            checker.END_MARKER,
            "review-worker-protocol/v1 is the implementation authority.\n"
            + checker.END_MARKER,
        )
        path.write_text(text, encoding="utf-8")

        report = self._report()

        self.assertEqual("FAIL", report["status"])
        self.assertIn("contradictory_block", self._errors(report, "server"))

    def test_stale_authority_fails(self) -> None:
        path = self.workspace / "pullwise-admin" / "AGENTS.md"
        text = path.read_text(encoding="utf-8").replace(
            checker.CURRENT_AUTHORITY_URL,
            "https://app.notion.com/p/00000000000000000000000000000000",
        )
        path.write_text(text, encoding="utf-8")

        report = self._report()

        self.assertEqual("FAIL", report["status"])
        self.assertIn("stale_authority", self._errors(report, "admin"))

    def test_no_network_is_used(self) -> None:
        with (
            mock.patch.object(socket, "create_connection", side_effect=AssertionError),
            mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError),
        ):
            report = self._report()

        self.assertEqual("PASS", report["status"])

    def test_repository_parent_symlink_is_rejected(self) -> None:
        target = self.workspace / "pullwise-admin"
        original_lstat = Path.lstat

        def marked_lstat(path: Path) -> object:
            metadata = original_lstat(path)
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return metadata

        with mock.patch.object(Path, "lstat", autospec=True, side_effect=marked_lstat):
            report = self._report()

        self.assertEqual("INDETERMINATE", report["status"])
        self.assertIn(
            {"repo": "admin", "code": "repository_path_not_safe"},
            report["environment_errors"],
        )

    def test_repository_parent_reparse_point_is_rejected(self) -> None:
        target = self.workspace / "pullwise-admin"
        original_lstat = Path.lstat
        reparse_flag = 0x400

        def marked_lstat(path: Path) -> object:
            metadata = original_lstat(path)
            if path == target:
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_file_attributes=(
                        getattr(metadata, "st_file_attributes", 0) | reparse_flag
                    ),
                )
            return metadata

        with (
            mock.patch.object(
                checker.stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                reparse_flag,
                create=True,
            ),
            mock.patch.object(
                Path, "lstat", autospec=True, side_effect=marked_lstat
            ),
        ):
            report = self._report()

        self.assertEqual("INDETERMINATE", report["status"])
        self.assertIn(
            {"repo": "admin", "code": "repository_path_not_safe"},
            report["environment_errors"],
        )

    def test_agents_file_symlink_is_rejected(self) -> None:
        target = self.workspace / "pullwise-web" / "AGENTS.md"
        original_lstat = Path.lstat

        def linked_lstat(path: Path) -> object:
            metadata = original_lstat(path)
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return metadata

        with mock.patch.object(Path, "lstat", autospec=True, side_effect=linked_lstat):
            report = self._report()

        self.assertEqual("INDETERMINATE", report["status"])
        self.assertIn(
            {"repo": "web", "code": "agents_file_not_regular"},
            report["environment_errors"],
        )

    def test_agents_file_resolution_must_remain_in_repository(self) -> None:
        target = self.workspace / "pullwise-server" / "AGENTS.md"
        diverted = self.workspace.parent / "outside" / "AGENTS.md"
        original_resolve = Path.resolve

        def diverted_resolve(path: Path, strict: bool = False) -> Path:
            if path == target:
                return diverted
            return original_resolve(path, strict=strict)

        with mock.patch.object(
            Path, "resolve", autospec=True, side_effect=diverted_resolve
        ):
            report = self._report()

        self.assertEqual("INDETERMINATE", report["status"])
        self.assertIn(
            {"repo": "server", "code": "agents_file_outside_repository"},
            report["environment_errors"],
        )

    def test_invalid_utf8_is_structured(self) -> None:
        (self.workspace / "pullwise-worker" / "AGENTS.md").write_bytes(b"\xff")

        report = self._report()

        self.assertEqual("INDETERMINATE", report["status"])
        self.assertIn(
            {"repo": "worker", "code": "agents_file_not_utf8"},
            report["environment_errors"],
        )

    def test_filesystem_error_is_structured(self) -> None:
        target = self.workspace / "pullwise-worker" / "AGENTS.md"
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path: Path) -> bytes:
            if path == target:
                raise PermissionError("denied")
            return original_read_bytes(path)

        with mock.patch.object(
            Path, "read_bytes", autospec=True, side_effect=guarded_read_bytes
        ):
            report = self._report()

        self.assertEqual("INDETERMINATE", report["status"])
        self.assertIn(
            {"repo": "worker", "code": "agents_file_unreadable"},
            report["environment_errors"],
        )

    def test_cli_emits_machine_readable_failure(self) -> None:
        (self.workspace / "pullwise-worker" / "AGENTS.md").write_text(
            "# Missing routing block\n", encoding="utf-8"
        )
        process = subprocess.run(
            [
                sys.executable,
                str(WORKER_ROOT / "scripts" / "check_current_reviewer_authority.py"),
                "--workspace-root",
                str(self.workspace),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(1, process.returncode)
        self.assertEqual("FAIL", json.loads(process.stdout)["status"])
        self.assertEqual("", process.stderr)

    def test_repo_cli_validates_current_worker_checkout(self) -> None:
        process = subprocess.run(
            [
                sys.executable,
                str(CHECKER_PATH),
                "--repo",
                "worker",
            ],
            cwd=WORKER_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        report = json.loads(process.stdout)
        repositories = report["repositories"]
        self.assertEqual(0, process.returncode)
        self.assertEqual("PASS", report["status"])
        self.assertEqual(
            ["worker"], [item["repo"] for item in repositories]
        )
        self.assertEqual([], report["environment_errors"])
        self.assertEqual("", process.stderr)


if __name__ == "__main__":
    unittest.main()
