from __future__ import annotations

import base64
import json

from pullwise_worker.agent_kernel_current_package import (
    verify_current_document_digest,
)

from tests.current_journal_support import CurrentJournalTestCase


class CurrentR0SettlementTest(CurrentJournalTestCase):
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


if __name__ == "__main__":
    import unittest

    unittest.main()
