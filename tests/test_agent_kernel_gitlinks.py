from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pullwise_worker.agent_kernel_gitlinks import inspect_gitlinks
from pullwise_worker.agent_kernel_source_state import (
    SourceSelectionPolicy,
    SourceStateError,
    snapshot_source_tree,
)


class AgentKernelGitlinkCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        executable = shutil.which("git")
        if executable is None:
            self.skipTest("git executable unavailable")
        self.git = Path(executable)
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-gitlinks-")
        self.root = Path(self.scratch.name) / "repository"
        self.root.mkdir()
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:gitlink-test"
        )
        self._git("init")
        self._git("config", "user.name", "Pullwise Test")
        self._git("config", "user.email", "pullwise@example.invalid")
        (self.root / "README.md").write_text("root", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "root")
        self.gitlink_commit = self._git("rev-parse", "HEAD").stdout.strip()
        vendor = self.root / "vendor"
        vendor.mkdir()
        (vendor / "hidden.txt").write_text("not parent source", encoding="utf-8")
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.gitlink_commit},vendor",
        )
        self._git("commit", "-m", "gitlink")
        self.base_revision = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        if hasattr(self, "scratch"):
            self.scratch.cleanup()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.git), "-C", str(self.root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_git_checkout_requires_a_verified_catalog(self) -> None:
        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_CATALOG_REQUIRED"
        ):
            snapshot_source_tree(
                self.root,
                policy=self.policy,
                base_revision=self.base_revision,
            )

    def test_exact_revision_catalog_records_gitlink_without_traversal(self) -> None:
        catalog = inspect_gitlinks(
            self.root,
            base_revision=self.base_revision,
            git_executable=self.git,
        )

        snapshot = snapshot_source_tree(
            self.root,
            policy=self.policy,
            base_revision=self.base_revision,
            gitlink_catalog=catalog,
        )

        self.assertEqual(["README.md", "vendor"], [e.path for e in snapshot.entries])
        self.assertEqual("gitlink", snapshot.entries[1].type)
        self.assertEqual(self.gitlink_commit, snapshot.entries[1].commit_sha)

    def test_catalog_is_bound_to_exact_root_and_revision(self) -> None:
        catalog = inspect_gitlinks(
            self.root,
            base_revision=self.base_revision,
            git_executable=self.git,
        )

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_CATALOG_MISMATCH"
        ):
            snapshot_source_tree(
                self.root,
                policy=self.policy,
                base_revision="a" * 40,
                gitlink_catalog=catalog,
            )

        replacement = Path(self.scratch.name) / "replacement"
        replacement.mkdir()
        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_CATALOG_MISMATCH"
        ):
            snapshot_source_tree(
                replacement,
                policy=self.policy,
                base_revision=self.base_revision,
                gitlink_catalog=catalog,
            )

    def test_git_output_digest_and_entries_are_deterministic(self) -> None:
        first = inspect_gitlinks(
            self.root,
            base_revision=self.base_revision,
            git_executable=self.git,
        )
        second = inspect_gitlinks(
            self.root,
            base_revision=self.base_revision,
            git_executable=self.git,
        )

        self.assertEqual(first.entries, second.entries)
        self.assertEqual(first.tree_digest, second.tree_digest)


if __name__ == "__main__":
    unittest.main()
