from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
)
from pullwise_worker.agent_kernel_current_checkpoint import (
    CurrentCheckpointError,
    CurrentCheckpointStore,
)
from pullwise_worker.agent_kernel_current_database import CurrentAgentKernelDatabase
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from tests.current_checkpoint_support import (
    checkpoint_documents,
    manifest_document,
)
from tests.current_runtime_bootstrap_support import bootstrap_bytes


CHECKPOINT_TABLES = (
    "checkpoint_objects",
    "checkpoint_manifests",
    "checkpoint_index",
    "checkpoint_heads",
    "checkpoint_server_acks",
)


class CurrentCheckpointStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-checkpoint-")
        self.database = CurrentAgentKernelDatabase.open(
            Path(self.scratch.name) / "current", CURRENT_PACKAGE
        )
        self.bootstrap = CurrentRuntimeBootstrapConsumer(self.database).ingest(
            bootstrap_bytes()
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def counts(self) -> tuple[int, ...]:
        connection = self.database.connect()
        try:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CHECKPOINT_TABLES
            )
        finally:
            connection.close()

    def test_genesis_commit_is_atomic_content_addressed_and_exactly_replayable(
        self,
    ) -> None:
        context = checkpoint_documents(1)
        store = CurrentCheckpointStore(self.database)

        first = store.commit(*context)
        replay = store.commit(*context)

        manifest = manifest_document(context[0])
        self.assertEqual(manifest["manifest_hash"], first.manifest_hash)
        self.assertEqual(first, replay)
        self.assertEqual((6, 1, 1, 1, 0), self.counts())
        connection = self.database.connect()
        try:
            head = connection.execute(
                "SELECT generation, manifest_hash FROM checkpoint_heads"
            ).fetchone()
            task = connection.execute(
                "SELECT task_version, checkpoint_generation, checkpoint_hash "
                "FROM runtime_task_records ORDER BY task_version DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((1, manifest["manifest_hash"]), tuple(head))
        self.assertEqual((3, 1, manifest["manifest_hash"]), tuple(task))

    def test_manifest_cas_rejects_skip_and_same_generation_fork(self) -> None:
        store = CurrentCheckpointStore(self.database)
        first = checkpoint_documents(1)
        committed = store.commit(*first)
        first_manifest = manifest_document(first[0])

        skipped = checkpoint_documents(3, previous_manifest=first_manifest)
        with self.assertRaises(CurrentCheckpointError) as skipped_error:
            store.commit(*skipped)
        self.assertEqual("CHECKPOINT_CAS_CONFLICT", skipped_error.exception.code)

        second = checkpoint_documents(2, previous_manifest=first_manifest)
        store.commit(*second)
        fork = checkpoint_documents(
            2, previous_manifest=first_manifest, summary_suffix=" Fork."
        )
        before = self.counts()
        with self.assertRaises(CurrentCheckpointError) as fork_error:
            store.commit(*fork)
        self.assertEqual("CHECKPOINT_REPLAY_CONFLICT", fork_error.exception.code)
        self.assertEqual(before, self.counts())
        self.assertEqual(first_manifest["manifest_hash"], committed.manifest_hash)

    def test_every_commit_stage_rolls_back_without_advancing_task_or_index(self) -> None:
        context = checkpoint_documents(1)
        stages = (
            "after_objects",
            "after_manifest",
            "after_index",
            "after_task_record",
            "before_head_cas",
            "after_head_cas",
            "before_commit",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                def inject(actual: str, *, selected: str = stage) -> None:
                    if actual == selected:
                        raise RuntimeError(f"injected:{selected}")

                store = CurrentCheckpointStore(self.database, fault_hook=inject)
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    store.commit(*context)
                self.assertEqual((0, 0, 0, 0, 0), self.counts())
                connection = self.database.connect()
                try:
                    versions = connection.execute(
                        "SELECT COUNT(*), MAX(task_version) FROM runtime_task_records"
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual((1, 2), tuple(versions))

    def test_recovery_uses_latest_acknowledged_complete_generation_and_falls_back(
        self,
    ) -> None:
        store = CurrentCheckpointStore(self.database)
        first = checkpoint_documents(1)
        first_commit = store.commit(*first)
        first_manifest = manifest_document(first[0])
        second = checkpoint_documents(2, previous_manifest=first_manifest)
        second_commit = store.commit(*second)

        store.record_server_ack(first_commit)
        self.assertEqual(first_commit, store.recover(self.bootstrap.task_id).commit)
        store.record_server_ack(second_commit)
        self.assertEqual(second_commit, store.recover(self.bootstrap.task_id).commit)

        connection = sqlite3.connect(self.database.path)
        connection.execute("DROP TRIGGER checkpoint_manifests_no_update")
        connection.execute(
            "UPDATE checkpoint_manifests SET manifest_bytes=x'7b7d' "
            "WHERE manifest_hash=?",
            (second_commit.manifest_hash,),
        )
        connection.commit()
        connection.close()

        recovered = store.recover(self.bootstrap.task_id)
        self.assertEqual(first_commit, recovered.commit)
        self.assertEqual(1, recovered.generation)


if __name__ == "__main__":
    unittest.main()
