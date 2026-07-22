from __future__ import annotations

import hashlib
import json

from pullwise_worker.agent_kernel_current_package import (
    CURRENT_TOOL_CATALOG,
    verify_current_document_digest,
)
from pullwise_worker.agent_kernel_current_r0_execution import (
    CurrentR0ExecutionAdapter,
)
from pullwise_worker.agent_kernel_gateway import AgentKernelGateway
from pullwise_worker.agent_kernel_r0_read import R0ReadReceipt

from tests.current_journal_support import CurrentJournalTestCase


class _Codec:
    def __init__(self, call: object) -> None:
        self.call = call

    def validate(self, raw: bytes) -> object:
        del raw
        return self.call


class _Policy:
    @staticmethod
    def assert_capability(*args: object) -> None:
        del args

    @staticmethod
    def assert_execution_controls(*args: object) -> None:
        del args


class _Preparer:
    def __init__(self, prepared: object, source_after: object) -> None:
        self.prepared = prepared
        self.source_after = source_after

    def prepare(self, *args: object) -> object:
        del args
        return self.prepared

    def capture_after(self, prepared: object) -> object:
        if prepared is not self.prepared:
            raise AssertionError("unexpected prepared dispatch")
        return self.source_after

    @staticmethod
    def discard(prepared: object) -> None:
        del prepared


class _RawDispatcher:
    def __init__(self, case: CurrentJournalTestCase, payload: bytes) -> None:
        self.case = case
        self.payload = payload
        self.calls = 0

    def dispatch(self, capability: object, prepared: object) -> R0ReadReceipt:
        del capability, prepared
        self.calls += 1
        with self.case.database.connect() as connection:
            state = connection.execute(
                "SELECT state FROM dispatch_intents"
            ).fetchone()[0]
        if state != "DISPATCHED":
            raise AssertionError("tool dispatch began before capability consumption")
        return R0ReadReceipt(
            payload=self.payload,
            sha256=hashlib.sha256(self.payload).hexdigest(),
            size_bytes=len(self.payload),
        )


class CurrentR0ExecutionAdapterTest(CurrentJournalTestCase):
    def test_gateway_adapter_consumes_publishes_receipts_and_settles(self) -> None:
        payload = b"journal-aware current payload"
        raw_dispatcher = _RawDispatcher(self, payload)
        timestamps = iter(
            ("2026-07-22T12:34:55.000Z", "2026-07-22T12:34:56.000Z")
        )
        adapter = CurrentR0ExecutionAdapter(
            self.journal,
            dispatcher=raw_dispatcher,
            clock=lambda: next(timestamps),
        )
        gateway = AgentKernelGateway(
            codec=_Codec(self.call),
            journal=self.journal,
            authority=self.journal,
            catalog=CURRENT_TOOL_CATALOG,
            policy=_Policy(),
            preparer=_Preparer(self.prepared, self.before),
            budget=self.journal,
            dispatcher=adapter,
            committer=adapter,
        )

        replay = gateway.invoke(b"canonical request bytes")

        result = verify_current_document_digest("r0-read-result/v1", json.loads(replay))
        self.assertEqual(self.call.invocation_digest, result["invocation_digest"])
        self.assertEqual(1, raw_dispatcher.calls)
        self.assertEqual((1_000, 0, 1, 0), self.budget())
        self.assertEqual(
            "COMPLETED",
            self.journal.probe(
                self.call.task_id,
                self.call.idempotency_key,
                self.call.invocation_digest,
            ).kind,
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT receipt_bytes, observation_bytes FROM dispatch_settlements"
            ).fetchone()
        receipt = verify_current_document_digest(
            "local-tool-receipt/v1", json.loads(row[0])
        )
        observation = verify_current_document_digest(
            "observation/v1", json.loads(row[1])
        )
        self.assertEqual(1_000, receipt["elapsed_ms"])
        self.assertEqual("succeeded", observation["status"])
        self.assertEqual(
            receipt["payload_ref"]["artifact_id"],
            result["payload_ref"]["artifact_id"],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
