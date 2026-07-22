from __future__ import annotations

import os
import hashlib
import json
import sqlite3
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest

from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
    CurrentDatabaseError,
)
from pullwise_worker.agent_kernel_current_objects import (
    CurrentObjectError,
    CurrentObjectStore,
)
from pullwise_worker.agent_kernel_current_package import (
    CURRENT_PACKAGE,
    CURRENT_TOOL_CATALOG,
    ServerAuthorityEnvelope,
    ServerDispatchGrant,
    canonical_validated_current_bytes,
    seal_current_document,
    verify_current_document_digest,
)
from pullwise_worker.agent_kernel_dispatch_journal import (
    CurrentDispatchJournal,
    CurrentJournalError,
)
from pullwise_worker.agent_kernel_gateway import (
    CheckedInvocation,
    PreparedDispatch,
    ToolDescriptor,
)
from pullwise_worker.agent_kernel_r0_read import R0ReadReceipt
from pullwise_worker.agent_kernel_source_state import (
    SourceEntry,
    SourceTreeSnapshot,
    diff_source_trees,
)


class _PackageRef:
    def __init__(self, suffix: str = "a") -> None:
        self.package_identity = "test-current-package"
        self.package_version = "1.0.0"
        self.content_sha256 = suffix * 64
        self.root_sha256 = "b" * 64

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (
            self.package_identity,
            self.package_version,
            self.content_sha256,
            self.root_sha256,
        )


class CurrentAgentKernelDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-agent-kernel-")
        self.root = Path(self.scratch.name) / "current"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_new_root_has_strict_sqlite_configuration_and_migration_one(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())

        self.assertEqual(self.root / "agent-kernel-current.sqlite3", database.path)
        self.assertNotIn("shadow", database.path.as_posix())
        with database.connect() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(2, connection.execute("PRAGMA synchronous").fetchone()[0])
            self.assertGreaterEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0], 1_000
            )
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
        if os.name == "posix":
            self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(database.path.stat().st_mode))

    def test_package_lock_rejects_non_lowercase_digest(self) -> None:
        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_PACKAGE_LOCK_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef("A"))

    def test_reopen_fails_closed_on_package_lock_mismatch(self) -> None:
        CurrentAgentKernelDatabase.open(self.root, _PackageRef("a"))

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_PACKAGE_LOCK_MISMATCH"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef("c"))

    def test_reopen_fails_closed_on_unknown_schema_version(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        connection = sqlite3.connect(database.path)
        connection.execute("PRAGMA user_version = 99")
        connection.close()

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_SCHEMA_UNKNOWN"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())

    def test_reopen_rejects_hardlinked_database_file(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        os.link(database.path, self.root / "unexpected-hardlink.sqlite3")

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_DATABASE_FILE_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())

    def test_root_symlink_is_rejected_when_platform_can_create_one(self) -> None:
        target = Path(self.scratch.name) / "target"
        target.mkdir()
        try:
            self.root.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_DATABASE_ROOT_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())


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


class CurrentDispatchJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-journal-")
        self.root = Path(self.scratch.name) / "current"
        self.database = CurrentAgentKernelDatabase.open(self.root, CURRENT_PACKAGE)
        self.objects = CurrentObjectStore(self.root / "content")
        self.journal = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
            clock=lambda: "2026-07-22T12:34:56.000Z",
        )
        self.authority = self._authority()
        self.journal.record_authority(self.authority)
        self.call = self._call(self.authority)
        self.descriptor = CURRENT_TOOL_CATALOG.resolve("internal.read_source")
        self.before = SourceTreeSnapshot("a" * 40, "b" * 64, ())
        self.prepared = PreparedDispatch(
            self.descriptor.tool_key, self.descriptor.tool_version,
            self.before, object(),
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _authority(
        self, *, task_version: int = 1, native_epoch: int = 3,
        digest_char: str = "c",
    ) -> ServerAuthorityEnvelope:
        grant = seal_current_document("agent-worker-grant/v1", {
            "schema_id": "agent-worker-grant/v1",
            "package": CURRENT_PACKAGE.as_document(),
            "grant_id": f"grant_{digest_char * 32}",
            "task_id": "task_" + "1" * 32,
            "attempt_id": "attempt_" + "2" * 32,
            "session_id": "sess_" + "3" * 32,
            "owner_id": "owner_" + "4" * 32,
            "lease_id": "lease_" + "5" * 32,
            "task_version": task_version, "deletion_version": 0,
            "owner_epoch": 2, "native_epoch": native_epoch,
            "transport_epoch": 4, "policy_digest": "6" * 64,
            "capability_ids": ["source.read"],
            "tool_keys": ["internal.read_source"],
            "elapsed_budget_ms": 60_000, "tool_call_limit": 2,
        })
        typed_grant = ServerDispatchGrant.from_document(grant)
        envelope = seal_current_document("server-authority-envelope/v1", {
            "schema_id": "server-authority-envelope/v1",
            "package": CURRENT_PACKAGE.as_document(),
            "task_id": typed_grant.task_id, "attempt_id": typed_grant.attempt_id,
            "session_id": typed_grant.session_id, "owner_id": typed_grant.owner_id,
            "lease_id": typed_grant.lease_id, "task_version": task_version,
            "deletion_version": 0, "owner_epoch": 2,
            "native_epoch": native_epoch, "transport_epoch": 4,
            "lifecycle": "ACTIVE", "desired_state": "RUN",
            "grant": typed_grant.as_document(),
        })
        return ServerAuthorityEnvelope.from_canonical_bytes(
            canonical_validated_current_bytes("server-authority-envelope/v1", envelope),
        )

    @staticmethod
    def _call(authority: ServerAuthorityEnvelope) -> CheckedInvocation:
        return CheckedInvocation(
            "idem-current-r0", "7" * 64, authority.digest,
            authority.package.content_sha256, authority.package.root_sha256,
            authority.grant_digest, authority.task_id, authority.attempt_id,
            authority.owner_id, authority.session_id, authority.lease_id,
            authority.task_version, authority.deletion_version,
            authority.owner_epoch, authority.native_epoch,
            authority.transport_epoch, "internal.read_source",
            {"relative_path": "README.md"},
        )

    def _begin(self):
        plan = self.journal.plan_reservation(
            self.authority, self.call, self.descriptor
        )
        return self.journal.begin(
            self.authority, self.call, self.descriptor, self.prepared, plan
        )

    def _outcome(self, capability):
        raw = R0ReadReceipt(b"current payload", hashlib.sha256(b"current payload").hexdigest(), 15)
        payload = self.journal.publish_payload(capability, raw)
        receipt = seal_current_document("local-tool-receipt/v1", {
            "schema_id": "local-tool-receipt/v1",
            "receipt_kind": "local_tool", "tool_key": self.call.tool_key,
            "invocation_digest": self.call.invocation_digest,
            "status": "succeeded", "payload_ref": payload.content_ref,
            "started_at": "2026-07-22T12:34:55.000Z",
            "completed_at": "2026-07-22T12:34:56.000Z", "elapsed_ms": 1_000,
        })
        return SimpleNamespace(raw=raw, payload=payload, receipt=receipt)

    def test_plan_is_pure_and_begin_atomically_reserves_intent(self) -> None:
        plan = self.journal.plan_reservation(self.authority, self.call, self.descriptor)
        with self.database.connect() as connection:
            self.assertEqual((0, 0), tuple(connection.execute(
                "SELECT active_reserved, consumed FROM tool_call_budgets"
            ).fetchone()))
            self.assertEqual(0, connection.execute("SELECT count(*) FROM dispatch_intents").fetchone()[0])
        decision = self.journal.begin(
            self.authority, self.call, self.descriptor, self.prepared, plan
        )
        self.assertEqual("WINNER", decision.kind)
        self.assertEqual("PENDING", self.journal.probe("idem-current-r0", "7" * 64).kind)
        reopened = CurrentDispatchJournal(self.database, object_store=self.objects)
        self.assertEqual("PENDING", reopened.probe("idem-current-r0", "7" * 64).kind)
        self.assertEqual("PENDING", reopened.begin(
            self.authority, self.call, self.descriptor, self.prepared, plan
        ).kind)
        self.assertEqual(self.authority.canonical_bytes,
                         reopened.resolve_authority("idem-current-r0").canonical_bytes)

    def test_conflict_and_successor_never_reissue_old_capability(self) -> None:
        capability = self._begin().dispatch_capability
        with self.assertRaisesRegex(CurrentJournalError, "IDEMPOTENCY_CONFLICT"):
            self.journal.probe("idem-current-r0", "8" * 64)
        successor = self._authority(task_version=2, native_epoch=4, digest_char="d")
        self.journal.record_authority(
            successor, expected_previous_digest=self.authority.digest
        )
        self.assertEqual(self.authority.canonical_bytes,
                         self.journal.resolve_authority("idem-current-r0").canonical_bytes)
        with self.assertRaisesRegex(CurrentJournalError, "AUTHORITY_FENCED"):
            self.journal.consume_capability(capability)

    def test_capability_is_one_shot_and_dispatched_is_ambiguous(self) -> None:
        capability = self._begin().dispatch_capability
        self.journal.consume_capability(capability)
        with self.assertRaisesRegex(CurrentJournalError, "CAPABILITY_ALREADY_CONSUMED"):
            self.journal.consume_capability(capability)
        with self.assertRaisesRegex(CurrentJournalError, "DISPATCH_AMBIGUOUS"):
            self.journal.abandon_intent(capability, "DISPATCH_CANCELLED")
        self.assertEqual("PENDING", self.journal.probe("idem-current-r0", "7" * 64).kind)

    def test_intent_abandon_releases_budget_and_has_exact_replay(self) -> None:
        capability = self._begin().dispatch_capability
        first = self.journal.abandon_intent(capability, "DISPATCH_CANCELLED")
        second = self.journal.abandon_intent(capability, "DISPATCH_CANCELLED")
        self.assertEqual(first, second)
        replay = self.journal.probe("idem-current-r0", "7" * 64)
        self.assertEqual(("COMPLETED", first), (replay.kind, replay.result))
        with self.database.connect() as connection:
            self.assertEqual((0, 0), tuple(connection.execute(
                "SELECT active_reserved, consumed FROM tool_call_budgets"
            ).fetchone()))

    def test_settlement_binds_cas_receipt_result_observation_and_budget(self) -> None:
        capability = self._begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self._outcome(capability)
        replay = self.journal.commit(
            capability, self.call, self.prepared, outcome, self.before
        )
        result = verify_current_document_digest("r0-read-result/v1", json.loads(replay))
        self.assertEqual(self.before.source_state_id, result["source_state_after_id"])
        self.assertEqual(replay, self.journal.commit(
            capability, self.call, self.prepared, outcome, self.before
        ))
        self.assertEqual(replay, self.journal.probe(
            "idem-current-r0", "7" * 64
        ).result)
        with self.database.connect() as connection:
            self.assertEqual((0, 1), tuple(connection.execute(
                "SELECT active_reserved, consumed FROM tool_call_budgets"
            ).fetchone()))
            self.assertEqual(2, connection.execute("SELECT count(*) FROM content_bindings").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM dispatch_settlements").fetchone()[0])

    def test_settlement_crash_rolls_back_binding_and_remains_pending(self) -> None:
        capability = self._begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self._outcome(capability)
        crashing = CurrentDispatchJournal(
            self.database, object_store=self.objects,
            fault_hook=lambda stage: (_ for _ in ()).throw(RuntimeError("crash"))
            if stage == "before_settlement_commit" else None,
        )
        with self.assertRaisesRegex(RuntimeError, "crash"):
            crashing.commit(capability, self.call, self.prepared, outcome, self.before)
        self.assertEqual("PENDING", self.journal.probe("idem-current-r0", "7" * 64).kind)
        with self.database.connect() as connection:
            self.assertEqual((0, 0), tuple(row[0] for row in connection.execute(
                "SELECT count(*) FROM content_bindings UNION ALL "
                "SELECT count(*) FROM dispatch_settlements"
            ).fetchall()))
        self.assertEqual(outcome.raw.payload, self.objects.read_verified(outcome.payload.object))

    def test_server_transport_receipt_is_explicitly_rejected(self) -> None:
        capability = self._begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self._outcome(capability)
        outcome.receipt.clear()
        outcome.receipt.update({"schema_id": "transport-receipt/v1", "receipt_kind": "server_transport"})
        with self.assertRaisesRegex(CurrentJournalError, "SERVER_TRANSPORT_RECEIPT_FORBIDDEN"):
            self.journal.commit(capability, self.call, self.prepared, outcome, self.before)

    def test_source_violation_persists_evidence_and_withholds_normal_result(self) -> None:
        capability = self._begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self._outcome(capability)
        after = SourceTreeSnapshot("a" * 40, "b" * 64, (
            SourceEntry.file("changed.txt", size_bytes=1, sha256="e" * 64),
        ))
        replay = self.journal.commit_source_violation(
            capability, self.call, self.prepared, outcome, after,
            diff_source_trees(self.before, after),
        )
        self.assertEqual("SOURCE_MUTATION_FORBIDDEN", json.loads(replay)["code"])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT result_bytes IS NOT NULL, violation_bytes, observation_bytes "
                "FROM dispatch_settlements"
            ).fetchone()
        self.assertEqual(1, row[0])
        self.assertTrue(row[1])
        self.assertEqual("policy_violation", json.loads(row[2])["status"])


if __name__ == "__main__":
    unittest.main()
