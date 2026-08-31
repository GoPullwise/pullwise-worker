from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

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

    def test_concurrent_exact_publish_converges_without_transient_unsafe(self) -> None:
        payload = b"concurrent durable current payload"
        second_store = CurrentObjectStore(self.root)
        first_unlink_entered = threading.Event()
        allow_first_unlink = threading.Event()
        second_staging_fsynced = threading.Event()
        allow_second_staging_fsync = threading.Event()
        second_link_failed = threading.Event()
        allow_second_link_return = threading.Event()
        second_verify_entered = threading.Event()
        allow_second_verify = threading.Event()
        second_thread_id: list[int] = []
        first_staging_unlink = True
        unlink_lock = threading.Lock()
        original_fsync = os.fsync
        original_link = os.link
        original_unlink = Path.unlink
        original_verify = CurrentObjectStore._verify

        def controlled_link(source: Path, target: Path, *args, **kwargs):
            try:
                return original_link(source, target, *args, **kwargs)
            except FileExistsError:
                if (
                    second_thread_id
                    and threading.get_ident() == second_thread_id[0]
                ):
                    second_link_failed.set()
                    if not allow_second_link_return.wait(timeout=5):
                        raise TimeoutError("second publish did not resume linking")
                raise

        def controlled_fsync(descriptor: int) -> None:
            original_fsync(descriptor)
            if (
                second_thread_id
                and threading.get_ident() == second_thread_id[0]
                and not second_staging_fsynced.is_set()
            ):
                second_staging_fsynced.set()
                if not allow_second_staging_fsync.wait(timeout=5):
                    raise TimeoutError("second publish did not resume after fsync")

        def controlled_unlink(path: Path, *args, **kwargs):
            nonlocal first_staging_unlink
            should_block = False
            if path.parent == self.store.staging and path.suffix == ".tmp":
                with unlink_lock:
                    if first_staging_unlink:
                        first_staging_unlink = False
                        should_block = True
            if should_block:
                first_unlink_entered.set()
                if not allow_first_unlink.wait(timeout=15):
                    raise TimeoutError("first staging unlink did not resume")
            return original_unlink(path, *args, **kwargs)

        def controlled_verify(
            store: CurrentObjectStore,
            published: PublishedCurrentObject,
            *,
            return_bytes: bool,
        ):
            if (
                second_thread_id
                and threading.get_ident() == second_thread_id[0]
                and not second_verify_entered.is_set()
            ):
                second_verify_entered.set()
                if not allow_second_verify.wait(timeout=5):
                    raise TimeoutError("second publish did not resume verification")
            return original_verify(store, published, return_bytes=return_bytes)

        def publish_second() -> PublishedCurrentObject:
            second_thread_id.append(threading.get_ident())
            return second_store.publish(payload)

        with mock.patch.object(os, "fsync", new=controlled_fsync):
            with mock.patch.object(os, "link", new=controlled_link):
                with mock.patch.object(Path, "unlink", new=controlled_unlink):
                    with mock.patch.object(
                        CurrentObjectStore, "_verify", new=controlled_verify
                    ):
                        with ThreadPoolExecutor(max_workers=2) as executor:
                            first = executor.submit(self.store.publish, payload)
                            self.assertTrue(first_unlink_entered.wait(timeout=5))
                            second = executor.submit(publish_second)

                            if second_staging_fsynced.wait(timeout=5):
                                allow_second_staging_fsync.set()
                                self.assertTrue(second_link_failed.wait(timeout=5))
                                allow_second_link_return.set()
                                self.assertTrue(second_verify_entered.wait(timeout=5))
                                allow_second_verify.set()
                                try:
                                    second.result(timeout=5)
                                finally:
                                    allow_first_unlink.set()
                                self.fail(
                                    "second publisher reached verification before "
                                    "the first staging link was removed"
                                )

                            allow_first_unlink.set()
                            first_object = first.result(timeout=5)
                            self.assertTrue(second_staging_fsynced.wait(timeout=5))
                            allow_second_staging_fsync.set()
                            self.assertTrue(second_link_failed.wait(timeout=5))
                            allow_second_link_return.set()
                            self.assertTrue(second_verify_entered.wait(timeout=5))
                            allow_second_verify.set()
                            second_object = second.result(timeout=5)

        self.assertEqual(first_object, second_object)
        self.assertEqual(
            b"concurrent durable current payload",
            second_store.read_verified(first_object),
        )
        self.assertEqual(1, second_store.path_for(first_object).stat().st_nlink)

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
