"""Immutable, authority-fenced Requirement Ledger persistence."""

from __future__ import annotations

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


LEDGER_SCHEMA = "requirement-ledger/v1"
BOOTSTRAP_SEMANTIC_SCHEMAS = (
    ("task-request/v1", "task_request"),
    ("effective-execution-policy/v1", "effective_policy"),
    (LEDGER_SCHEMA, "requirement_ledger"),
)


class CurrentRequirementLedgerError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CurrentRequirementLedger:
    task_id: str
    ledger_version: int
    ledger_digest: str
    object_sha256: str
    canonical_bytes: bytes
    previous_ledger_digest: str | None
    committed_at: str


def _error_detail(error: BaseException) -> str:
    return str(getattr(error, "code", type(error).__name__))


def _parse_exact_ledger(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise CurrentRequirementLedgerError("REQUIREMENT_LEDGER_INVALID")
    try:
        detached = json.loads(raw.decode("utf-8"))
        if contract.canonical_document_bytes(detached) != raw:
            raise CurrentRequirementLedgerError(
                "REQUIREMENT_LEDGER_NONCANONICAL"
            )
        checked = contract.verify_document_digest(LEDGER_SCHEMA, detached)
        if contract.canonical_validated_bytes(LEDGER_SCHEMA, checked) != raw:
            raise CurrentRequirementLedgerError(
                "REQUIREMENT_LEDGER_NONCANONICAL"
            )
        return checked
    except CurrentRequirementLedgerError:
        raise
    except Exception as exc:
        raise CurrentRequirementLedgerError(
            "REQUIREMENT_LEDGER_INVALID", _error_detail(exc)
        ) from exc


def _put_object(
    connection: sqlite3.Connection,
    schema_id: str,
    raw: bytes,
) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO checkpoint_objects "
        "(sha256,content_schema_id,size_bytes,object_bytes) VALUES (?,?,?,?)",
        (digest, schema_id, len(raw), raw),
    )
    row = connection.execute(
        "SELECT content_schema_id,size_bytes,object_bytes "
        "FROM checkpoint_objects WHERE sha256=?",
        (digest,),
    ).fetchone()
    if row is None or (
        row["content_schema_id"],
        row["size_bytes"],
        bytes(row["object_bytes"]),
    ) != (schema_id, len(raw), raw):
        raise CurrentRequirementLedgerError("SEMANTIC_OBJECT_COLLISION")
    return digest


def install_bootstrap_semantics(
    connection: sqlite3.Connection,
    bootstrap: dict[str, object],
) -> None:
    request = bootstrap["accept_request"]
    roots: dict[str, tuple[dict[str, object], bytes, str]] = {}
    try:
        for schema_id, field in BOOTSTRAP_SEMANTIC_SCHEMAS:
            document = contract.validate_document(schema_id, request[field])
            raw = contract.canonical_validated_bytes(schema_id, document)
            roots[field] = (
                document,
                raw,
                _put_object(connection, schema_id, raw),
            )
    except CurrentRequirementLedgerError:
        raise
    except Exception as exc:
        raise CurrentRequirementLedgerError(
            "RUNTIME_BOOTSTRAP_SEMANTIC_ROOT_INVALID", _error_detail(exc)
        ) from exc
    ledger, _raw, object_sha256 = roots["requirement_ledger"]
    connection.execute(
        "INSERT INTO requirement_ledger_versions VALUES (?,?,?,?,?,?)",
        (
            ledger["ledger_digest"],
            object_sha256,
            ledger["task_id"],
            ledger["ledger_version"],
            None,
            bootstrap["accept_response"]["accepted_at"],
        ),
    )
    connection.execute(
        "INSERT INTO requirement_ledger_heads VALUES (?,?,?,?)",
        (
            ledger["task_id"],
            ledger["ledger_version"],
            ledger["ledger_digest"],
            object_sha256,
        ),
    )


def verify_bootstrap_semantics(
    connection: sqlite3.Connection,
    bootstrap: dict[str, object],
) -> None:
    request = bootstrap["accept_request"]
    for schema_id, field in BOOTSTRAP_SEMANTIC_SCHEMAS:
        document = contract.validate_document(schema_id, request[field])
        raw = contract.canonical_validated_bytes(schema_id, document)
        digest = hashlib.sha256(raw).hexdigest()
        row = connection.execute(
            "SELECT content_schema_id,size_bytes,object_bytes "
            "FROM checkpoint_objects WHERE sha256=?",
            (digest,),
        ).fetchone()
        if row is None or (
            row["content_schema_id"],
            row["size_bytes"],
            bytes(row["object_bytes"]),
        ) != (schema_id, len(raw), raw):
            raise CurrentRequirementLedgerError(
                "RUNTIME_BOOTSTRAP_SEMANTIC_ROOT_CORRUPT"
            )
    ledger = request["requirement_ledger"]
    current = _load_current(connection, ledger["task_id"])
    raw = contract.canonical_validated_bytes(LEDGER_SCHEMA, ledger)
    if (
        current.ledger_version != ledger["ledger_version"]
        or current.ledger_digest != ledger["ledger_digest"]
        or current.canonical_bytes != raw
        or current.previous_ledger_digest is not None
    ):
        raise CurrentRequirementLedgerError(
            "RUNTIME_BOOTSTRAP_SEMANTIC_ROOT_CORRUPT"
        )


def _row_value(row: sqlite3.Row) -> CurrentRequirementLedger:
    raw = bytes(row["object_bytes"])
    document = _parse_exact_ledger(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if (
        document["task_id"] != row["task_id"]
        or document["ledger_version"] != row["ledger_version"]
        or document["ledger_digest"] != row["ledger_digest"]
        or digest != row["object_sha256"]
        or row["content_schema_id"] != LEDGER_SCHEMA
        or row["size_bytes"] != len(raw)
    ):
        raise CurrentRequirementLedgerError(
            "REQUIREMENT_LEDGER_STORAGE_CORRUPT"
        )
    return CurrentRequirementLedger(
        task_id=row["task_id"],
        ledger_version=row["ledger_version"],
        ledger_digest=row["ledger_digest"],
        object_sha256=row["object_sha256"],
        canonical_bytes=raw,
        previous_ledger_digest=row["previous_ledger_digest"],
        committed_at=row["committed_at"],
    )


def _load_current(
    connection: sqlite3.Connection, task_id: str
) -> CurrentRequirementLedger:
    row = connection.execute(
        "SELECT v.*,o.content_schema_id,o.size_bytes,o.object_bytes "
        "FROM requirement_ledger_heads h "
        "JOIN requirement_ledger_versions v "
        "USING(task_id,ledger_version,ledger_digest,object_sha256) "
        "JOIN checkpoint_objects o ON o.sha256=v.object_sha256 "
        "WHERE h.task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise CurrentRequirementLedgerError("REQUIREMENT_LEDGER_NOT_FOUND")
    return _row_value(row)


load_current_requirement_ledger = _load_current


class CurrentRequirementLedgerStore:
    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentRequirementLedgerError("CURRENT_DATABASE_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise CurrentRequirementLedgerError(
                "REQUIREMENT_LEDGER_HOOK_INVALID"
            )
        self.database = database
        self.fault_hook = fault_hook or (lambda _stage: None)
        self.authority = CurrentAuthorityProjection(database)

    def current(self, task_id: str) -> CurrentRequirementLedger:
        with self.database.connect() as connection:
            return _load_current(connection, task_id)

    def append(
        self,
        raw: bytes,
        *,
        expected_previous_digest: str,
    ) -> CurrentRequirementLedger:
        candidate = _parse_exact_ledger(raw)
        task_id = candidate["task_id"]
        try:
            with self.database.transaction() as connection:
                authority = self.authority.load_head(connection, task_id)
                self.authority.assert_runnable(authority)
                replay = self._load_digest(
                    connection, candidate["ledger_digest"]
                )
                if replay is not None:
                    if (
                        replay.canonical_bytes != raw
                        or replay.previous_ledger_digest
                        != expected_previous_digest
                    ):
                        self._fail("REQUIREMENT_LEDGER_REPLAY_CONFLICT")
                    return replay
                previous = _load_current(connection, task_id)
                if previous.ledger_digest != expected_previous_digest:
                    self._fail("REQUIREMENT_LEDGER_CAS_CONFLICT")
                previous_document = _parse_exact_ledger(
                    previous.canonical_bytes
                )
                try:
                    contract.validate_requirement_ledger_transition(
                        previous_document, candidate
                    )
                except Exception as exc:
                    self._fail(
                        "REQUIREMENT_LEDGER_TRANSITION_INVALID",
                        _error_detail(exc),
                    )
                object_sha256 = _put_object(
                    connection, LEDGER_SCHEMA, raw
                )
                self.fault_hook("after_object")
                committed_at = candidate["entries"][-1]["introduced_at"]
                connection.execute(
                    "INSERT INTO requirement_ledger_versions "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        candidate["ledger_digest"],
                        object_sha256,
                        task_id,
                        candidate["ledger_version"],
                        expected_previous_digest,
                        committed_at,
                    ),
                )
                self.fault_hook("after_version")
                self.fault_hook("before_head_cas")
                cursor = connection.execute(
                    "UPDATE requirement_ledger_heads SET "
                    "ledger_version=?,ledger_digest=?,object_sha256=? "
                    "WHERE task_id=? AND ledger_version=? AND ledger_digest=?",
                    (
                        candidate["ledger_version"],
                        candidate["ledger_digest"],
                        object_sha256,
                        task_id,
                        previous.ledger_version,
                        expected_previous_digest,
                    ),
                )
                if cursor.rowcount != 1:
                    self._fail("REQUIREMENT_LEDGER_CAS_CONFLICT")
                self.fault_hook("after_head_cas")
                self.fault_hook("before_commit")
                return _load_current(connection, task_id)
        except CurrentRequirementLedgerError:
            raise
        except CurrentAuthorityProjectionError as exc:
            self._fail("AUTHORITY_FENCED", exc.code)
        except sqlite3.Error as exc:
            self._fail(
                "REQUIREMENT_LEDGER_WRITE_FAILED", type(exc).__name__
            )

    @staticmethod
    def _load_digest(
        connection: sqlite3.Connection, ledger_digest: str
    ) -> CurrentRequirementLedger | None:
        row = connection.execute(
            "SELECT v.*,o.content_schema_id,o.size_bytes,o.object_bytes "
            "FROM requirement_ledger_versions v "
            "JOIN checkpoint_objects o ON o.sha256=v.object_sha256 "
            "WHERE v.ledger_digest=?",
            (ledger_digest,),
        ).fetchone()
        return None if row is None else _row_value(row)

    @staticmethod
    def _fail(code: str, detail: str = "") -> None:
        raise CurrentRequirementLedgerError(code, detail)


__all__ = [
    "CurrentRequirementLedger",
    "CurrentRequirementLedgerError",
    "CurrentRequirementLedgerStore",
    "install_bootstrap_semantics",
    "load_current_requirement_ledger",
    "verify_bootstrap_semantics",
]
