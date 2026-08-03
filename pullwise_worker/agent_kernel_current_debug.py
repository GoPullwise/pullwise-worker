"""Local-only current WorkerDebugFragment capture and immutable descriptors."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Callable

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_debug_capture import prepare_debug_content
from .agent_kernel_current_debug_contract import (
    CapturedWorkerDebugFragment,
    CurrentWorkerDebugError,
    DebugCaptureLimits,
    SealedWorkerDebugDescriptor,
    StagedTaskResultCore,
    canonical_bytes,
    content_ref,
    object_sha256,
    parse_exact,
)
from .agent_kernel_current_objects import (
    CurrentObjectError,
    CurrentObjectStore,
    PublishedCurrentObject,
)
from .agent_kernel_current_terminalization_contract import put_object


_JOB = re.compile(r"^job_[0-9a-f]{32}$")
_RUN = re.compile(r"^run_[0-9a-f]{32}$")
_LEASE = re.compile(r"^lease_[0-9a-f]{32}$")
_TRANSPORT_ATTEMPT = re.compile(r"^transport_attempt_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CAPTURE_KINDS = {"startup", "checkpoint", "terminal", "crash"}


class CurrentWorkerDebugStore:
    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        object_store: CurrentObjectStore | None = None,
        limits: DebugCaptureLimits | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentWorkerDebugError("CURRENT_DATABASE_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
        self.database = database
        self.object_store = object_store or CurrentObjectStore(
            database.root / "content"
        )
        self.limits = limits or DebugCaptureLimits()
        self.fault_hook = fault_hook or (lambda _stage: None)

    def stage_terminal_core(self, task_result_bytes: bytes) -> StagedTaskResultCore:
        result = parse_exact("task-result/v1", task_result_bytes, "TASK_RESULT_INVALID")
        try:
            core = contract.derive_task_result_core(result)
            raw = canonical_bytes("task-result-core/v1", core)
            digest = object_sha256(raw)
            reference = content_ref("task-result-core/v1", raw)
            with self.database.transaction() as connection:
                put_object(connection, "task-result-core/v1", raw)
                self.fault_hook("after_terminal_core")
                self.fault_hook("before_terminal_core_commit")
            return StagedTaskResultCore(core, raw, digest, reference)
        except CurrentWorkerDebugError:
            raise
        except Exception as exc:
            self._fail("TASK_RESULT_INVALID", self._detail(exc))

    def capture(
        self,
        *,
        task_id: str,
        capture_kind: str,
        snapshot_seq: int,
        captured_at: str,
        source_state_id: str,
        input_root: Path,
        redaction_plan_bytes: bytes,
        local_event_seq: int,
        last_server_acked_event_seq: int,
        task_result_core: StagedTaskResultCore | None = None,
    ) -> CapturedWorkerDebugFragment:
        self._validate_capture_args(
            task_id,
            capture_kind,
            snapshot_seq,
            captured_at,
            source_state_id,
            local_event_seq,
            last_server_acked_event_seq,
            task_result_core,
        )
        plan = parse_exact(
            "debug-redaction-plan/v1",
            redaction_plan_bytes,
            "DEBUG_REDACTION_FAILED",
        )
        if (
            plan["task_id"] != task_id
            or set(plan["rule_ids"]) != {"secret.deny", "text.redact"}
        ):
            self._fail("DEBUG_REDACTION_FAILED")
        try:
            context = self._capture_context(task_id, capture_kind, task_result_core)
            content = prepare_debug_content(
                input_root=input_root,
                redaction_plan=plan,
                limits=self.limits,
                staging_root=self.object_store.staging,
            )
            manifest_ref = content_ref(
                "worker-debug-file-manifest/v1", content.file_manifest_bytes
            )
            report_ref = content_ref(
                "worker-debug-redaction-report/v1",
                content.redaction_report_bytes,
            )
            core_availability = (
                {
                    "availability": "available",
                    "ref": task_result_core.content_ref,
                }
                if task_result_core is not None
                else {
                    "availability": "not_applicable",
                    "reason_code": "TASK_RESULT_CORE_NOT_APPLICABLE",
                }
            )
            identity = {
                "task_id": task_id,
                "job_id": context["job_id"],
                "run_id": context["run_id"],
                "lease_id": context["lease_id"],
                "transport_attempt_id": context["transport_attempt_id"],
                "transport_epoch": context["transport_epoch"],
                "native_attempt_id": context["native_attempt_id"],
                "native_epoch": context["native_epoch"],
                "capture_kind": capture_kind,
                "snapshot_seq": snapshot_seq,
                "file_manifest_digest": content.file_manifest["manifest_digest"],
            }
            fragment_id = "frag_" + hashlib.sha256(
                b"pullwise:worker-debug-fragment-id/v1\0"
                + contract.canonical_document_bytes(identity)
            ).hexdigest()
            fragment = {
                "schema_id": "worker-debug-fragment/v1",
                "fragment_id": fragment_id,
                **{key: identity[key] for key in (
                    "task_id", "job_id", "run_id", "lease_id",
                    "transport_attempt_id", "transport_epoch",
                    "native_attempt_id", "native_epoch",
                    "capture_kind", "snapshot_seq",
                )},
                "protocol_mode": "agent_task_v1",
                "captured_at": captured_at,
                "sealed": True,
                "task_version": context["task_version"],
                "checkpoint_generation": context["checkpoint_generation"],
                "local_event_seq": local_event_seq,
                "last_server_acked_event_seq": last_server_acked_event_seq,
                "task_result_core": core_availability,
                "source_state_id": source_state_id,
                "file_manifest_ref": manifest_ref,
                "redaction_report_ref": report_ref,
                "status": content.status,
                "reason_code": content.reason_code,
            }
            checked = contract.verify_worker_debug_fragment_content(
                fragment,
                task_result_core.document if task_result_core is not None else None,
                content.file_manifest,
                content.redaction_report,
            )
            raw = canonical_bytes("worker-debug-fragment/v1", checked)
            archive = self.object_store.publish(content.archive_bytes)
            captured = CapturedWorkerDebugFragment(
                checked,
                raw,
                object_sha256(raw),
                content_ref("worker-debug-fragment/v1", raw),
                content.file_manifest,
                content.file_manifest_bytes,
                content.redaction_report,
                content.redaction_report_bytes,
                archive,
            )
            return self._commit_capture(captured, context, task_result_core)
        except CurrentWorkerDebugError:
            raise
        except CurrentObjectError as exc:
            self._fail("DEBUG_UNAVAILABLE", exc.code)
        except Exception as exc:
            self._fail("DEBUG_UNAVAILABLE", self._detail(exc))

    def record_upload_failure(
        self, captured: CapturedWorkerDebugFragment
    ) -> SealedWorkerDebugDescriptor:
        return self._record_descriptor(captured, None)

    def record_uploaded(
        self,
        captured: CapturedWorkerDebugFragment,
        server_receipt_bytes: bytes,
    ) -> SealedWorkerDebugDescriptor:
        receipt = parse_exact(
            "server-transport-receipt/v1",
            server_receipt_bytes,
            "DEBUG_RECEIPT_CONFLICT",
        )
        return self._record_descriptor(captured, (receipt, server_receipt_bytes))

    def bind_task_result(
        self,
        task_result_bytes: bytes,
        core: StagedTaskResultCore,
        descriptor: SealedWorkerDebugDescriptor,
    ) -> bytes:
        result = parse_exact("task-result/v1", task_result_bytes, "TASK_RESULT_INVALID")
        if (
            not isinstance(core, StagedTaskResultCore)
            or not isinstance(descriptor, SealedWorkerDebugDescriptor)
        ):
            self._fail("TASK_RESULT_INVALID")
        try:
            if contract.derive_task_result_core(result) != core.document:
                self._fail("TASK_RESULT_INVALID")
            result = deepcopy(result)
            result["diagnostics"]["worker_debug_fragment"] = {
                "availability": "available",
                "ref": descriptor.content_ref,
            }
            checked = contract.validate_document("task-result/v1", result)
            contract.verify_task_result_core(checked, core.document)
            return canonical_bytes("task-result/v1", checked)
        except CurrentWorkerDebugError:
            raise
        except Exception as exc:
            self._fail("TASK_RESULT_INVALID", self._detail(exc))

    def _capture_context(
        self,
        task_id: str,
        capture_kind: str,
        core: StagedTaskResultCore | None,
    ) -> dict[str, object]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT bootstrap_bytes FROM runtime_bootstraps WHERE task_id=?",
                (task_id,),
            ).fetchone()
            task_row = connection.execute(
                "SELECT r.record_bytes FROM runtime_task_heads h "
                "JOIN runtime_task_records r "
                "USING(task_id,task_version,record_sha256) WHERE h.task_id=?",
                (task_id,),
            ).fetchone()
            if row is None or task_row is None:
                self._fail("DEBUG_UNAVAILABLE")
            bootstrap = parse_exact(
                "agent-task-runtime-bootstrap/v1",
                bytes(row["bootstrap_bytes"]),
                "DEBUG_UNAVAILABLE",
            )
            task = parse_exact(
                "task-record/v1",
                bytes(task_row["record_bytes"]),
                "DEBUG_UNAVAILABLE",
            )
            binding = bootstrap["transport_binding"]
            attempt = bootstrap["construction_roots"]["attempt"]
            exact = (
                bootstrap["authority"]["task_id"] == task_id == task["task_id"]
                and binding["outer_job_id"] == task["outer_job_id"]
                and binding["run_id"] == task["run_id"]
                and binding["lease_id"] == task["lease_id"]
                and binding["transport_attempt_id"] == task["transport_attempt_id"]
                and binding["transport_epoch"] == task["transport_epoch"]
                and attempt["attempt_id"] == task["current_attempt_id"]
                and attempt["native_epoch"] == task["native_epoch"]
            )
            if (
                not exact
                or _JOB.fullmatch(binding["outer_job_id"]) is None
                or _RUN.fullmatch(binding["run_id"]) is None
                or _LEASE.fullmatch(binding["lease_id"]) is None
                or _TRANSPORT_ATTEMPT.fullmatch(binding["transport_attempt_id"])
                is None
            ):
                self._fail("TRANSPORT_IDENTITY_MISMATCH")
            if capture_kind == "terminal":
                assert core is not None
                stored = connection.execute(
                    "SELECT content_schema_id,size_bytes,object_bytes "
                    "FROM checkpoint_objects WHERE sha256=?",
                    (core.sha256,),
                ).fetchone()
                if stored is None or (
                    stored["content_schema_id"],
                    stored["size_bytes"],
                    bytes(stored["object_bytes"]),
                ) != (
                    "task-result-core/v1",
                    len(core.canonical_bytes),
                    core.canonical_bytes,
                ):
                    self._fail("DEBUG_TERMINAL_CORE_REQUIRED")
                task_version = core.document["published_from_version"]
                generation = core.document["provenance"]["checkpoint_generation"]
            else:
                task_version = task["task_version"]
                generation = task["current_checkpoint_generation"]
            return {
                "job_id": binding["outer_job_id"],
                "run_id": binding["run_id"],
                "lease_id": binding["lease_id"],
                "transport_attempt_id": binding["transport_attempt_id"],
                "transport_epoch": binding["transport_epoch"],
                "native_attempt_id": attempt["attempt_id"],
                "native_epoch": attempt["native_epoch"],
                "task_version": task_version,
                "checkpoint_generation": generation,
            }
        finally:
            connection.close()

    def _commit_capture(self, captured, context, core):
        archive = captured.archive_object
        values = (
            captured.sha256,
            captured.document["task_id"],
            context["job_id"],
            context["run_id"],
            context["lease_id"],
            context["transport_attempt_id"],
            context["transport_epoch"],
            context["native_attempt_id"],
            context["native_epoch"],
            captured.document["capture_kind"],
            captured.document["snapshot_seq"],
            archive.sha256,
            object_sha256(captured.file_manifest_bytes),
            object_sha256(captured.redaction_report_bytes),
            core.sha256 if core is not None else None,
            captured.document["captured_at"],
        )
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM worker_debug_fragments WHERE "
                    "task_id=? AND job_id=? AND run_id=? AND lease_id=? AND "
                    "transport_attempt_id=? AND transport_epoch=? AND "
                    "native_attempt_id=? AND native_epoch=? AND snapshot_seq=?",
                    values[1:9] + (values[10],),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != values:
                        self._fail("IDEMPOTENCY_CONFLICT")
                    return captured
                for schema_id, raw in (
                    ("worker-debug-file-manifest/v1", captured.file_manifest_bytes),
                    (
                        "worker-debug-redaction-report/v1",
                        captured.redaction_report_bytes,
                    ),
                    ("worker-debug-fragment/v1", captured.canonical_bytes),
                ):
                    put_object(connection, schema_id, raw)
                self.fault_hook("after_debug_objects")
                connection.execute(
                    "INSERT OR IGNORE INTO content_objects VALUES (?,?,?)",
                    (archive.sha256, archive.size_bytes, archive.relative_path),
                )
                stored = connection.execute(
                    "SELECT size_bytes,relative_path FROM content_objects "
                    "WHERE sha256=?",
                    (archive.sha256,),
                ).fetchone()
                if stored is None or tuple(stored) != (
                    archive.size_bytes,
                    archive.relative_path,
                ):
                    self._fail("DEBUG_UNAVAILABLE")
                self.fault_hook("after_debug_archive")
                connection.execute(
                    "INSERT INTO worker_debug_fragments VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                self.fault_hook("after_debug_fragment")
                self.fault_hook("before_debug_commit")
                return captured
        except CurrentWorkerDebugError:
            raise
        except sqlite3.IntegrityError as exc:
            self._fail("IDEMPOTENCY_CONFLICT", type(exc).__name__)
        except sqlite3.Error as exc:
            self._fail("DEBUG_UNAVAILABLE", type(exc).__name__)

    def _record_descriptor(self, captured, receipt):
        if not isinstance(captured, CapturedWorkerDebugFragment):
            self._fail("DEBUG_UNAVAILABLE")
        receipt_document = receipt[0] if receipt is not None else None
        receipt_ref = (
            content_ref("server-transport-receipt/v1", receipt[1])
            if receipt is not None
            else None
        )
        value = {
            "schema_id": "worker-debug-fragment-descriptor/v1",
            "state": "uploaded" if receipt is not None else "local_only",
            "fragment_ref": captured.content_ref,
            "sealed": True,
            "snapshot_seq": captured.document["snapshot_seq"],
            "source_sha256": captured.sha256,
            "transport_kind": "server_transport" if receipt is not None else "none",
            "server_fragment_ref": (
                receipt_document["content_ref"] if receipt is not None else None
            ),
            "server_receipt_ref": receipt_ref,
            "reason_code": None if receipt is not None else "DEBUG_UPLOAD_FAILED",
        }
        try:
            checked = contract.verify_worker_debug_descriptor_content(
                value,
                captured.document,
                transport_receipt=receipt_document,
            )
            raw = canonical_bytes("worker-debug-fragment-descriptor/v1", checked)
            sealed = SealedWorkerDebugDescriptor(
                checked,
                raw,
                object_sha256(raw),
                content_ref("worker-debug-fragment-descriptor/v1", raw),
            )
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT descriptor_sha256,state,server_receipt_sha256 "
                    "FROM worker_debug_descriptors WHERE fragment_sha256=?",
                    (captured.sha256,),
                ).fetchone()
                expected = (
                    sealed.sha256,
                    checked["state"],
                    object_sha256(receipt[1]) if receipt is not None else None,
                )
                if existing is not None:
                    if tuple(existing) != expected:
                        self._fail("DEBUG_RECEIPT_CONFLICT")
                    return sealed
                row = connection.execute(
                    "SELECT fragment_sha256 FROM worker_debug_fragments "
                    "WHERE fragment_sha256=?",
                    (captured.sha256,),
                ).fetchone()
                if row is None:
                    self._fail("DEBUG_UNAVAILABLE")
                if receipt is not None:
                    put_object(
                        connection, "server-transport-receipt/v1", receipt[1]
                    )
                put_object(
                    connection,
                    "worker-debug-fragment-descriptor/v1",
                    sealed.canonical_bytes,
                )
                self.fault_hook("after_debug_descriptor_objects")
                connection.execute(
                    "INSERT INTO worker_debug_descriptors VALUES (?,?,?,?)",
                    (sealed.sha256, captured.sha256, *expected[1:]),
                )
                self.fault_hook("after_debug_descriptor")
                self.fault_hook("before_debug_descriptor_commit")
                return sealed
        except CurrentWorkerDebugError:
            raise
        except sqlite3.IntegrityError as exc:
            self._fail("DEBUG_RECEIPT_CONFLICT", type(exc).__name__)
        except sqlite3.Error as exc:
            self._fail("DEBUG_UNAVAILABLE", type(exc).__name__)
        except Exception as exc:
            self._fail("DEBUG_RECEIPT_CONFLICT", self._detail(exc))

    @staticmethod
    def _validate_capture_args(
        task_id,
        capture_kind,
        snapshot_seq,
        captured_at,
        source_state_id,
        local_event_seq,
        last_server_acked_event_seq,
        core,
    ):
        integers = (snapshot_seq, local_event_seq, last_server_acked_event_seq)
        invalid = (
            not isinstance(task_id, str)
            or capture_kind not in _CAPTURE_KINDS
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in integers
            )
            or snapshot_seq < 1
            or last_server_acked_event_seq > local_event_seq
            or not isinstance(captured_at, str)
            or _DIGEST.fullmatch(source_state_id or "") is None
            or (capture_kind == "terminal") != isinstance(core, StagedTaskResultCore)
        )
        if invalid:
            raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")

    @staticmethod
    def _detail(exc: BaseException) -> str:
        return str(getattr(exc, "code", type(exc).__name__))

    @staticmethod
    def _fail(code: str, detail: str = "") -> None:
        raise CurrentWorkerDebugError(code, detail)


__all__ = [
    "CapturedWorkerDebugFragment",
    "CurrentWorkerDebugError",
    "CurrentWorkerDebugStore",
    "DebugCaptureLimits",
    "SealedWorkerDebugDescriptor",
    "StagedTaskResultCore",
]
