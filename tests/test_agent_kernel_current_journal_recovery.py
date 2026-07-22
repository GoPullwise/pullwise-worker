from __future__ import annotations

import hashlib
import json
import os

from pullwise_worker.agent_kernel_current_gc import collect_current_orphans
from pullwise_worker.agent_kernel_current_package import (
    verify_current_document_digest,
)
from pullwise_worker.agent_kernel_dispatch_journal import (
    CurrentDispatchJournal,
    CurrentJournalError,
)
from pullwise_worker.agent_kernel_r0_read import R0ReadReceipt

from tests.current_journal_support import CurrentJournalTestCase


class CurrentJournalRecoveryTest(CurrentJournalTestCase):
    def test_exact_fenced_replay_still_requires_original_cas_token(self) -> None:
        fenced = self.make_fenced_head(self.authority)
        self.journal.record_authority(
            fenced,
            expected_previous_digest=self.authority.digest,
        )

        with self.assertRaisesRegex(
            CurrentJournalError, "FENCED_AUTHORITY_REPLAY_CONFLICT"
        ):
            self.journal.record_authority(
                fenced,
                expected_previous_digest="f" * 64,
            )

    def test_restart_recovery_fences_and_abandons_intent_exactly_once(self) -> None:
        self.begin()
        fenced = self.make_fenced_head(self.authority)
        reopened = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
            clock=lambda: "2026-07-22T12:34:56.000Z",
        )

        first = reopened.recover_abandon(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
            fenced,
        )
        second = reopened.recover_abandon(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
            fenced,
        )

        self.assertEqual(first, second)
        self.assertEqual((0, 0, 0, 0), self.budget())
        replay = reopened.probe(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
        )
        self.assertEqual(("COMPLETED", first), (replay.kind, replay.result))
        with self.assertRaisesRegex(CurrentJournalError, "AUTHORITY_FENCED"):
            reopened.assert_actor_current(self.call)
        historical = reopened.resolve_authority(
            self.call.task_id,
            self.call.idempotency_key,
        )
        self.assertEqual(self.authority.canonical_bytes, historical.canonical_bytes)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT budget_settlement_bytes FROM dispatch_abandonments"
            ).fetchone()
        budget = verify_current_document_digest(
            "elapsed-budget-settlement/v1", json.loads(row[0])
        )
        self.assertEqual(
            (0, 60_000, 0, 1, "abandoned"),
            (
                budget["consumed_ms"],
                budget["released_ms"],
                budget["consumed_calls"],
                budget["released_calls"],
                budget["outcome"],
            ),
        )

    def test_recovery_crash_rolls_back_fenced_head_and_budget_release(self) -> None:
        self.begin()
        fenced = self.make_fenced_head(self.authority)
        crashing = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
            clock=lambda: "2026-07-22T12:34:56.000Z",
            fault_hook=lambda stage: (
                (_ for _ in ()).throw(RuntimeError("crash"))
                if stage == "before_abandonment_commit"
                else None
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "crash"):
            crashing.recover_abandon(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
                fenced,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        self.assertEqual(
            self.authority.digest,
            self.journal.assert_actor_current(self.call).digest,
        )
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM dispatch_abandonments"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_dispatched_recovery_fences_but_never_abandons_or_redrives(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        fenced = self.make_fenced_head(self.authority)
        reopened = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
        )

        with self.assertRaisesRegex(CurrentJournalError, "DISPATCH_AMBIGUOUS"):
            reopened.recover_abandon(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
                fenced,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        with self.assertRaisesRegex(CurrentJournalError, "AUTHORITY_FENCED"):
            reopened.assert_actor_current(self.call)
        replay = reopened.probe(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
        )
        self.assertEqual("PENDING", replay.kind)
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM dispatch_abandonments"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_fenced_dispatched_predecessor_cannot_late_commit_success(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability)
        with self.assertRaisesRegex(
            CurrentJournalError, "CURRENT_OBJECT_COLLECTION_NOT_IDLE"
        ):
            collect_current_orphans(
                self.database,
                self.objects,
                min_age_seconds=60,
                now=120,
            )
        fenced = self.make_fenced_head(self.authority)

        with self.assertRaisesRegex(CurrentJournalError, "DISPATCH_AMBIGUOUS"):
            self.journal.recover_abandon(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
                fenced,
            )
        with self.assertRaisesRegex(CurrentJournalError, "AUTHORITY_FENCED"):
            self.journal.commit(
                capability,
                self.call,
                self.prepared,
                outcome,
                self.before,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        with self.database.connect() as connection:
            intent = connection.execute(
                "SELECT state FROM dispatch_intents"
            ).fetchone()[0]
            settlements = connection.execute(
                "SELECT count(*) FROM dispatch_settlements"
            ).fetchone()[0]
            objects = connection.execute(
                "SELECT count(*) FROM content_objects"
            ).fetchone()[0]
            bindings = connection.execute(
                "SELECT count(*) FROM content_bindings"
            ).fetchone()[0]
        self.assertEqual(("DISPATCHED", 0), (intent, settlements))
        self.assertEqual((0, 0), (objects, bindings))
        orphan_files = [
            path for path in self.objects.objects.rglob("*") if path.is_file()
        ]
        self.assertEqual(3, len(orphan_files))
        for path in orphan_files:
            os.utime(path, (0, 0))
        removed = collect_current_orphans(
            self.database,
            self.objects,
            min_age_seconds=60,
            now=120,
        )
        self.assertEqual(3, len(removed))
        self.assertFalse(
            any(path.is_file() for path in self.objects.objects.rglob("*"))
        )

    def test_fence_after_dispatch_blocks_payload_publication(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        payload = b"dispatcher already returned"
        raw = R0ReadReceipt(
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        fenced = self.make_fenced_head(self.authority)

        with self.assertRaisesRegex(CurrentJournalError, "DISPATCH_AMBIGUOUS"):
            self.journal.recover_abandon(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
                fenced,
            )
        with self.assertRaisesRegex(CurrentJournalError, "AUTHORITY_FENCED"):
            self.journal.publish_payload(capability, raw)

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        self.assertFalse(
            any(path.is_file() for path in self.objects.objects.rglob("*"))
        )

    def test_recovery_rejects_unrelated_fenced_projection(self) -> None:
        self.begin()
        unrelated = self.make_authority(identity_char="2", grant_char="d")
        unrelated_fenced = self.make_fenced_head(unrelated)

        with self.assertRaisesRegex(
            CurrentJournalError, "FENCED_AUTHORITY_INVOCATION_CONFLICT"
        ):
            self.journal.recover_abandon(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
                unrelated_fenced,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        self.assertEqual(
            self.authority.digest,
            self.journal.assert_actor_current(self.call).digest,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
