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

    def test_unsafe_allowlist_entries_fail_closed(self) -> None:
        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        )

        hardlink_source = self.root / "hardlink-source.jsonl"
        hardlink_source.write_text("{}\n", encoding="utf-8")
        hardlink = self.capture_root / "worker.log.jsonl"
        os.link(hardlink_source, hardlink)
        with self.assertRaisesRegex(CurrentWorkerDebugError, "DEBUG_UNAVAILABLE"):
            self.capture(store, kind="startup", seq=1)
        hardlink.unlink()
        hardlink_source.unlink()

        wrong_case = self.capture_root / "QA.JSON"
        wrong_case.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(CurrentWorkerDebugError, "DEBUG_UNAVAILABLE"):
            self.capture(store, kind="startup", seq=2)
        wrong_case.unlink()

        special = self.capture_root / "qa.json"
        special.mkdir()
        with self.assertRaisesRegex(CurrentWorkerDebugError, "DEBUG_UNAVAILABLE"):
            self.capture(store, kind="startup", seq=3)
        special.rmdir()

        connection = self.database.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM worker_debug_fragments"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, count)

    def test_allowlisted_symlink_fails_closed(self) -> None:
        target = self.root / "symlink-target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.capture_root / "qa.json"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {type(exc).__name__}")
        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        )
        with self.assertRaisesRegex(CurrentWorkerDebugError, "DEBUG_UNAVAILABLE"):
            self.capture(store, kind="startup", seq=1)

    def test_capture_and_descriptor_crash_points_are_all_or_nothing(self) -> None:
        selected = {"stage": None}

        def fault(stage: str) -> None:
            if stage == selected["stage"]:
                raise RuntimeError("simulated crash")

        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
            fault_hook=fault,
        )
        for stage in (
            "after_debug_objects",
            "after_debug_archive",
            "after_debug_fragment",
            "before_debug_commit",
        ):
            selected["stage"] = stage
            with self.subTest(stage=stage), self.assertRaisesRegex(
                CurrentWorkerDebugError,
                "DEBUG_UNAVAILABLE",
            ):
                self.capture(store, kind="startup", seq=1)
            self._assert_debug_counts(fragments=0, descriptors=0)

        selected["stage"] = None
        captured = self.capture(store, kind="startup", seq=1)
        self._assert_debug_counts(fragments=1, descriptors=0)

        for stage in (
            "after_debug_descriptor_objects",
            "after_debug_descriptor",
            "before_debug_descriptor_commit",
        ):
            selected["stage"] = stage
            with self.subTest(stage=stage), self.assertRaisesRegex(
                CurrentWorkerDebugError,
                "DEBUG_RECEIPT_CONFLICT",
            ):
                store.record_upload_failure(captured)
            self._assert_debug_counts(fragments=1, descriptors=0)

        selected["stage"] = None
        descriptor = store.record_upload_failure(captured)
        self.assertEqual("local_only", descriptor.document["state"])
        self._assert_debug_counts(fragments=1, descriptors=1)

    def test_uploaded_descriptor_binds_exact_immutable_server_receipt(self) -> None:
        store = CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        )
        captured = self.capture(store, kind="startup", seq=1)
        receipt = deepcopy(
            contract.fixture("receipt_golden_immutable_transport")["document"]
        )
        receipt.pop("receipt_digest")
        receipt.update(
            {
                "package": CURRENT_PACKAGE.as_document(),
                "task_id": self.authority.task_id,
                "attempt_id": self.authority.attempt_id,
                "session_id": self.authority.session_id,
                "owner_id": self.authority.owner_id,
                "lease_id": self.authority.lease_id,
                "authority_digest": self.authority.authority_digest,
                "grant_digest": self.authority.grant_digest,
                "task_version": self.authority.task_version,
                "deletion_version": self.authority.deletion_version,
                "owner_epoch": self.authority.owner_epoch,
                "native_epoch": self.authority.native_epoch,
                "transport_epoch": self.authority.transport_epoch,
                "content_ref": captured.content_ref,
                "accepted_at": "2026-07-22T00:00:10.000Z",
            }
        )
        receipt = contract.seal_document("server-transport-receipt/v1", receipt)
        receipt_bytes = contract.canonical_validated_bytes(
            "server-transport-receipt/v1", receipt
        )
        descriptor = store.record_uploaded(captured, receipt_bytes)
        self.assertEqual(
            descriptor,
            store.record_uploaded(captured, receipt_bytes),
        )
        checked = contract.verify_worker_debug_descriptor_content(
            descriptor.document,
            captured.document,
            transport_receipt=receipt,
        )
        self.assertEqual("uploaded", checked["state"])
        with self.assertRaisesRegex(
            CurrentWorkerDebugError,
            "DEBUG_RECEIPT_CONFLICT",
        ):
            store.record_upload_failure(captured)

    def test_noncanonical_outer_job_and_run_ids_are_not_fabricated(self) -> None:
        database = CurrentAgentKernelDatabase.open(
            self.root / "invalid-outer", CURRENT_PACKAGE
        )
        authority = CurrentRuntimeBootstrapConsumer(database).ingest(
            bootstrap_bytes(golden_runtime_bootstrap())
        )
        store = CurrentWorkerDebugStore(database)
        with self.assertRaisesRegex(
            CurrentWorkerDebugError,
            "TRANSPORT_IDENTITY_MISMATCH",
        ):
            store.capture(
                task_id=authority.task_id,
                capture_kind="startup",
                snapshot_seq=1,
                captured_at="2026-07-22T00:00:01.000Z",
                source_state_id="a" * 64,
                input_root=self.capture_root,
                redaction_plan_bytes=self.plan_bytes,
                local_event_seq=1,
                last_server_acked_event_seq=0,
            )

    def _assert_debug_counts(self, *, fragments: int, descriptors: int) -> None:
        connection = self.database.connect()
        try:
            actual = (
                connection.execute(
                    "SELECT COUNT(*) FROM worker_debug_fragments"
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM worker_debug_descriptors"
                ).fetchone()[0],
            )
        finally:
            connection.close()
        self.assertEqual((fragments, descriptors), actual)


if __name__ == "__main__":
    unittest.main()
