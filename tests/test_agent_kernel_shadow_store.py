from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_canonical import canonical_bytes
from pullwise_worker.agent_kernel_database import AgentKernelDatabase
from pullwise_worker.agent_kernel_object_store import ContentRefConflictError
from pullwise_worker.agent_kernel_schema_registry import SchemaValidationError
from pullwise_worker.agent_kernel_shadow_store import AgentKernelShadowStore


CONTRACT_ROOT = (
    Path(__file__).resolve().parents[1] / "contracts" / "agent-task" / "v1"
)


def _fixture(schema_id: str) -> dict[str, object]:
    for path in sorted((CONTRACT_ROOT / "fixtures").glob("schema-golden*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            if case["schema_id"] == schema_id:
                return copy.deepcopy(case["valid"])
    raise AssertionError(schema_id)


class AgentKernelShadowStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-shadow-")
        self.worker_root = Path(self.scratch.name) / "worker"
        self.store = AgentKernelShadowStore.open(
            self.worker_root, contract_root=CONTRACT_ROOT
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_validated_contract_round_trips_as_canonical_cas_bytes(self) -> None:
        policy = _fixture("effective-execution-policy/v1")
        task_id = "task_" + "1" * 32

        ref = self.store.put_contract(
            task_id=task_id,
            artifact_id="art_" + "2" * 32,
            schema_id="effective-execution-policy/v1",
            instance=policy,
        )

        self.assertEqual("effective-execution-policy/v1", ref["content_schema_id"])
        self.assertEqual(canonical_bytes(policy), self.store.objects.read_verified(ref))
        self.assertEqual(policy, self.store.read_contract(ref))
        metrics = self.store.metrics.snapshot()
        self.assertEqual(1, metrics["agent_kernel_shadow_contract_writes_total"])
        self.assertEqual(1, metrics["agent_kernel_shadow_contract_reads_total"])
        self.assertEqual(
            len(canonical_bytes(policy)),
            metrics["agent_kernel_shadow_contract_write_bytes_total"],
        )
        with self.store.database.connect() as connection:
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM result_publications").fetchone()[0]
            )
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    def test_invalid_contract_is_rejected_before_any_object_or_binding(self) -> None:
        policy = _fixture("effective-execution-policy/v1")
        policy["allowed_write_roots"] = ["repository"]

        with self.assertRaises(SchemaValidationError):
            self.store.put_contract(
                task_id="task_" + "3" * 32,
                artifact_id="art_" + "4" * 32,
                schema_id="effective-execution-policy/v1",
                instance=policy,
            )

        with self.store.database.connect() as connection:
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0]
            )
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM content_bindings").fetchone()[0]
            )
        self.assertEqual(
            1,
            self.store.metrics.snapshot()[
                "agent_kernel_shadow_contract_validation_failures_total"
            ],
        )

    def test_same_artifact_id_cannot_be_rebound_to_another_contract(self) -> None:
        task_id = "task_" + "5" * 32
        artifact_id = "art_" + "6" * 32
        request = _fixture("task-request/v1")
        self.store.put_contract(
            task_id=task_id,
            artifact_id=artifact_id,
            schema_id="task-request/v1",
            instance=request,
        )
        changed = copy.deepcopy(request)
        changed["objective"] = "A different immutable objective."

        with self.assertRaisesRegex(ContentRefConflictError, "CONTENT_REF_CONFLICT"):
            self.store.put_contract(
                task_id=task_id,
                artifact_id=artifact_id,
                schema_id="task-request/v1",
                instance=changed,
            )
        self.assertEqual(
            1,
            self.store.metrics.snapshot()["agent_kernel_shadow_cas_conflicts_total"],
        )

    def test_read_revalidates_bytes_and_declared_schema(self) -> None:
        request = _fixture("task-request/v1")
        ref = self.store.put_contract(
            task_id="task_" + "7" * 32,
            artifact_id="art_" + "8" * 32,
            schema_id="task-request/v1",
            instance=request,
        )
        wrong = dict(ref)
        wrong["content_schema_id"] = "task-record/v1"

        with self.assertRaises(SchemaValidationError):
            self.store.read_contract(wrong)

    def test_read_rejects_strict_json_failure_as_contract_validation(self) -> None:
        ref = self.store.objects.put_bytes(
            b'{"schema_id":"actor/v1","kind":"worker","kind":"owner"}',
            task_id="task_" + "d" * 32,
            artifact_id="art_" + "e" * 32,
            media_type="application/json",
            content_schema_id="actor/v1",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SchemaValidationError, "duplicate_object_key"):
            self.store.read_contract(ref)
        self.assertEqual(
            1,
            self.store.metrics.snapshot()[
                "agent_kernel_shadow_contract_validation_failures_total"
            ],
        )

    def test_open_is_idempotent_and_uses_instance_scoped_directory(self) -> None:
        reopened = AgentKernelShadowStore.open(
            self.worker_root, contract_root=CONTRACT_ROOT
        )
        self.assertEqual(
            self.worker_root / "agent-kernel" / "state.sqlite3",
            reopened.database.path,
        )
        self.assertIsInstance(reopened.database, AgentKernelDatabase)


if __name__ == "__main__":
    unittest.main()
