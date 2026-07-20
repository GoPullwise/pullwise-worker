from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from pullwise_worker.agent_kernel_database import AgentKernelDatabase
from pullwise_worker.agent_kernel_object_store import ObjectStore


class AgentKernelCasConcurrencyTest(unittest.TestCase):
    def test_concurrent_reader_waits_for_durable_hardlink_convergence(self) -> None:
        observer_started = threading.Event()

        class CoordinatedStore(ObjectStore):
            @classmethod
            def _verify_concurrent_publish(
                cls, path: Path, digest: str, size: int
            ) -> None:
                observer_started.set()
                ObjectStore._verify_concurrent_publish(path, digest, size)

        with tempfile.TemporaryDirectory(prefix='agent-kernel-cas-race-') as scratch:
            database = AgentKernelDatabase(Path(scratch) / 'worker')
            database.initialize()
            store = CoordinatedStore(database)
            winner_at_directory_fsync = threading.Event()
            hold_winner_once = threading.Lock()
            original_fsync = store._fsync_directory
            winner_held = False

            def coordinated_fsync(path: Path) -> None:
                nonlocal winner_held
                should_hold = False
                with hold_winner_once:
                    if not winner_held and path.parent == store.objects_root:
                        winner_held = True
                        should_hold = True
                if should_hold:
                    winner_at_directory_fsync.set()
                    self.assertTrue(observer_started.wait(timeout=2.0))
                    time.sleep(0.25)
                original_fsync(path)

            store._fsync_directory = coordinated_fsync
            arguments = {
                'task_id': 'task_' + 'e' * 32,
                'artifact_id': 'art_' + 'f' * 32,
                'media_type': 'application/octet-stream',
                'content_schema_id': 'opaque-bytes/v1',
                'encoding': 'binary',
            }
            with ThreadPoolExecutor(max_workers=2) as executor:
                winner = executor.submit(
                    store.put_bytes, b'same immutable bytes', **arguments
                )
                self.assertTrue(winner_at_directory_fsync.wait(timeout=2.0))
                observer = executor.submit(
                    store.put_bytes, b'same immutable bytes', **arguments
                )
                refs = (winner.result(timeout=10.0), observer.result(timeout=10.0))

            self.assertEqual(refs[0], refs[1])
            self.assertEqual(b'same immutable bytes', store.read_verified(refs[0]))


if __name__ == '__main__':
    unittest.main()
