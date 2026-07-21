from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_canonical import canonical_sha256
from pullwise_worker.agent_kernel_source_state import (
    SourceEntry,
    SourceSelectionPolicy,
    SourceStateError,
    SourceTreeSnapshot,
    assert_pullwise_source_unchanged,
    diff_source_trees,
    snapshot_source_tree,
)


BASE_REVISION = "a" * 40


class AgentKernelSourceStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-source-")
        self.root = Path(self.scratch.name) / "repository"
        self.root.mkdir()
        self.policy = SourceSelectionPolicy.pullwise_full_scan(
            root_identity="repository:test-fixture"
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_snapshot_is_deterministic_and_excludes_only_control_roots(self) -> None:
        (self.root / "z.txt").write_bytes(b"z\n")
        (self.root / "a.txt").write_bytes(b"a\r\n")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "b.bin").write_bytes(b"\x00\xff")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.root / ".codex-review").mkdir()
        (self.root / ".codex-review" / "runtime.json").write_text(
            "runtime", encoding="utf-8"
        )

        first = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )
        second = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )

        self.assertEqual(first, second)
        self.assertEqual(
            ["a.txt", "nested/b.bin", "z.txt"],
            [entry.path for entry in first.entries],
        )
        self.assertEqual(7, first.total_bytes)
        self.assertEqual(3, first.entry_count)
        self.assertEqual(64, len(first.source_state_id))

    def test_snapshot_hashes_raw_bytes_and_records_executable_identity(self) -> None:
        script = self.root / "tool.sh"
        script.write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
        if os.name != "nt":
            script.chmod(0o755)

        snapshot = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )
        entry = snapshot.entries[0]

        self.assertEqual("file", entry.type)
        self.assertEqual(hashlib.sha256(script.read_bytes()).hexdigest(), entry.sha256)
        self.assertEqual(os.name != "nt", entry.executable)

    def test_symlink_identity_is_recorded_without_following_target(self) -> None:
        outside = Path(self.scratch.name) / "outside.txt"
        outside.write_text("outside secret", encoding="utf-8")
        link = self.root / "outside-link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        snapshot = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )

        self.assertEqual(1, len(snapshot.entries))
        self.assertEqual("symlink", snapshot.entries[0].type)
        self.assertEqual(str(outside), snapshot.entries[0].target)
        self.assertIsNone(snapshot.entries[0].sha256)

    def test_special_file_fails_closed_instead_of_being_ignored(self) -> None:
        fifo = self.root / "pipe"
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation unavailable")
        try:
            os.mkfifo(fifo)
        except OSError as exc:
            self.skipTest(f"FIFO creation unavailable: {exc}")

        with self.assertRaisesRegex(SourceStateError, "SOURCE_SPECIAL_FILE"):
            snapshot_source_tree(
                self.root, policy=self.policy, base_revision=BASE_REVISION
            )

    def test_scan_detects_file_replacement_or_mutation(self) -> None:
        target = self.root / "mutable.txt"
        target.write_text("before", encoding="utf-8")
        changed = False

        def mutate(stage: str, path: Path) -> None:
            nonlocal changed
            if stage == "after_file_read" and path == target and not changed:
                changed = True
                target.write_text("after", encoding="utf-8")

        with self.assertRaisesRegex(SourceStateError, "SOURCE_CHANGED_DURING_SCAN"):
            snapshot_source_tree(
                self.root,
                policy=self.policy,
                base_revision=BASE_REVISION,
                stage_hook=mutate,
            )

    def test_snapshot_rejects_casefold_collisions_even_off_case_sensitive_hosts(self) -> None:
        digest = hashlib.sha256(b"x").hexdigest()
        entries = (
            SourceEntry.file("Folder/A.txt", size_bytes=1, sha256=digest),
            SourceEntry.file("folder/a.txt", size_bytes=1, sha256=digest),
        )

        with self.assertRaisesRegex(SourceStateError, "SOURCE_PATH_CASE_COLLISION"):
            SourceTreeSnapshot(
                base_revision=BASE_REVISION,
                selection_policy_digest=self.policy.digest,
                entries=entries,
            )

    def test_snapshot_rejects_case_colliding_directory_components(self) -> None:
        (self.root / "Folder").mkdir()
        (self.root / "Folder" / "one.txt").write_text("one", encoding="utf-8")
        try:
            (self.root / "folder").mkdir()
        except FileExistsError:
            self.skipTest("host filesystem is case-insensitive")
        (self.root / "folder" / "two.txt").write_text("two", encoding="utf-8")

        with self.assertRaisesRegex(SourceStateError, "SOURCE_PATH_CASE_COLLISION"):
            snapshot_source_tree(
                self.root, policy=self.policy, base_revision=BASE_REVISION
            )

    def test_policy_fails_closed_for_unimplemented_ephemeral_patterns(self) -> None:
        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_EPHEMERAL_PATTERN_UNSUPPORTED"
        ):
            SourceSelectionPolicy(
                root_identity="repository:test-fixture",
                include="all_repository_regular_files",
                excluded_control_roots=(".codex-review", ".git"),
                ephemeral_patterns=("*.tmp",),
            )

    def test_pullwise_policy_rejects_caller_selected_exclusions(self) -> None:
        with self.assertRaisesRegex(SourceStateError, "SOURCE_CONTROL_ROOTS_UNTRUSTED"):
            SourceSelectionPolicy(
                root_identity="repository:test-fixture",
                include="all_repository_regular_files",
                excluded_control_roots=(".git", "src"),
            )

    def test_entry_rejects_non_utf8_path_identity(self) -> None:
        with self.assertRaisesRegex(SourceStateError, "SOURCE_PATH_NOT_UTF8"):
            SourceEntry.file(
                "bad-" + chr(0xDCFF),
                size_bytes=1,
                sha256=hashlib.sha256(b"x").hexdigest(),
            )

    def test_unverified_gitlink_catalog_cannot_hide_a_subtree(self) -> None:
        vendor = self.root / "vendor"
        vendor.mkdir()
        (vendor / "source.txt").write_text("must not be hidden", encoding="utf-8")

        with self.assertRaisesRegex(
            SourceStateError, "SOURCE_GITLINK_CATALOG_UNVERIFIED"
        ):
            snapshot_source_tree(
                self.root,
                policy=self.policy,
                base_revision=BASE_REVISION,
                gitlink_catalog={"vendor": "b" * 40},
            )

    def test_internal_policy_facts_do_not_claim_a_versioned_server_schema(self) -> None:
        facts = self.policy.identity_facts()
        self.assertNotIn("schema_id", facts)
        self.assertEqual(canonical_sha256(facts), self.policy.digest)

    def test_changeset_is_deterministic_and_pullwise_rejects_every_change(self) -> None:
        path = self.root / "tracked.txt"
        path.write_text("before", encoding="utf-8")
        original = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )
        path.write_text("after", encoding="utf-8")
        (self.root / "added.txt").write_text("added", encoding="utf-8")
        final = snapshot_source_tree(
            self.root, policy=self.policy, base_revision=BASE_REVISION
        )

        changes = diff_source_trees(original, final)
        self.assertEqual(["added.txt"], [item.path for item in changes.added])
        self.assertEqual(
            ["tracked.txt"], [item.path for item in changes.modified]
        )
        self.assertEqual((), changes.deleted)
        self.assertEqual((), changes.type_changed)
        with self.assertRaisesRegex(SourceStateError, "SOURCE_MUTATION_FORBIDDEN"):
            assert_pullwise_source_unchanged(original, final)

        unchanged = diff_source_trees(final, final)
        self.assertTrue(unchanged.is_empty)
        assert_pullwise_source_unchanged(final, final)

        incompatible = SourceTreeSnapshot(
            base_revision="b" * 40,
            selection_policy_digest=final.selection_policy_digest,
            entries=final.entries,
        )
        with self.assertRaisesRegex(SourceStateError, "SOURCE_DIFF_IDENTITY_MISMATCH"):
            diff_source_trees(final, incompatible)


if __name__ == "__main__":
    unittest.main()
