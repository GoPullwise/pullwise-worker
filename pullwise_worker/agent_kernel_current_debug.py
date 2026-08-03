"""Local-only current WorkerDebugFragment capture and immutable descriptors."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re
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
from .agent_kernel_current_debug_storage import (
    commit_capture,
    load_capture_context,
    record_descriptor,
)
from .agent_kernel_current_objects import (
    CurrentObjectError,
    CurrentObjectStore,
)
from .agent_kernel_current_terminalization_contract import put_object


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
            context = load_capture_context(
                self.database, task_id, capture_kind, task_result_core
            )
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
            return commit_capture(
                self.database,
                self.fault_hook,
                captured,
                context,
                task_result_core,
            )
        except CurrentWorkerDebugError:
            raise
        except CurrentObjectError as exc:
            self._fail("DEBUG_UNAVAILABLE", exc.code)
        except Exception as exc:
            self._fail("DEBUG_UNAVAILABLE", self._detail(exc))

    def record_upload_failure(
        self, captured: CapturedWorkerDebugFragment
    ) -> SealedWorkerDebugDescriptor:
        return record_descriptor(
            self.database, self.fault_hook, captured, None
        )

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
        return record_descriptor(
            self.database,
            self.fault_hook,
            captured,
            (receipt, server_receipt_bytes),
        )

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
