from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pullwise_worker import agent_kernel_gitlinks as gitlinks
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

    def test_replace_refs_cannot_change_the_exact_revision_tree(self) -> None:
        self._git("replace", self.base_revision, self.gitlink_commit)
        try:
            catalog = inspect_gitlinks(
                self.root,
                base_revision=self.base_revision,
                git_executable=self.git,
            )
        finally:
            self._git("replace", "-d", self.base_revision)

        self.assertEqual(["vendor"], [entry.path for entry in catalog.entries])

    def test_git_subprocess_disables_replace_objects_and_lazy_fetch(self) -> None:
        with mock.patch(
            "pullwise_worker.agent_kernel_gitlinks.subprocess.run",
            wraps=subprocess.run,
        ) as run:
            inspect_gitlinks(
                self.root,
                base_revision=self.base_revision,
                git_executable=self.git,
            )

        self.assertEqual(3, run.call_count)
        calls = run.call_args_list
        self.assertEqual([str(self.git), "--version"], calls[0].args[0])
        self.assertIn("rev-parse", calls[1].args[0])
        self.assertIn("ls-tree", calls[2].args[0])
        self.assertTrue(all(call.args[0][0] == str(self.git) for call in calls))
        self.assertTrue(all(call.kwargs["timeout"] == 30 for call in calls))
        self.assertTrue(all(call.kwargs["env"] is calls[0].kwargs["env"] for call in calls))
        environment = calls[0].kwargs["env"]
        self.assertEqual("1", environment["GIT_NO_REPLACE_OBJECTS"])
        self.assertEqual("1", environment["GIT_NO_LAZY_FETCH"])

    def test_git_version_probe_fails_closed(self) -> None:
        cases = (
            (0, b"git version 2.44.9\n", b"", "SOURCE_GIT_VERSION_UNSUPPORTED"),
            (0, b"git version 2.45\n", b"", "SOURCE_GIT_VERSION_INVALID"),
            (0, b"git version 2.45.0 extra\n", b"", "SOURCE_GIT_VERSION_INVALID"),
            (1, b"git version 2.45.0\n", b"", "SOURCE_GIT_VERSION_UNAVAILABLE"),
            (0, b"git version 2.45.0\n", b"warning", "SOURCE_GIT_VERSION_UNAVAILABLE"),
        )
        for code, stdout, stderr, expected in cases:
            with self.subTest(expected=expected, stdout=stdout, stderr=stderr):
                result = subprocess.CompletedProcess([], code, stdout, stderr)
                with mock.patch(
                    "pullwise_worker.agent_kernel_gitlinks.subprocess.run",
                    return_value=result,
                ):
                    with self.assertRaisesRegex(SourceStateError, expected):
                        inspect_gitlinks(
                            self.root,
                            base_revision=self.base_revision,
                            git_executable=self.git,
                        )

    def test_git_executable_must_be_absolute_and_regular(self) -> None:
        for executable in (Path(self.git.name), self.root):
            with self.subTest(executable=executable):
                with self.assertRaisesRegex(
                    SourceStateError, "SOURCE_GIT_EXECUTABLE_INVALID"
                ):
                    inspect_gitlinks(
                        self.root,
                        base_revision=self.base_revision,
                        git_executable=executable,
                    )

    def test_version_probe_rejects_executable_identity_drift(self) -> None:
        stable = gitlinks._git_executable_identity(self.git)
        changed = (*stable[:-1], stable[-1] + 1)
        result = subprocess.CompletedProcess(
            [], 0, b"git version 2.45.0\n", b""
        )
        with mock.patch(
            "pullwise_worker.agent_kernel_gitlinks._git_executable_identity",
            side_effect=(stable, stable, changed),
        ), mock.patch(
            "pullwise_worker.agent_kernel_gitlinks.subprocess.run",
            return_value=result,
        ):
            with self.assertRaisesRegex(
                SourceStateError, "SOURCE_GIT_EXECUTABLE_CHANGED"
            ):
                inspect_gitlinks(
                    self.root,
                    base_revision=self.base_revision,
                    git_executable=self.git,
                )

    def test_ls_tree_rejects_executable_identity_drift(self) -> None:
        stable = gitlinks._git_executable_identity(self.git)
        changed = (*stable[:-1], stable[-1] + 1)
        results = (
            subprocess.CompletedProcess([], 0, b"git version 2.45.0\n", b""),
            subprocess.CompletedProcess(
                [], 0, os.fsencode(self.root) + b"\n", b""
            ),
            subprocess.CompletedProcess([], 0, b"", b""),
        )
        with mock.patch(
            "pullwise_worker.agent_kernel_gitlinks._git_executable_identity",
            side_effect=(*((stable,) * 6), changed),
        ), mock.patch(
            "pullwise_worker.agent_kernel_gitlinks.subprocess.run",
            side_effect=results,
        ):
            with self.assertRaisesRegex(
                SourceStateError, "SOURCE_GIT_EXECUTABLE_CHANGED"
            ):
                inspect_gitlinks(
                    self.root,
                    base_revision=self.base_revision,
                    git_executable=self.git,
                )

    def test_nested_path_cannot_discover_parent_repository(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GIT_REPOSITORY_MISMATCH"
        ):
            inspect_gitlinks(
                nested,
                base_revision=self.base_revision,
                git_executable=self.git,
            )

    def test_linked_worktree_git_file_is_a_valid_exact_repository_root(self) -> None:
        worktree = Path(self.scratch.name) / "linked-worktree"
        self._git("worktree", "add", "--detach", str(worktree), self.base_revision)
        try:
            self.assertTrue((worktree / ".git").is_file())
            catalog = inspect_gitlinks(
                worktree,
                base_revision=self.base_revision,
                git_executable=self.git,
            )

            self.assertEqual(["vendor"], [entry.path for entry in catalog.entries])
        finally:
            self._git("worktree", "remove", "--force", str(worktree))

    def test_catalog_rejects_non_directory_checkout_ancestor(self) -> None:
        self._git("update-index", "--force-remove", "vendor")
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{self.gitlink_commit},vendor/sub",
        )
        self._git("commit", "-m", "nested gitlink")
        nested_revision = self._git("rev-parse", "HEAD").stdout.strip()
        shutil.rmtree(self.root / "vendor")
        (self.root / "vendor").write_text("not a directory", encoding="utf-8")

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_TOPOLOGY_INVALID"
        ):
            inspect_gitlinks(
                self.root,
                base_revision=nested_revision,
                git_executable=self.git,
            )

    def test_snapshot_rejects_prefix_conflict_created_after_inspection(self) -> None:
        self._git("update-index", "--force-remove", "vendor")
        self._git(
            "update-index", "--add", "--cacheinfo",
            f"160000,{self.gitlink_commit},vendor/sub",
        )
        self._git("commit", "-m", "nested gitlink")
        revision = self._git("rev-parse", "HEAD").stdout.strip()
        (self.root / "vendor" / "sub").mkdir()
        catalog = inspect_gitlinks(
            self.root, base_revision=revision, git_executable=self.git
        )

        def replace_ancestor(stage: str, _path: Path) -> None:
            if stage == "before_root_open":
                shutil.rmtree(self.root / "vendor")
                (self.root / "vendor").write_text("file", encoding="utf-8")

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_ENTRY_TOPOLOGY_INVALID"
        ):
            snapshot_source_tree(
                self.root,
                policy=self.policy,
                base_revision=revision,
                gitlink_catalog=catalog,
                stage_hook=replace_ancestor,
            )

    def test_catalog_rejects_gitlink_prefix_coexistence(self) -> None:
        payload = (
            f"160000 commit {self.gitlink_commit}\tvendor\0"
            f"160000 commit {self.gitlink_commit}\tvendor/sub\0"
        ).encode("ascii")

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_TOPOLOGY_INVALID"
        ):
            gitlinks._parse_tree(payload)

    def test_catalog_rejects_root_identity_change_during_git_inspection(self) -> None:
        metadata = self.root.lstat()
        first = (metadata.st_dev, metadata.st_ino)
        second = (metadata.st_dev, metadata.st_ino + 1)
        with mock.patch(
            "pullwise_worker.agent_kernel_gitlinks._root_identity",
            side_effect=(first, second),
        ):
            with self.assertRaisesRegex(
                SourceStateError, "SOURCE_GITLINK_CATALOG_MISMATCH"
            ):
                inspect_gitlinks(
                    self.root,
                    base_revision=self.base_revision,
                    git_executable=self.git,
                )

    def test_catalog_identity_is_bound_to_the_root_opened_by_scanner(self) -> None:
        catalog = inspect_gitlinks(
            self.root,
            base_revision=self.base_revision,
            git_executable=self.git,
        )
        replacement = Path(self.scratch.name) / "replacement"
        replacement.mkdir()
        (replacement / "README.md").write_text("replacement", encoding="utf-8")
        original = Path(self.scratch.name) / "original"

        def swap(stage: str, path: Path) -> None:
            if stage != "before_root_open":
                return
            try:
                self.root.rename(original)
                replacement.rename(self.root)
            except OSError as exc:
                self.skipTest(f"root replacement unavailable: {exc}")

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_CATALOG_MISMATCH"
        ):
            snapshot_source_tree(
                self.root,
                policy=self.policy,
                base_revision=self.base_revision,
                gitlink_catalog=catalog,
                stage_hook=swap,
            )


if __name__ == "__main__":
    unittest.main()
