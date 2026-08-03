from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from pullwise_worker import _generated_agent_task_contract as contract
from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
)
from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
)
from pullwise_worker.agent_kernel_current_debug import CurrentWorkerDebugStore
from pullwise_worker.agent_kernel_current_objects import CurrentObjectStore
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from pullwise_worker.agent_kernel_current_terminalization import (
    CurrentTerminalizationStore,
)
from pullwise_worker.agent_kernel_current_transport import (
    CurrentTaskResultTransportStore,
)
from tests.current_runtime_bootstrap_support import (
    bootstrap_bytes,
    golden_runtime_bootstrap,
)
from tests.current_s5_support import (
    blocked_task_result_bytes,
    terminalization_inputs,
)


class CurrentWorkerDebugStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s7-debug-")
        self.root = Path(self.scratch.name)
        self.database = CurrentAgentKernelDatabase.open(
            self.root / "current", CURRENT_PACKAGE
        )
        self.objects = CurrentObjectStore(self.database.root / "content")
        bootstrap = golden_runtime_bootstrap(
            outer_job_id="job_" + "1" * 32,
            run_id="run_" + "2" * 32,
        )
        self.authority = CurrentRuntimeBootstrapConsumer(
            self.database
        ).ingest(bootstrap_bytes(bootstrap))
        self.inputs = terminalization_inputs(
            self.database,
            self.authority,
            source_available=True,
        )
        self.terminalization = CurrentTerminalizationStore(self.database)
        self.prepared = self.terminalization.prepare(
            **{
                key: value
                for key, value in self.inputs.items()
                if key != "documents"
            }
        )
        self.result_bytes = blocked_task_result_bytes(
            self.authority,
            self.prepared,
            self.inputs["documents"],
        )
        self.capture_root = self.root / "capture"
        self.capture_root.mkdir()
        (self.capture_root / "debug-summary.json").write_text(
            json.dumps({"message": "ready", "token": "secret-value"}),
            encoding="utf-8",
        )
        (self.capture_root / "task-events.jsonl").write_text(
            json.dumps(
                {
                    "event": "started",
                    "authorization": "Bearer visible-secret-token",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.capture_root / "source.py").write_text(
            "repository source must never enter debug.zip\n",
            encoding="utf-8",
        )
        self.store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_terminal_fragment_closes_core_capture_and_transport(self) -> None:
        plan = self.inputs["documents"]["debug"]
        plan_bytes = contract.canonical_validated_bytes(
            "debug-redaction-plan/v1", plan
        )
        core = self.store.stage_terminal_core(self.result_bytes)

        connection = self.database.connect()
        try:
            stored_core = connection.execute(
                "SELECT content_schema_id,object_bytes FROM checkpoint_objects "
                "WHERE sha256=?",
                (core.sha256,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("task-result-core/v1", stored_core[0])
        self.assertEqual(core.canonical_bytes, bytes(stored_core[1]))

        captured = self.store.capture(
            task_id=self.authority.task_id,
            capture_kind="terminal",
            snapshot_seq=1,
            captured_at="2026-07-22T00:00:42.000Z",
            source_state_id=core.document["final_source_state"]["ref"]["sha256"],
            input_root=self.capture_root,
            redaction_plan_bytes=plan_bytes,
            local_event_seq=10,
            last_server_acked_event_seq=9,
            task_result_core=core,
        )
        replay = self.store.capture(
            task_id=self.authority.task_id,
            capture_kind="terminal",
            snapshot_seq=1,
            captured_at="2026-07-22T00:00:42.000Z",
            source_state_id=core.document["final_source_state"]["ref"]["sha256"],
            input_root=self.capture_root,
            redaction_plan_bytes=plan_bytes,
            local_event_seq=10,
            last_server_acked_event_seq=9,
            task_result_core=core,
        )
        self.assertEqual(captured, replay)

        fragment = contract.verify_worker_debug_fragment_content(
            captured.document,
            core.document,
            captured.file_manifest,
            captured.redaction_report,
        )
        self.assertEqual(
            "transport_attempt_" + "3" * 32,
            fragment["transport_attempt_id"],
        )
        self.assertEqual(self.authority.attempt_id, fragment["native_attempt_id"])
        self.assertNotEqual(
            fragment["transport_attempt_id"], fragment["native_attempt_id"]
        )
        self.assertEqual(
            hashlib.sha256(captured.canonical_bytes).hexdigest(),
            captured.sha256,
        )
        self.assertEqual("redacted", captured.redaction_report["status"])
        self.assertGreaterEqual(
            captured.redaction_report["structured_pass_detection_count"],
            2,
        )
        self.assertEqual(
            0,
            captured.redaction_report["archive_rescan_detection_count"],
        )

        archive = self.objects.read_verified(captured.archive_object)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            names = [item.filename for item in infos]
            self.assertEqual(sorted(names), names)
            self.assertEqual(
                [
                    "debug-summary.json",
                    "fragment-files.json",
                    "redaction-report.json",
                    "task-events.jsonl",
                ],
                names,
            )
            self.assertTrue(
                all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in infos)
            )
            self.assertTrue(
                all((item.external_attr >> 16) & 0o777 == 0o644 for item in infos)
            )
            summary = json.loads(bundle.read("debug-summary.json"))
            events = bundle.read("task-events.jsonl").decode("utf-8")
            self.assertEqual(["source.py"], summary["ignored_paths"])
            self.assertEqual("[REDACTED]", summary["token"])
            self.assertNotIn("visible-secret-token", events)
            self.assertNotIn("repository source", archive.decode("latin-1"))
            files = json.loads(bundle.read("fragment-files.json"))
            self.assertNotIn(
                "fragment-files.json",
                [item["path"] for item in files["entries"]],
            )

        descriptor = self.store.record_upload_failure(captured)
        checked_descriptor = contract.verify_worker_debug_descriptor_content(
            descriptor.document,
            captured.document,
        )
        self.assertEqual("local_only", checked_descriptor["state"])
        self.assertEqual(captured.sha256, checked_descriptor["source_sha256"])

        result_bytes = self.store.bind_task_result(
            self.result_bytes,
            core,
            descriptor,
        )
        result = contract.validate_document(
            "task-result/v1",
            json.loads(result_bytes),
        )
        self.assertEqual(
            descriptor.content_ref,
            result["diagnostics"]["worker_debug_fragment"]["ref"],
        )
        objects = deepcopy(self.inputs["objects"])
        objects[descriptor.sha256] = (
            "worker-debug-fragment-descriptor/v1",
            descriptor.canonical_bytes,
        )
        frozen = self.terminalization.freeze(
            self.prepared,
            result_bytes,
            objects,
        )
        transport = CurrentTaskResultTransportStore(self.database).prepare(
            frozen.task_id,
            worker_debug_descriptor_bytes=descriptor.canonical_bytes,
        )
        self.assertEqual(
            descriptor.sha256,
            transport.worker_debug_descriptor_sha256,
        )


if __name__ == "__main__":
    unittest.main()
