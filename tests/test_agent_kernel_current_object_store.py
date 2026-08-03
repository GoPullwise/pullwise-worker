from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_objects import (
    CurrentObjectError,
    CurrentObjectStore,
    PublishedCurrentObject,
)

class CurrentObjectStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-object-store-")
        self.root = Path(self.scratch.name) / "content"
        self.store = CurrentObjectStore(self.root)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_publish_is_private_verified_and_idempotent(self) -> None:
        first = self.store.publish(b"durable current payload")
        second = self.store.publish(b"durable current payload")

        self.assertEqual(first, second)
        self.assertEqual(b"durable current payload", self.store.read_verified(first))
        path = self.store.path_for(first)
        self.assertEqual(1, path.stat().st_nlink)
        if os.name == "posix":
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_existing_corrupt_object_fails_closed(self) -> None:
        published = self.store.publish(b"original")
        path = self.store.path_for(published)
        path.write_bytes(b"corrupt")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_CORRUPT"):
            self.store.publish(b"original")

    def test_hardlinked_object_is_never_accepted(self) -> None:
        published = self.store.publish(b"one-link-only")
        os.link(self.store.path_for(published), self.root / "extra-link")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_UNSAFE"):
            self.store.read_verified(published)

    def test_symlinked_object_is_never_followed(self) -> None:
        published = self.store.publish(b"original object")
        outside = Path(self.scratch.name) / "outside-object"
        outside.write_bytes(b"original object")
        path = self.store.path_for(published)
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_UNSAFE"):
            self.store.read_verified(published)

    def test_replaced_staging_directory_is_rejected_before_write(self) -> None:
        original = self.root / "staging-original"
        self.store.staging.rename(original)
        outside = Path(self.scratch.name) / "outside-staging"
        outside.mkdir()
        try:
            self.store.staging.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            original.rename(self.store.staging)
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_ROOT_INVALID"):
            self.store.publish(b"must not escape")
        self.assertEqual([], list(outside.iterdir()))

    def test_forged_object_identity_cannot_escape_or_weaken_cas_checks(self) -> None:
        forged = (
            PublishedCurrentObject("../" + "a" * 61, 1, "objects/../outside"),
            PublishedCurrentObject("A" * 64, 1, f"objects/AA/{'A' * 64}"),
            PublishedCurrentObject("a" * 64, -1, f"objects/aa/{'a' * 64}"),
        )

        for identity in forged:
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(
                    CurrentObjectError, "CURRENT_OBJECT_IDENTITY_INVALID"
                ):
                    self.store.read_verified(identity)
if __name__ == "__main__":
    unittest.main()