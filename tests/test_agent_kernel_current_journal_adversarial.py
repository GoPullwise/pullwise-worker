from __future__ import annotations

from pullwise_worker.agent_kernel_current_package import (
    ServerAuthorityEnvelope,
    ServerDispatchGrant,
    canonical_validated_current_bytes,
    seal_current_document,
)
from pullwise_worker.agent_kernel_dispatch_journal import CurrentJournalError
from pullwise_worker.agent_kernel_gateway import ToolDescriptor

from tests.current_journal_support import CurrentJournalTestCase


class CurrentJournalAdversarialTest(CurrentJournalTestCase):
    def test_begin_resolves_catalog_again_and_rejects_forged_version(self) -> None:
        plan = self.journal.plan_reservation(
            self.authority,
            self.call,
            self.descriptor,
        )
        forged = ToolDescriptor(
            tool_key=self.descriptor.tool_key,
            tool_version="999.0.0",
            risk=self.descriptor.risk,
            capability=self.descriptor.capability,
            uses_command=self.descriptor.uses_command,
            uses_network=self.descriptor.uses_network,
            uses_secret=self.descriptor.uses_secret,
            requests_approval=self.descriptor.requests_approval,
        )

        with self.assertRaisesRegex(
            CurrentJournalError, "DISPATCH_NOT_AUTHORIZED"
        ):
            self.journal.begin(
                self.authority,
                self.call,
                forged,
                self.prepared,
                plan,
            )

        self.assertEqual((0, 0, 0, 0), self.budget())
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM dispatch_intents"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_active_successor_is_forbidden_and_cannot_reset_budget(self) -> None:
        successor = self._active_successor()

        with self.assertRaisesRegex(
            CurrentJournalError, "ACTIVE_AUTHORITY_SUCCESSOR_FORBIDDEN"
        ):
            self.journal.record_authority(
                successor,
                expected_previous_digest=self.authority.digest,
            )

        with self.database.connect() as connection:
            heads = connection.execute(
                "SELECT count(*) FROM authority_heads"
            ).fetchone()[0]
            budgets = connection.execute(
                "SELECT count(*) FROM dispatch_budgets"
            ).fetchone()[0]
        self.assertEqual((1, 1), (heads, budgets))
        self.assertEqual(self.authority.digest, self.journal.assert_actor_current(self.call).digest)

    def test_same_task_key_with_different_digest_is_a_conflict(self) -> None:
        self.begin()
        conflicting = self.make_call(
            self.authority,
            key=self.call.idempotency_key,
            digest_char="8",
        )

        with self.assertRaisesRegex(CurrentJournalError, "IDEMPOTENCY_CONFLICT"):
            self.journal.probe(
                conflicting.task_id,
                conflicting.idempotency_key,
                conflicting.invocation_digest,
            )

    def test_numeric_budget_tampering_is_detected_by_canonical_ledger(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE dispatch_budgets SET consumed_ms = 1 "
                "WHERE task_id = ? AND grant_digest = ?",
                (self.authority.task_id, self.authority.grant_digest),
            )

        with self.assertRaisesRegex(CurrentJournalError, "BUDGET_LEDGER_CORRUPT"):
            self.journal.plan_reservation(
                self.authority,
                self.call,
                self.descriptor,
            )

    def _active_successor(self) -> ServerAuthorityEnvelope:
        grant_document = self.authority.grant.as_document()
        grant_document.pop("grant_digest")
        grant_document.update(
            {
                "grant_id": "grant_" + "d" * 32,
                "attempt_id": "attempt_" + "d" * 32,
                "session_id": "sess_" + "d" * 32,
                "lease_id": "lease_" + "d" * 32,
                "task_version": 2,
                "owner_epoch": 3,
                "native_epoch": 4,
                "transport_epoch": 5,
            }
        )
        sealed_grant = seal_current_document(
            "agent-worker-grant/v1", grant_document
        )
        grant = ServerDispatchGrant.from_document(sealed_grant)
        envelope_document = self.authority.as_document()
        envelope_document.pop("authority_digest")
        envelope_document.update(
            {
                "attempt_id": grant.attempt_id,
                "session_id": grant.session_id,
                "lease_id": grant.lease_id,
                "task_version": grant.task_version,
                "owner_epoch": grant.owner_epoch,
                "native_epoch": grant.native_epoch,
                "transport_epoch": grant.transport_epoch,
                "grant": grant.as_document(),
            }
        )
        sealed = seal_current_document(
            "server-authority-envelope/v1", envelope_document
        )
        encoded = canonical_validated_current_bytes(
            "server-authority-envelope/v1", sealed
        )
        return ServerAuthorityEnvelope.from_canonical_bytes(encoded)


if __name__ == "__main__":
    import unittest

    unittest.main()
