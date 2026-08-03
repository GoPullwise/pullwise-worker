"""Authority loading and append-only persistence for current debug capture."""

from __future__ import annotations

import re
import sqlite3

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_debug_contract import (
    CapturedWorkerDebugFragment,
    CurrentWorkerDebugError,
    SealedWorkerDebugDescriptor,
    StagedTaskResultCore,
    canonical_bytes,
    content_ref,
    object_sha256,
    parse_exact,
)
from .agent_kernel_current_terminalization_contract import put_object


_JOB = re.compile(r"^job_[0-9a-f]{32}$")
_RUN = re.compile(r"^run_[0-9a-f]{32}$")
_LEASE = re.compile(r"^lease_[0-9a-f]{32}$")
_TRANSPORT_ATTEMPT = re.compile(r"^transport_attempt_[0-9a-f]{32}$")


def load_capture_context(database, task_id, capture_kind, core):
    connection = database.connect()
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
            raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
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
            or _TRANSPORT_ATTEMPT.fullmatch(binding["transport_attempt_id"]) is None
        ):
            raise CurrentWorkerDebugError("TRANSPORT_IDENTITY_MISMATCH")
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
                raise CurrentWorkerDebugError("DEBUG_TERMINAL_CORE_REQUIRED")
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


def commit_capture(database, fault_hook, captured, context, core):
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
        with database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM worker_debug_fragments WHERE "
                "task_id=? AND job_id=? AND run_id=? AND lease_id=? AND "
                "transport_attempt_id=? AND transport_epoch=? AND "
                "native_attempt_id=? AND native_epoch=? AND snapshot_seq=?",
                values[1:9] + (values[10],),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise CurrentWorkerDebugError("IDEMPOTENCY_CONFLICT")
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
            fault_hook("after_debug_objects")
            connection.execute(
                "INSERT OR IGNORE INTO content_objects VALUES (?,?,?)",
                (archive.sha256, archive.size_bytes, archive.relative_path),
            )
            stored = connection.execute(
                "SELECT size_bytes,relative_path FROM content_objects WHERE sha256=?",
                (archive.sha256,),
            ).fetchone()
            if stored is None or tuple(stored) != (
                archive.size_bytes,
                archive.relative_path,
            ):
                raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
            fault_hook("after_debug_archive")
            connection.execute(
                "INSERT INTO worker_debug_fragments VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            fault_hook("after_debug_fragment")
            fault_hook("before_debug_commit")
            return captured
    except CurrentWorkerDebugError:
        raise
    except sqlite3.IntegrityError as exc:
        raise CurrentWorkerDebugError(
            "IDEMPOTENCY_CONFLICT", type(exc).__name__
        ) from exc
    except sqlite3.Error as exc:
        raise CurrentWorkerDebugError(
            "DEBUG_UNAVAILABLE", type(exc).__name__
        ) from exc


def record_descriptor(database, fault_hook, captured, receipt):
    if not isinstance(captured, CapturedWorkerDebugFragment):
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
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
        with database.transaction() as connection:
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
                    raise CurrentWorkerDebugError("DEBUG_RECEIPT_CONFLICT")
                return sealed
            row = connection.execute(
                "SELECT fragment_sha256 FROM worker_debug_fragments "
                "WHERE fragment_sha256=?",
                (captured.sha256,),
            ).fetchone()
            if row is None:
                raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
            if receipt is not None:
                put_object(connection, "server-transport-receipt/v1", receipt[1])
            put_object(
                connection,
                "worker-debug-fragment-descriptor/v1",
                sealed.canonical_bytes,
            )
            fault_hook("after_debug_descriptor_objects")
            connection.execute(
                "INSERT INTO worker_debug_descriptors VALUES (?,?,?,?)",
                (sealed.sha256, captured.sha256, *expected[1:]),
            )
            fault_hook("after_debug_descriptor")
            fault_hook("before_debug_descriptor_commit")
            return sealed
    except CurrentWorkerDebugError:
        raise
    except sqlite3.IntegrityError as exc:
        raise CurrentWorkerDebugError(
            "DEBUG_RECEIPT_CONFLICT", type(exc).__name__
        ) from exc
    except sqlite3.Error as exc:
        raise CurrentWorkerDebugError(
            "DEBUG_UNAVAILABLE", type(exc).__name__
        ) from exc
    except Exception as exc:
        detail = str(getattr(exc, "code", type(exc).__name__))
        raise CurrentWorkerDebugError("DEBUG_RECEIPT_CONFLICT", detail) from exc


__all__ = ["commit_capture", "load_capture_context", "record_descriptor"]
