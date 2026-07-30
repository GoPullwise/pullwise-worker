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
from pullwise_worker.agent_kernel_current_authority import CurrentAuthorityProjection
from pullwise_worker.agent_kernel_current_database import CurrentAgentKernelDatabase
from pullwise_worker.agent_kernel_current_package import (
    AgentClaimAbandonResponse,
    CURRENT_PACKAGE,
    canonical_validated_current_bytes,
    seal_current_document,
)
from tests.current_checkpoint_support import (
    checkpoint_ack_bytes,
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
    "checkpoint_server_ack_documents",
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

    def fenced_authority(self) -> AgentClaimAbandonResponse:
        authority = self.bootstrap
        document = seal_current_document(
            "agent-claim-abandon-response/v1",
            {
                "schema_id": "agent-claim-abandon-response/v1",
                "package": authority.package.as_document(),
                "task_id": authority.task_id,
                "attempt_id": authority.attempt_id,
                "session_id": authority.session_id,
                "owner_id": authority.owner_id,
                "grant_id": authority.grant.grant_id,
                "lease_id": authority.lease_id,
                "previous_task_version": authority.task_version,
                "task_version": authority.task_version + 1,
                "deletion_version": authority.deletion_version,
                "owner_epoch": authority.owner_epoch,
                "native_epoch": authority.native_epoch,
                "transport_epoch": authority.transport_epoch,
                "state": "FENCED",
                "grant": authority.grant.as_document(),
                "superseded_authority_digest": authority.digest,
                "reason": "authority_revoked",
                "abandoned_at": "2026-07-22T00:00:09.000Z",
            },
        )
        raw = canonical_validated_current_bytes(
            "agent-claim-abandon-response/v1", document
        )
        return AgentClaimAbandonResponse.from_canonical_bytes(raw)

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
        self.assertEqual((6, 1, 1, 1, 0, 0), self.counts())
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
                self.assertEqual((0, 0, 0, 0, 0, 0), self.counts())
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

        store.record_server_ack(checkpoint_ack_bytes(first_commit, self.bootstrap))
        self.assertEqual(first_commit, store.recover(self.bootstrap.task_id).commit)
        store.record_server_ack(checkpoint_ack_bytes(second_commit, self.bootstrap))
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

    def test_recovery_validates_the_complete_ancestor_chain_before_resuming(self) -> None:
        store = CurrentCheckpointStore(self.database)
        first = checkpoint_documents(1)
        first_commit = store.commit(*first)
        first_manifest = manifest_document(first[0])
        second = checkpoint_documents(2, previous_manifest=first_manifest)
        second_commit = store.commit(*second)
        second_manifest = manifest_document(second[0])
        third = checkpoint_documents(3, previous_manifest=second_manifest)
        third_commit = store.commit(*third)
        for item in (first_commit, second_commit, third_commit):
            store.record_server_ack(checkpoint_ack_bytes(item, self.bootstrap))

        connection = sqlite3.connect(self.database.path)
        digest = connection.execute(
            "SELECT machine_state_sha256 FROM checkpoint_manifests "
            "WHERE task_id=? AND generation=2",
            (self.bootstrap.task_id,),
        ).fetchone()[0]
        connection.execute("DROP TRIGGER checkpoint_objects_no_update")
        connection.execute(
            "UPDATE checkpoint_objects SET size_bytes=2, object_bytes=x'7b7d' "
            "WHERE sha256=?",
            (digest,),
        )
        connection.commit()
        connection.close()

        recovered = store.recover(self.bootstrap.task_id)
        self.assertEqual(first_commit, recovered.commit)
        self.assertEqual(1, recovered.generation)

    def test_fenced_authority_rejects_new_commit_and_server_ack(self) -> None:
        store = CurrentCheckpointStore(self.database)
        first = checkpoint_documents(1)
        first_commit = store.commit(*first)
        first_manifest = manifest_document(first[0])
        CurrentAuthorityProjection(self.database).record_fenced(
            self.fenced_authority(),
            expected_previous_digest=self.bootstrap.digest,
        )

        with self.assertRaises(CurrentCheckpointError) as ack_error:
            store.record_server_ack(
                checkpoint_ack_bytes(first_commit, self.bootstrap)
            )
        self.assertEqual("AUTHORITY_FENCED", ack_error.exception.code)
        with self.assertRaises(CurrentCheckpointError) as commit_error:
            store.commit(
                *checkpoint_documents(2, previous_manifest=first_manifest)
            )
        self.assertEqual("AUTHORITY_FENCED", commit_error.exception.code)
        self.assertEqual((6, 1, 1, 1, 0, 0), self.counts())

    def test_server_ack_bytes_are_exactly_verified_persisted_and_replayed(self) -> None:
        store = CurrentCheckpointStore(self.database)
        committed = store.commit(*checkpoint_documents(1))
        ack = checkpoint_ack_bytes(committed, self.bootstrap)

        self.assertEqual(committed, store.record_server_ack(ack))
        self.assertEqual(committed, store.record_server_ack(ack))
        self.assertEqual((6, 1, 1, 1, 1, 1), self.counts())

        with self.assertRaises(CurrentCheckpointError) as noncanonical:
            store.record_server_ack(ack + b"\n")
        self.assertEqual("CHECKPOINT_ACK_INVALID", noncanonical.exception.code)
        conflict = checkpoint_ack_bytes(
            committed,
            self.bootstrap,
            accepted_at="2026-07-22T00:02:01.000Z",
        )
        with self.assertRaises(CurrentCheckpointError) as conflicted:
            store.record_server_ack(conflict)
        self.assertEqual("CHECKPOINT_ACK_CONFLICT", conflicted.exception.code)
        self.assertEqual((6, 1, 1, 1, 1, 1), self.counts())

    def test_every_server_ack_write_stage_rolls_back_both_ack_rows(self) -> None:
        committed = CurrentCheckpointStore(self.database).commit(
            *checkpoint_documents(1)
        )
        ack = checkpoint_ack_bytes(committed, self.bootstrap)
        before = self.counts()
        for stage in ("ack.after_index", "ack.after_document", "ack.before_commit"):
            with self.subTest(stage=stage):
                def inject(actual: str, *, selected: str = stage) -> None:
                    if actual == selected:
                        raise RuntimeError(f"injected:{selected}")

                store = CurrentCheckpointStore(self.database, fault_hook=inject)
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    store.record_server_ack(ack)
                self.assertEqual(before, self.counts())


if __name__ == "__main__":
    unittest.main()
