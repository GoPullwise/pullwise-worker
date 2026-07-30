"""Fail-closed consumption of one canonical Server runtime bootstrap."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable

from .agent_kernel_current_authority import (
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_package import (
    ServerAuthorityEnvelope,
    canonical_current_document_bytes,
    canonical_validated_current_bytes,
    validate_current_document,
    verify_current_document_digest,
)
from .agent_kernel_current_requirements import (
    CurrentRequirementLedgerError,
    install_bootstrap_semantics,
    verify_bootstrap_semantics,
)


BOOTSTRAP_SCHEMA_ID = "agent-task-runtime-bootstrap/v1"


class CurrentRuntimeBootstrapError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class CurrentRuntimeBootstrapConsumer:
    """Validate and atomically install the only current runtime root."""

    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentRuntimeBootstrapError("CURRENT_DATABASE_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise CurrentRuntimeBootstrapError("RUNTIME_BOOTSTRAP_HOOK_INVALID")
        self.database = database
        self.fault_hook = fault_hook or (lambda _stage: None)
        self.authority = CurrentAuthorityProjection(database)

    def ingest(self, raw: bytes) -> ServerAuthorityEnvelope:
        document, envelope, roots = self._parse(raw)
        bootstrap_digest = document["bootstrap_digest"]
        task_id = envelope.task_id
        try:
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM runtime_bootstraps WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if existing is not None:
                    self._assert_exact_replay(
                        connection, existing, raw, envelope, roots
                    )
                    return envelope
                collision = connection.execute(
                    "SELECT task_id FROM runtime_bootstraps "
                    "WHERE bootstrap_digest = ?",
                    (bootstrap_digest,),
                ).fetchone()
                if collision is not None:
                    self._fail("RUNTIME_BOOTSTRAP_STORAGE_CORRUPT")
                self._insert_bootstrap(
                    connection, document, raw, envelope, roots
                )
                self.fault_hook("after_bootstrap")
                self._insert_task(connection, document, roots["task"])
                self.fault_hook("after_task")
                self._insert_attempt(connection, document, roots["attempt"])
                self.fault_hook("after_attempt")
                self._insert_owner(connection, document, roots["owner"])
                self.fault_hook("after_owner")
                recorded = self.authority.apply_active(connection, envelope)
                self.fault_hook("after_authority")
                install_bootstrap_semantics(connection, document)
                self.fault_hook("after_semantic_roots")
                self.fault_hook("before_commit")
                return recorded
        except CurrentRuntimeBootstrapError:
            raise
        except CurrentAuthorityProjectionError as exc:
            code = (
                "RUNTIME_BOOTSTRAP_STORAGE_CORRUPT"
                if exc.code == "AUTHORITY_HISTORY_CORRUPT"
                else "RUNTIME_BOOTSTRAP_AUTHORITY_CONFLICT"
            )
            raise CurrentRuntimeBootstrapError(code, exc.code) from exc
        except CurrentRequirementLedgerError as exc:
            raise CurrentRuntimeBootstrapError(
                "RUNTIME_BOOTSTRAP_SEMANTIC_ROOT_INVALID", exc.code
            ) from exc
        except sqlite3.Error as exc:
            raise CurrentRuntimeBootstrapError(
                "RUNTIME_BOOTSTRAP_WRITE_FAILED", type(exc).__name__
            ) from exc

    def _parse(
        self, raw: bytes
    ) -> tuple[
        dict[str, object],
        ServerAuthorityEnvelope,
        dict[str, tuple[dict[str, object], bytes, str]],
    ]:
        if not isinstance(raw, bytes):
            self._fail("RUNTIME_BOOTSTRAP_INVALID")
        try:
            detached = json.loads(raw.decode("utf-8"))
            canonical = canonical_current_document_bytes(detached)
        except Exception as exc:
            raise CurrentRuntimeBootstrapError(
                "RUNTIME_BOOTSTRAP_INVALID", type(exc).__name__
            ) from exc
        if canonical != raw:
            self._fail("RUNTIME_BOOTSTRAP_NONCANONICAL")
        try:
            document = verify_current_document_digest(
                BOOTSTRAP_SCHEMA_ID, detached
            )
            if canonical_validated_current_bytes(BOOTSTRAP_SCHEMA_ID, document) != raw:
                self._fail("RUNTIME_BOOTSTRAP_NONCANONICAL")
            authority_bytes = canonical_validated_current_bytes(
                "server-authority-envelope/v1", document["authority"]
            )
            envelope = ServerAuthorityEnvelope.from_canonical_bytes(authority_bytes)
            roots = {
                name: self._root(schema_id, document["construction_roots"][field])
                for name, schema_id, field in (
                    ("task", "task-record/v1", "task_record"),
                    ("attempt", "attempt-record/v1", "attempt"),
                    ("owner", "task-owner/v1", "owner"),
                )
            }
        except CurrentRuntimeBootstrapError:
            raise
        except Exception as exc:
            raise CurrentRuntimeBootstrapError(
                "RUNTIME_BOOTSTRAP_INVALID", type(exc).__name__
            ) from exc
        return document, envelope, roots

    @staticmethod
    def _root(
        schema_id: str, value: object
    ) -> tuple[dict[str, object], bytes, str]:
        document = validate_current_document(schema_id, value)
        raw = canonical_validated_current_bytes(schema_id, document)
        return document, raw, hashlib.sha256(raw).hexdigest()

    def _assert_exact_replay(
        self,
        connection: sqlite3.Connection,
        existing: sqlite3.Row,
        raw: bytes,
        envelope: ServerAuthorityEnvelope,
        roots: dict[str, tuple[dict[str, object], bytes, str]],
    ) -> None:
        expected = (
            raw,
            roots["task"][1],
            roots["attempt"][1],
            roots["owner"][1],
            envelope.digest,
        )
        actual = (
            bytes(existing["bootstrap_bytes"]),
            bytes(existing["task_record_bytes"]),
            bytes(existing["attempt_record_bytes"]),
            bytes(existing["owner_record_bytes"]),
            existing["authority_digest"],
        )
        if actual != expected:
            self._fail("RUNTIME_BOOTSTRAP_REPLAY_CONFLICT")
        for table, identity, document, encoded, digest in (
            ("runtime_task_records", "task_id", *roots["task"]),
            ("runtime_attempt_records", "attempt_id", *roots["attempt"]),
            ("runtime_owner_records", "task_id", *roots["owner"]),
        ):
            row = connection.execute(
                f"SELECT record_bytes, record_sha256, source_digest FROM {table} "
                f"WHERE {identity} = ? ORDER BY rowid LIMIT 1",
                (document[identity],),
            ).fetchone()
            if row is None or (
                bytes(row["record_bytes"]),
                row["record_sha256"],
                row["source_digest"],
            ) != (encoded, digest, existing["bootstrap_digest"]):
                self._fail("RUNTIME_BOOTSTRAP_STORAGE_CORRUPT")
        authority = connection.execute(
            "SELECT projection_bytes, grant_bytes FROM authority_history "
            "WHERE projection_digest = ?",
            (envelope.digest,),
        ).fetchone()
        if authority is None or (
            bytes(authority["projection_bytes"]),
            bytes(authority["grant_bytes"]),
        ) != (envelope.canonical_bytes, envelope.grant.canonical_bytes):
            self._fail("RUNTIME_BOOTSTRAP_STORAGE_CORRUPT")
        try:
            verify_bootstrap_semantics(
                connection, json.loads(raw.decode("utf-8"))
            )
        except CurrentRequirementLedgerError as exc:
            raise CurrentRuntimeBootstrapError(
                "RUNTIME_BOOTSTRAP_STORAGE_CORRUPT", exc.code
            ) from exc

    @staticmethod
    def _insert_bootstrap(
        connection: sqlite3.Connection,
        document: dict[str, object],
        raw: bytes,
        envelope: ServerAuthorityEnvelope,
        roots: dict[str, tuple[dict[str, object], bytes, str]],
    ) -> None:
        connection.execute(
            "INSERT INTO runtime_bootstraps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                document["bootstrap_digest"],
                envelope.task_id,
                envelope.digest,
                document["accept_request"]["accept_request_digest"],
                document["accept_response"]["response_digest"],
                roots["task"][2],
                roots["attempt"][2],
                roots["owner"][2],
                raw,
                roots["task"][1],
                roots["attempt"][1],
                roots["owner"][1],
            ),
        )

    @staticmethod
    def _insert_task(
        connection: sqlite3.Connection,
        bootstrap: dict[str, object],
        root: tuple[dict[str, object], bytes, str],
    ) -> None:
        task, raw, digest = root
        connection.execute(
            "INSERT INTO runtime_task_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task["task_id"], task["task_version"], digest, "BOOTSTRAP",
                bootstrap["bootstrap_digest"], task["lifecycle"],
                task["desired_state"], task["current_attempt_id"],
                task["native_epoch"], task["owner_epoch"],
                task["current_checkpoint_generation"],
                task["current_checkpoint_hash"], raw,
            ),
        )
        connection.execute(
            "INSERT INTO runtime_task_heads VALUES (?,?,?)",
            (task["task_id"], task["task_version"], digest),
        )

    @staticmethod
    def _insert_attempt(
        connection: sqlite3.Connection,
        bootstrap: dict[str, object],
        root: tuple[dict[str, object], bytes, str],
    ) -> None:
        attempt, raw, digest = root
        connection.execute(
            "INSERT INTO runtime_attempt_records VALUES (?,?,?,?,?,?,?,?,?)",
            (
                attempt["attempt_id"], attempt["state_version"], digest,
                "BOOTSTRAP", bootstrap["bootstrap_digest"], attempt["task_id"],
                attempt["native_epoch"], attempt["state"], raw,
            ),
        )
        connection.execute(
            "INSERT INTO runtime_attempt_heads VALUES (?,?,?)",
            (attempt["attempt_id"], attempt["state_version"], digest),
        )

    @staticmethod
    def _insert_owner(
        connection: sqlite3.Connection,
        bootstrap: dict[str, object],
        root: tuple[dict[str, object], bytes, str],
    ) -> None:
        owner, raw, digest = root
        connection.execute(
            "INSERT INTO runtime_owner_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                owner["task_id"], owner["owner_epoch"], owner["state_version"],
                digest, "BOOTSTRAP", bootstrap["bootstrap_digest"],
                owner["owner_id"], owner["attempt_id"], owner["native_epoch"],
                owner["state"], raw,
            ),
        )
        connection.execute(
            "INSERT INTO runtime_owner_heads VALUES (?,?,?,?)",
            (
                owner["task_id"], owner["owner_epoch"],
                owner["state_version"], digest,
            ),
        )

    @staticmethod
    def _fail(code: str) -> None:
        raise CurrentRuntimeBootstrapError(code)


__all__ = ["CurrentRuntimeBootstrapConsumer", "CurrentRuntimeBootstrapError"]
