from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pullwise_worker.agent_kernel_database import (
    AgentKernelDatabase,
    AgentKernelStorageError,
)
from pullwise_worker.agent_kernel_object_store import CasCorruptError, ObjectStore


class AgentKernelStorageBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-boundary-")
        database = AgentKernelDatabase(Path(self.scratch.name) / "worker")
        database.initialize()
        self.store = ObjectStore(database)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    @staticmethod
    def _metadata() -> dict[str, str]:
        return {
            "task_id": "task_" + "0" * 32,
            "artifact_id": "art_" + "1" * 32,
            "media_type": "application/octet-stream",
            "content_schema_id": "opaque-bytes/v1",
            "encoding": "binary",
        }

    def test_put_rejects_non_integer_or_out_of_range_size_limits(self) -> None:
        for max_bytes in (True, "1", None, 1.0, -1, 2**53):
            with self.subTest(max_bytes=max_bytes), self.assertRaisesRegex(
                AgentKernelStorageError, "content_size_limit_invalid"
            ):
                self.store.put_bytes(
                    b"x",
                    max_bytes=max_bytes,  # type: ignore[arg-type]
                    **self._metadata(),
                )

        self.assertEqual([], list(self.store.tmp_root.iterdir()))

    def test_read_returns_bytes_from_the_verified_handle(self) -> None:
        payload = b"trusted"
        content_ref = self.store.put_bytes(payload, **self._metadata())
        path = self.store.path_for_digest(str(content_ref["sha256"]))
        verify = self.store._verify_path

        def replace_after_verification(
            target: Path, digest: str, size: int, *, capture: bool = False
        ) -> bytes | None:
            verified = verify(target, digest, size, capture=capture)
            replacement = target.with_name(f"{target.name}.replacement")
            replacement.write_bytes(b"corrupt")
            replacement.chmod(0o600)
            os.replace(replacement, target)
            return verified

        self.store._verify_path = replace_after_verification  # type: ignore[method-assign]

        self.assertEqual(payload, self.store.read_verified(content_ref))

    def test_concurrent_publish_retries_when_two_links_already_converged_to_one(
        self,
    ) -> None:
        payload = b"converged"
        content_ref = self.store.put_bytes(payload, **self._metadata())
        path = self.store.path_for_digest(str(content_ref["sha256"]))
        verify = self.store._verify_path
        attempts = 0

        def transient_link_count(target: Path, digest: str, size: int) -> bytes | None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise CasCorruptError(
                    "CAS_CORRUPT: object has unexpected hardlinks"
                )
            return verify(target, digest, size)

        with mock.patch.object(ObjectStore, "_verify_path", transient_link_count):
            self.store._verify_concurrent_publish(
                path, str(content_ref["sha256"]), len(payload)
            )

        self.assertEqual(2, attempts)


if __name__ == "__main__":
    unittest.main()
