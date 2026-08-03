from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pullwise_worker import _generated_agent_task_contract as contract
from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
)
from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
)
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from pullwise_worker.agent_kernel_current_terminalization import (
    CurrentTerminalizationStore,
)
from pullwise_worker.agent_kernel_current_transport import (
    CurrentTaskResultTransportError,
    CurrentTaskResultTransportStore,
)
from tests.current_runtime_bootstrap_support import bootstrap_bytes
from tests.current_s5_result_support import blocked_task_result_bytes
from tests.current_s5_support import (
    terminalization_inputs,
)


class CurrentTaskResultTransportStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s6-transport-")
        self.database = CurrentAgentKernelDatabase.open(
            Path(self.scratch.name) / "current", CURRENT_PACKAGE
        )
        self.authority = CurrentRuntimeBootstrapConsumer(
            self.database
        ).ingest(bootstrap_bytes())
        inputs = terminalization_inputs(self.database, self.authority)
        terminalization = CurrentTerminalizationStore(self.database)
        prepared = terminalization.prepare(
            **{key: value for key, value in inputs.items() if key != "documents"}
        )
        self.frozen = terminalization.freeze(
            prepared,
            blocked_task_result_bytes(
                self.authority, prepared, inputs["documents"]
            ),
            inputs["objects"],
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def ack_bytes(self, envelope_digest: str) -> bytes:
        document = contract.seal_document(
            "task-result-transport-ack/v1",
            {
                "schema_id": "task-result-transport-ack/v1",
                "package": CURRENT_PACKAGE.as_document(),
                "result_id": self.frozen.result_id,
                "task_id": self.frozen.task_id,
                "outcome": self.frozen.outcome,
                "published_from_version": self.frozen.published_from_version,
                "terminal_task_version": self.frozen.terminal_task_version,
                "transport_envelope_digest": envelope_digest,
                "receipt_binding_state": "not_applicable",
                "receipt_digest": None,
                "accepted_at": "2026-07-22T00:00:46.000Z",
            },
        )
        return contract.canonical_validated_bytes(
            "task-result-transport-ack/v1", document
        )

    def test_envelope_and_ack_are_exact_immutable_projections(self) -> None:
        store = CurrentTaskResultTransportStore(self.database)
        prepared = store.prepare(self.frozen.task_id)
        replay = store.prepare(self.frozen.task_id)

        self.assertEqual(prepared, replay)
        checked = contract.verify_task_result_transport_envelope(
            prepared.document, prepared.task_result_core
        )
        self.assertEqual(
            prepared.transport_envelope_digest,
            checked["transport_envelope_digest"],
        )
        self.assertEqual(
            self.frozen.task_version_authority_sha256,
            prepared.task_version_authority_sha256,
        )

        ack_bytes = self.ack_bytes(prepared.transport_envelope_digest)
        ack = store.acknowledge(ack_bytes)
        self.assertEqual(ack, store.acknowledge(ack_bytes))

        conflicting = deepcopy(ack.document)
        conflicting.pop("ack_digest")
        conflicting["accepted_at"] = "2026-07-22T00:00:47.000Z"
        conflicting = contract.seal_document(
            "task-result-transport-ack/v1", conflicting
        )
        with self.assertRaisesRegex(
            CurrentTaskResultTransportError,
            "TRANSPORT_ACK_CONFLICT",
        ):
            store.acknowledge(
                contract.canonical_validated_bytes(
                    "task-result-transport-ack/v1", conflicting
                )
            )

        connection = self.database.connect()
        try:
            head = connection.execute(
                "SELECT task_version,record_sha256 FROM runtime_task_heads "
                "WHERE task_id=?",
                (self.frozen.task_id,),
            ).fetchone()
            result_count = connection.execute(
                "SELECT COUNT(*) FROM terminalization_candidates WHERE task_id=?",
                (self.frozen.task_id,),
            ).fetchone()[0]
            envelope_count = connection.execute(
                "SELECT COUNT(*) FROM task_result_transport_envelopes"
            ).fetchone()[0]
            ack_count = connection.execute(
                "SELECT COUNT(*) FROM task_result_transport_acks"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(self.frozen.terminal_task_version, head[0])
        self.assertEqual(1, result_count)
        self.assertEqual(1, envelope_count)
        self.assertEqual(1, ack_count)


if __name__ == "__main__":
    unittest.main()
