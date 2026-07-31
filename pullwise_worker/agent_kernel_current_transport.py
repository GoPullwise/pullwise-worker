"""Immutable local projection of current TaskResult transport and ACKs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Callable

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import (
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_terminalization_contract import put_object


class CurrentTaskResultTransportError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PreparedTaskResultTransport:
    task_id: str
    result_digest: str
    task_result_core_sha256: str
    task_version_authority_sha256: str
    worker_debug_descriptor_sha256: str | None
    transport_receipt_sha256: str | None
    transport_envelope_digest: str
    canonical_bytes: bytes
    document: dict[str, object]
    task_result_core: dict[str, object]


@dataclass(frozen=True)
class AcceptedTaskResultTransport:
    task_id: str
    result_digest: str
    transport_envelope_digest: str
    ack_sha256: str
    ack_digest: str
    accepted_at: str
    canonical_bytes: bytes
    document: dict[str, object]


class CurrentTaskResultTransportStore:
    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentTaskResultTransportError("CURRENT_DATABASE_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise CurrentTaskResultTransportError("TRANSPORT_HOOK_INVALID")
        self.database = database
        self.authority = CurrentAuthorityProjection(database)
        self.fault_hook = fault_hook or (lambda _stage: None)

    def prepare(
        self,
        task_id: str,
        *,
        worker_debug_descriptor_bytes: bytes | None = None,
        transport_receipt_bytes: bytes | None = None,
    ) -> PreparedTaskResultTransport:
        if not isinstance(task_id, str) or not task_id:
            self._fail("TRANSPORT_TASK_ID_INVALID")
        try:
            descriptor = self._optional_document(
                "worker-debug-fragment-descriptor/v1",
                worker_debug_descriptor_bytes,
                "TRANSPORT_DEBUG_DESCRIPTOR_INVALID",
            )
            receipt = self._optional_document(
                "server-transport-receipt/v1",
                transport_receipt_bytes,
                "TRANSPORT_RECEIPT_INVALID",
            )
            with self.database.transaction() as connection:
                prepared = self._build(
                    connection, task_id, descriptor, receipt
                )
                existing = connection.execute(
                    "SELECT * FROM task_result_transport_envelopes "
                    "WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                values = self._envelope_values(prepared)
                if existing is not None:
                    if tuple(existing) != values:
                        self._fail("TRANSPORT_ENVELOPE_CONFLICT")
                    stored = self._load_object(
                        connection,
                        prepared.transport_envelope_digest,
                        "task-result-transport-envelope/v1",
                    )
                    if stored != prepared.canonical_bytes:
                        self._fail("TRANSPORT_STORAGE_CORRUPT")
                    return prepared
                for item in (descriptor, receipt):
                    if item is not None:
                        put_object(connection, item[0], item[1])
                put_object(
                    connection,
                    "task-result-transport-envelope/v1",
                    prepared.canonical_bytes,
                )
                self.fault_hook("after_envelope_object")
                connection.execute(
                    "INSERT INTO task_result_transport_envelopes VALUES "
                    "(?,?,?,?,?,?,?)",
                    values,
                )
                self.fault_hook("after_envelope_row")
                self.fault_hook("before_envelope_commit")
                return prepared
        except CurrentTaskResultTransportError:
            raise
        except CurrentAuthorityProjectionError as exc:
            self._fail("AUTHORITY_FENCED", exc.code)
        except sqlite3.IntegrityError as exc:
            self._fail("TRANSPORT_ENVELOPE_CONFLICT", type(exc).__name__)
        except sqlite3.Error as exc:
            self._fail("TRANSPORT_WRITE_FAILED", type(exc).__name__)
        except Exception as exc:
            self._fail("TRANSPORT_ENVELOPE_INVALID", self._detail(exc))

    def acknowledge(self, ack_bytes: bytes) -> AcceptedTaskResultTransport:
        ack, canonical, ack_sha = self._document(
            "task-result-transport-ack/v1",
            ack_bytes,
            "TRANSPORT_ACK_INVALID",
        )
        try:
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM task_result_transport_envelopes "
                    "WHERE task_id=?",
                    (ack["task_id"],),
                ).fetchone()
                if row is None:
                    self._fail("TRANSPORT_ENVELOPE_NOT_FOUND")
                envelope_raw = self._load_object(
                    connection,
                    row["transport_envelope_sha256"],
                    "task-result-transport-envelope/v1",
                )
                envelope, _, _ = self._document(
                    "task-result-transport-envelope/v1",
                    envelope_raw,
                    "TRANSPORT_STORAGE_CORRUPT",
                )
                receipt = None
                if row["transport_receipt_sha256"] is not None:
                    receipt_raw = self._load_object(
                        connection,
                        row["transport_receipt_sha256"],
                        "server-transport-receipt/v1",
                    )
                    receipt, _, _ = self._document(
                        "server-transport-receipt/v1",
                        receipt_raw,
                        "TRANSPORT_STORAGE_CORRUPT",
                    )
                contract.verify_task_result_transport_ack(
                    ack, envelope, transport_receipt=receipt
                )
                accepted = AcceptedTaskResultTransport(
                    task_id=ack["task_id"],
                    result_digest=row["result_digest"],
                    transport_envelope_digest=ack[
                        "transport_envelope_digest"
                    ],
                    ack_sha256=ack_sha,
                    ack_digest=ack["ack_digest"],
                    accepted_at=ack["accepted_at"],
                    canonical_bytes=canonical,
                    document=ack,
                )
                existing = connection.execute(
                    "SELECT ack_sha256,accepted_at "
                    "FROM task_result_transport_acks WHERE result_digest=?",
                    (row["result_digest"],),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (ack_sha, ack["accepted_at"]):
                        self._fail("TRANSPORT_ACK_CONFLICT")
                    if self._load_object(
                        connection,
                        ack_sha,
                        "task-result-transport-ack/v1",
                    ) != canonical:
                        self._fail("TRANSPORT_STORAGE_CORRUPT")
                    return accepted
                put_object(
                    connection,
                    "task-result-transport-ack/v1",
                    canonical,
                )
                self.fault_hook("after_ack_object")
                connection.execute(
                    "INSERT INTO task_result_transport_acks VALUES (?,?,?)",
                    (row["result_digest"], ack_sha, ack["accepted_at"]),
                )
                self.fault_hook("after_ack_row")
                self.fault_hook("before_ack_commit")
                return accepted
        except CurrentTaskResultTransportError:
            raise
        except sqlite3.IntegrityError as exc:
            self._fail("TRANSPORT_ACK_CONFLICT", type(exc).__name__)
        except sqlite3.Error as exc:
            self._fail("TRANSPORT_WRITE_FAILED", type(exc).__name__)
        except Exception as exc:
            self._fail("TRANSPORT_ACK_INVALID", self._detail(exc))

    def _build(self, connection, task_id, descriptor, receipt):
        row = connection.execute(
            "SELECT c.result_digest,c.task_result_core_sha256,"
            "m.task_version_authority_sha256 "
            "FROM terminalization_candidates c "
            "JOIN terminalization_commits m "
            "ON m.result_digest=c.result_digest AND m.task_id=c.task_id "
            "WHERE c.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            self._fail("TRANSPORT_RESULT_NOT_COMMITTED")
        result = self._stored_document(
            connection, row["result_digest"], "task-result/v1"
        )
        core = self._stored_document(
            connection,
            row["task_result_core_sha256"],
            "task-result-core/v1",
        )
        proof = self._stored_document(
            connection,
            row["task_version_authority_sha256"],
            "task-version-authority-proof/v1",
        )
        authority = self.authority.load_head(connection, task_id)
        self.authority.assert_runnable(authority)
        descriptor_document = descriptor[3] if descriptor is not None else None
        receipt_document = receipt[3] if receipt is not None else None
        receipt_availability = (
            {
                "availability": "available",
                "ref": self._content_ref(
                    "server-transport-receipt/v1", receipt_document
                ),
            }
            if receipt_document is not None
            else {
                "availability": "not_applicable",
                "reason_code": "TRANSPORT_RECEIPT_NOT_APPLICABLE",
            }
        )
        envelope = {
            "schema_id": "task-result-transport-envelope/v1",
            "package": authority.package.as_document(),
            "authority": authority.as_document(),
            "full_fence": deepcopy(proof["full_fence"]),
            "task_result": result,
            "task_result_digest": row["result_digest"],
            "task_result_core_ref": self._content_ref(
                "task-result-core/v1", core
            ),
            "task_result_core_digest": row["task_result_core_sha256"],
            "task_version_authority": proof,
            "worker_debug_descriptor": descriptor_document,
            "transport_receipt": receipt_availability,
        }
        checked = contract.verify_task_result_transport_envelope(
            envelope,
            core,
            transport_receipt=receipt_document,
            worker_debug_descriptor=descriptor_document,
        )
        raw = checked["canonical_bytes"]
        return PreparedTaskResultTransport(
            task_id=task_id,
            result_digest=row["result_digest"],
            task_result_core_sha256=row["task_result_core_sha256"],
            task_version_authority_sha256=row[
                "task_version_authority_sha256"
            ],
            worker_debug_descriptor_sha256=(
                descriptor[2] if descriptor is not None else None
            ),
            transport_receipt_sha256=(
                receipt[2] if receipt is not None else None
            ),
            transport_envelope_digest=checked[
                "transport_envelope_digest"
            ],
            canonical_bytes=raw,
            document=checked["document"],
            task_result_core=core,
        )

    @staticmethod
    def _envelope_values(prepared: PreparedTaskResultTransport) -> tuple:
        return (
            prepared.result_digest,
            prepared.task_id,
            prepared.transport_envelope_digest,
            prepared.task_result_core_sha256,
            prepared.task_version_authority_sha256,
            prepared.worker_debug_descriptor_sha256,
            prepared.transport_receipt_sha256,
        )

    def _stored_document(self, connection, digest, schema_id):
        raw = self._load_object(connection, digest, schema_id)
        document, _, _ = self._document(
            schema_id, raw, "TRANSPORT_STORAGE_CORRUPT"
        )
        return document

    @staticmethod
    def _load_object(connection, digest: str, schema_id: str) -> bytes:
        row = connection.execute(
            "SELECT content_schema_id,size_bytes,object_bytes "
            "FROM checkpoint_objects WHERE sha256=?",
            (digest,),
        ).fetchone()
        if row is None:
            raise CurrentTaskResultTransportError(
                "TRANSPORT_STORAGE_CORRUPT"
            )
        raw = bytes(row["object_bytes"])
        if (
            row["content_schema_id"] != schema_id
            or row["size_bytes"] != len(raw)
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            raise CurrentTaskResultTransportError(
                "TRANSPORT_STORAGE_CORRUPT"
            )
        return raw

    def _optional_document(self, schema_id, raw, code):
        if raw is None:
            return None
        document, canonical, digest = self._document(schema_id, raw, code)
        return schema_id, canonical, digest, document

    @staticmethod
    def _document(schema_id, raw, code):
        if not isinstance(raw, bytes):
            raise CurrentTaskResultTransportError(code)
        try:
            value = json.loads(raw.decode("utf-8"))
            if contract.canonical_document_bytes(value) != raw:
                raise CurrentTaskResultTransportError(code, "NONCANONICAL")
            schema = contract.schema(schema_id)
            checked = (
                contract.verify_document_digest(schema_id, value)
                if isinstance(schema.get("x-pullwise-digest"), dict)
                else contract.validate_document(schema_id, value)
            )
            canonical = contract.canonical_validated_bytes(schema_id, checked)
            if canonical != raw:
                raise CurrentTaskResultTransportError(code, "NONCANONICAL")
            return checked, canonical, hashlib.sha256(canonical).hexdigest()
        except CurrentTaskResultTransportError:
            raise
        except Exception as exc:
            raise CurrentTaskResultTransportError(
                code, CurrentTaskResultTransportStore._detail(exc)
            ) from exc

    @staticmethod
    def _content_ref(schema_id, document):
        raw = contract.canonical_validated_bytes(schema_id, document)
        digest = hashlib.sha256(raw).hexdigest()
        return {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_" + digest[:32],
            "content_schema_id": schema_id,
            "sha256": digest,
            "size_bytes": len(raw),
            "media_type": "application/json",
            "encoding": "utf-8",
        }

    @staticmethod
    def _detail(exc: BaseException) -> str:
        return str(getattr(exc, "code", type(exc).__name__))

    @staticmethod
    def _fail(code: str, detail: str = "") -> None:
        raise CurrentTaskResultTransportError(code, detail)


__all__ = [
    "AcceptedTaskResultTransport",
    "CurrentTaskResultTransportError",
    "CurrentTaskResultTransportStore",
    "PreparedTaskResultTransport",
]
