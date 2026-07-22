from __future__ import annotations

import base64
import json

from pullwise_worker.agent_kernel_current_package import (
    validate_current_document,
    verify_current_document_digest,
)
from pullwise_worker.agent_kernel_dispatch_journal import (
    CurrentDispatchJournal,
    CurrentJournalError,
)
from pullwise_worker.agent_kernel_source_state import (
    SourceEntry,
    SourceTreeSnapshot,
    diff_source_trees,
)

from tests.current_journal_support import CurrentJournalTestCase


class CurrentDispatchJournalTest(CurrentJournalTestCase):
    def test_plan_is_pure_and_begin_atomically_reserves_intent(self) -> None:
        plan = self.journal.plan_reservation(
            self.authority,
            self.call,
            self.descriptor,
        )
        self.assertEqual((0, 0, 0, 0), self.budget())
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM dispatch_intents"
            ).fetchone()[0]
        self.assertEqual(0, count)

        decision = self.journal.begin(
            self.authority,
            self.call,
            self.descriptor,
            self.prepared,
            plan,
        )

        self.assertEqual("WINNER", decision.kind)
        self.assertEqual((0, 60_000, 0, 1), self.budget())
        replay = self.journal.probe(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
        )
        self.assertEqual("PENDING", replay.kind)

        reopened = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
        )
        reopened_replay = reopened.probe(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
        )
        self.assertEqual("PENDING", reopened_replay.kind)
        self.assertEqual(
            "PENDING",
            reopened.begin(
                self.authority,
                self.call,
                self.descriptor,
                self.prepared,
                plan,
            ).kind,
        )
        historical = reopened.resolve_authority(
            self.call.task_id,
            self.call.idempotency_key,
        )
        self.assertEqual(self.authority.canonical_bytes, historical.canonical_bytes)

    def test_same_agent_key_is_isolated_by_trusted_task(self) -> None:
        second_authority = self.make_authority(
            identity_char="2",
            grant_char="d",
        )
        self.journal.record_authority(second_authority)
        second_call = self.make_call(
            second_authority,
            key=self.call.idempotency_key,
            digest_char="8",
        )
        second_plan = self.journal.plan_reservation(
            second_authority,
            second_call,
            self.descriptor,
        )

        first = self.begin()
        second = self.journal.begin(
            second_authority,
            second_call,
            self.descriptor,
            self.prepared,
            second_plan,
        )

        self.assertEqual(("WINNER", "WINNER"), (first.kind, second.kind))
        self.assertEqual(
            "PENDING",
            self.journal.probe(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
            ).kind,
        )
        self.assertEqual(
            "PENDING",
            self.journal.probe(
                second_call.task_id,
                second_call.idempotency_key,
                second_call.invocation_digest,
            ).kind,
        )

    def test_capability_is_one_shot_and_dispatched_is_ambiguous(self) -> None:
        capability = self.begin().dispatch_capability

        self.journal.consume_capability(capability)

        with self.assertRaisesRegex(
            CurrentJournalError, "CAPABILITY_ALREADY_CONSUMED"
        ):
            self.journal.consume_capability(capability)
        with self.assertRaisesRegex(CurrentJournalError, "DISPATCH_AMBIGUOUS"):
            self.journal.abandon_intent(capability, "dispatch_not_started")
        self.assertEqual(
            "PENDING",
            self.journal.probe(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
            ).kind,
        )

    def test_intent_abandon_releases_both_budgets_with_exact_replay(self) -> None:
        capability = self.begin().dispatch_capability

        first = self.journal.abandon_intent(capability, "dispatch_not_started")
        second = self.journal.abandon_intent(capability, "dispatch_not_started")

        self.assertEqual(first, second)
        self.assertEqual((0, 0, 0, 0), self.budget())
        replay = self.journal.probe(
            self.call.task_id,
            self.call.idempotency_key,
            self.call.invocation_digest,
        )
        self.assertEqual(("COMPLETED", first), (replay.kind, replay.result))
        abandonment = verify_current_document_digest(
            "dispatch-abandonment/v1", json.loads(first)
        )
        self.assertEqual("dispatch_not_started", abandonment["reason"])

    def test_settlement_binds_exact_typed_cas_dag_and_budget(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability)

        replay = self.journal.commit(
            capability,
            self.call,
            self.prepared,
            outcome,
            self.before,
        )

        result = verify_current_document_digest(
            "r0-read-result/v1", json.loads(replay)
        )
        self.assertEqual(self.before.source_state_id, result["source_state_after_id"])
        self.assertEqual(outcome.payload.content_ref, result["payload_ref"])
        self.assertEqual((1_000, 0, 1, 0), self.budget())

        source_document = verify_current_document_digest(
            "source-content/v1",
            json.loads(self.objects.read_verified(outcome.payload.source.object)),
        )
        payload_document = verify_current_document_digest(
            "r0-read-payload/v1",
            json.loads(self.objects.read_verified(outcome.payload.payload.object)),
        )
        self.assertEqual(
            outcome.raw.payload,
            base64.b64decode(source_document["data_base64"], validate=True),
        )
        self.assertEqual(
            outcome.payload.source.content_ref,
            payload_document["content_ref"],
        )

        exact = self.journal.commit(
            capability,
            self.call,
            self.prepared,
            outcome,
            self.before,
        )
        self.assertEqual(replay, exact)
        with self.database.connect() as connection:
            schemas = {
                row[0]
                for row in connection.execute(
                    "SELECT content_schema_id FROM content_bindings"
                )
            }
            settlements = connection.execute(
                "SELECT count(*) FROM dispatch_settlements"
            ).fetchone()[0]
            record = connection.execute(
                "SELECT receipt_bytes, observation_bytes, observation_seq "
                "FROM dispatch_settlements"
            ).fetchone()
        self.assertEqual(
            {"source-content/v1", "r0-read-payload/v1", "r0-read-result/v1"},
            schemas,
        )
        self.assertEqual(1, settlements)
        receipt = verify_current_document_digest(
            "local-tool-receipt/v1", json.loads(record[0])
        )
        observation = verify_current_document_digest(
            "observation/v1", json.loads(record[1])
        )
        self.assertEqual("local_tool", receipt["receipt_kind"])
        self.assertEqual(outcome.payload.content_ref, receipt["payload_ref"])
        self.assertEqual(1, record[2])
        self.assertEqual(
            {
                "schema_id",
                "observation_id",
                "task_id",
                "attempt_id",
                "native_epoch",
                "actor",
                "tool_id",
                "tool_version",
                "tool_invocation_id",
                "idempotency_key",
                "input_digest",
                "status",
                "started_at",
                "completed_at",
                "duration_ms",
                "exit_code",
                "source_state_before_id",
                "source_state_after_id",
                "execution_state_id",
                "stdout_ref",
                "stderr_ref",
                "result_ref",
                "redaction_report_ref",
                "partial_side_effect",
                "observation_seq",
                "observation_digest",
            },
            set(observation),
        )
        self.assertEqual(
            {
                "schema_id": "actor/v1",
                "kind": "task_owner",
                "id": self.call.owner_id,
                "session_id": self.call.session_id,
            },
            observation["actor"],
        )
        self.assertEqual(self.prepared.tool_version, observation["tool_version"])
        self.assertEqual(self.call.idempotency_key, observation["idempotency_key"])
        self.assertEqual(self.call.invocation_digest, observation["input_digest"])
        self.assertEqual(
            "r0-read-result/v1",
            observation["result_ref"]["ref"]["content_schema_id"],
        )
        self.assertEqual(
            {"availability": "not_applicable", "reason_code": "IN_PROCESS_TOOL"},
            observation["stdout_ref"],
        )
        self.assertEqual(
            {
                "availability": "not_applicable",
                "reason_code": "REDACTION_NOT_REQUIRED",
            },
            observation["redaction_report_ref"],
        )

    def test_settlement_crash_leaves_only_orphan_cas_and_pending_intent(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability)
        crashing = CurrentDispatchJournal(
            self.database,
            object_store=self.objects,
            fault_hook=lambda stage: (
                (_ for _ in ()).throw(RuntimeError("crash"))
                if stage == "before_settlement_commit"
                else None
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "crash"):
            crashing.commit(
                capability,
                self.call,
                self.prepared,
                outcome,
                self.before,
            )

        self.assertEqual((0, 60_000, 0, 1), self.budget())
        self.assertEqual(
            "PENDING",
            self.journal.probe(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
            ).kind,
        )
        with self.database.connect() as connection:
            self.assertEqual(
                (0, 0, 0),
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT count(*) FROM content_objects UNION ALL "
                        "SELECT count(*) FROM content_bindings UNION ALL "
                        "SELECT count(*) FROM dispatch_settlements"
                    )
                ),
            )
        orphan_files = [
            path for path in self.objects.objects.rglob("*") if path.is_file()
        ]
        self.assertEqual(3, len(orphan_files))

    def test_server_transport_receipt_is_explicitly_rejected(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability)
        outcome.receipt.clear()
        outcome.receipt.update(
            {
                "schema_id": "server-transport-receipt/v1",
                "receipt_kind": "server_transport",
            }
        )

        with self.assertRaisesRegex(
            CurrentJournalError, "SERVER_TRANSPORT_RECEIPT_FORBIDDEN"
        ):
            self.journal.commit(
                capability,
                self.call,
                self.prepared,
                outcome,
                self.before,
            )

    def test_source_violation_emits_stable_error_and_withholds_result(self) -> None:
        capability = self.begin().dispatch_capability
        self.journal.consume_capability(capability)
        outcome = self.outcome(capability)
        after = SourceTreeSnapshot(
            "a" * 40,
            "b" * 64,
            (SourceEntry.file("changed.txt", size_bytes=1, sha256="e" * 64),),
        )

        replay = self.journal.commit_source_violation(
            capability,
            self.call,
            self.prepared,
            outcome,
            after,
            diff_source_trees(self.before, after),
        )

        response = validate_current_document(
            "error-response/v1", json.loads(replay)
        )
        self.assertEqual("SOURCE_STATE_CHANGED", response["error"]["code"])
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual("new_claim", response["error"]["retry_scope"])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT outcome_schema_id, violation_bytes, observation_bytes "
                "FROM dispatch_settlements"
            ).fetchone()
            schemas = {
                item[0]
                for item in connection.execute(
                    "SELECT content_schema_id FROM content_bindings"
                )
            }
        violation = verify_current_document_digest(
            "stable-error/v1", json.loads(row[1])
        )
        observation = verify_current_document_digest(
            "observation/v1", json.loads(row[2])
        )
        self.assertEqual("SOURCE_STATE_CHANGED", violation["code"])
        self.assertEqual("error-response/v1", row[0])
        self.assertEqual("policy_violation", observation["status"])
        self.assertEqual(
            "error-response/v1",
            observation["result_ref"]["ref"]["content_schema_id"],
        )
        self.assertNotIn("r0-read-result/v1", schemas)


if __name__ == "__main__":
    import unittest

    unittest.main()
