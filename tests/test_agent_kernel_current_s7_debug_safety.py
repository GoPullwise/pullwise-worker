from __future__ import annotations

from copy import deepcopy
import json
import os
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
from pullwise_worker.agent_kernel_current_debug import (
    CurrentWorkerDebugError,
    CurrentWorkerDebugStore,
    DebugCaptureLimits,
)
from pullwise_worker.agent_kernel_current_objects import CurrentObjectStore
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from tests.current_runtime_bootstrap_support import (
    bootstrap_bytes,
    golden_runtime_bootstrap,
)


class CurrentWorkerDebugSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s7-safety-")
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
        plan = deepcopy(
            contract.fixture("gate_preparation_golden_debug_plan")["document"]
        )
        plan.pop("plan_digest")
        plan["task_id"] = self.authority.task_id
        plan["debug_input_refs"] = []
        self.plan = contract.seal_document("debug-redaction-plan/v1", plan)
        self.plan_bytes = contract.canonical_validated_bytes(
            "debug-redaction-plan/v1", self.plan
        )
        self.capture_root = self.root / "capture"
        self.capture_root.mkdir()
        (self.capture_root / "debug-summary.json").write_text(
            json.dumps({"state": "ready"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def capture(self, store, *, kind: str, seq: int):
        return store.capture(
            task_id=self.authority.task_id,
            capture_kind=kind,
            snapshot_seq=seq,
            captured_at=f"2026-07-22T00:00:0{seq}.000Z",
            source_state_id="a" * 64,
            input_root=self.capture_root,
            redaction_plan_bytes=self.plan_bytes,
            local_event_seq=seq,
            last_server_acked_event_seq=seq - 1,
        )

    def test_nonterminal_variants_are_exact_and_archive_deterministic(self) -> None:
        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        )
        captures = [
            self.capture(store, kind=kind, seq=index)
            for index, kind in enumerate(
                ("startup", "checkpoint", "crash"),
                start=1,
            )
        ]
        archives = [
            self.objects.read_verified(item.archive_object) for item in captures
        ]
        self.assertEqual(archives[0], archives[1])
        self.assertEqual(archives[1], archives[2])
        self.assertEqual(3, len({item.sha256 for item in captures}))
        for expected_kind, item in zip(
            ("startup", "checkpoint", "crash"), captures
        ):
            checked = contract.verify_worker_debug_fragment_content(
                item.document,
                None,
                item.file_manifest,
                item.redaction_report,
            )
            self.assertEqual(expected_kind, checked["capture_kind"])
            self.assertEqual(
                {
                    "availability": "not_applicable",
                    "reason_code": "TASK_RESULT_CORE_NOT_APPLICABLE",
                },
                checked["task_result_core"],
            )

    def test_snapshot_replay_is_exact_and_changed_bytes_conflict(self) -> None:
        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        )
        first = self.capture(store, kind="startup", seq=1)
        self.assertEqual(first, self.capture(store, kind="startup", seq=1))

        (self.capture_root / "debug-summary.json").write_text(
            json.dumps({"state": "changed"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            CurrentWorkerDebugError,
            "IDEMPOTENCY_CONFLICT",
        ):
            self.capture(store, kind="startup", seq=1)

        connection = self.database.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM worker_debug_fragments"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, count)

    def test_limits_omit_whole_entries_without_mid_line_truncation(self) -> None:
        original_line = json.dumps(
            {"event": "x" * 256},
            separators=(",", ":"),
        )
        (self.capture_root / "task-events.jsonl").write_text(
            original_line + "\n",
            encoding="utf-8",
        )
        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
            limits=DebugCaptureLimits(
                max_file_bytes=64,
                max_total_bytes=4096,
                max_archive_bytes=4096,
                max_entries=15,
            ),
        )
        captured = self.capture(store, kind="checkpoint", seq=1)
        self.assertEqual("partial", captured.document["status"])
        self.assertEqual(
            "DEBUG_LIMIT_EXCEEDED",
            captured.document["reason_code"],
        )
        self.assertEqual(
            ["debug-summary.json", "redaction-report.json"],
            [item["path"] for item in captured.file_manifest["entries"]],
        )
        archive = self.objects.read_verified(captured.archive_object)
        self.assertNotIn(original_line.encode("utf-8"), archive)


if __name__ == "__main__":
    unittest.main()
