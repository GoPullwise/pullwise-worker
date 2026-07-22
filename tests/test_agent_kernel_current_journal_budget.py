from __future__ import annotations

from pullwise_worker.agent_kernel_dispatch_journal import CurrentJournalError

from tests.current_journal_support import CurrentJournalTestCase


class CurrentJournalBudgetTest(CurrentJournalTestCase):
    def test_elapsed_budget_exhaustion_is_independent_of_call_limit(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability, elapsed_ms=60_000)
        self.journal.commit(
            capability,
            self.call,
            self.prepared,
            outcome,
            self.before,
        )
        next_call = self.make_call(
            self.authority,
            key="elapsed-exhausted",
            digest_char="8",
        )

        self.assertEqual((60_000, 0, 1, 0), self.budget())
        with self.assertRaisesRegex(CurrentJournalError, "BUDGET_EXHAUSTED"):
            self.journal.plan_reservation(
                self.authority,
                next_call,
                self.descriptor,
            )

    def test_call_budget_exhaustion_is_independent_of_elapsed_limit(self) -> None:
        calls = (
            self.call,
            self.make_call(
                self.authority,
                key="second-call",
                digest_char="8",
            ),
        )
        for call in calls:
            decision = self.begin(call)
            capability = decision.dispatch_capability
            self.journal.consume_capability(capability)
            outcome = self.outcome(capability, call=call, elapsed_ms=1_000)
            self.journal.commit(
                capability,
                call,
                self.prepared,
                outcome,
                self.before,
            )
        third_call = self.make_call(
            self.authority,
            key="third-call",
            digest_char="9",
        )

        self.assertEqual((2_000, 0, 2, 0), self.budget())
        with self.assertRaisesRegex(CurrentJournalError, "BUDGET_EXHAUSTED"):
            self.journal.plan_reservation(
                self.authority,
                third_call,
                self.descriptor,
            )

    def test_receipt_cannot_consume_more_elapsed_than_reserved(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability, elapsed_ms=60_001)

        with self.assertRaisesRegex(
            CurrentJournalError, "BUDGET_SETTLEMENT_OVERFLOW"
        ):
            self.journal.commit(
                capability,
                self.call,
                self.prepared,
                outcome,
                self.before,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        replay = self.journal.probe(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
        )
        self.assertEqual("PENDING", replay.kind)

    def test_begin_rejects_stale_pure_reservation_proposal(self) -> None:
        second_call = self.make_call(
            self.authority,
            key="concurrent-call",
            digest_char="8",
        )
        stale_plan = self.journal.plan_reservation(
            self.authority,
            second_call,
            self.descriptor,
        )
        self.begin()

        with self.assertRaisesRegex(
            CurrentJournalError, "BUDGET_RESERVATION_STALE"
        ):
            self.journal.begin(
                self.authority,
                second_call,
                self.descriptor,
                self.prepared,
                stale_plan,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        self.assertEqual(
            "NEW",
            self.journal.probe(
                second_call.task_id,
                second_call.idempotency_key,
                second_call.invocation_digest,
            ).kind,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
