"""Append-only storage for current TaskResult transport projections."""

from __future__ import annotations

import hashlib


MIGRATION_8 = (
    """
    CREATE TABLE current_schema_v8 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 8),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE task_result_transport_envelopes (
        result_digest TEXT PRIMARY KEY CHECK (length(result_digest) = 64)
            REFERENCES terminalization_commits(result_digest),
        task_id TEXT NOT NULL UNIQUE,
        transport_envelope_sha256 TEXT NOT NULL UNIQUE
            CHECK (length(transport_envelope_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_result_core_sha256 TEXT NOT NULL
            CHECK (length(task_result_core_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_version_authority_sha256 TEXT NOT NULL
            CHECK (length(task_version_authority_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        worker_debug_descriptor_sha256 TEXT
            CHECK (
                worker_debug_descriptor_sha256 IS NULL
                OR length(worker_debug_descriptor_sha256) = 64
            )
            REFERENCES checkpoint_objects(sha256),
        transport_receipt_sha256 TEXT
            CHECK (
                transport_receipt_sha256 IS NULL
                OR length(transport_receipt_sha256) = 64
            )
            REFERENCES checkpoint_objects(sha256),
        CHECK (
            transport_receipt_sha256 IS NULL
            OR worker_debug_descriptor_sha256 IS NOT NULL
        )
    ) STRICT
    """,
    """
    CREATE TABLE task_result_transport_acks (
        result_digest TEXT PRIMARY KEY
            REFERENCES task_result_transport_envelopes(result_digest),
        ack_sha256 TEXT NOT NULL UNIQUE CHECK (length(ack_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        accepted_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TRIGGER current_schema_v8_no_update
    BEFORE UPDATE ON current_schema_v8
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER current_schema_v8_no_delete
    BEFORE DELETE ON current_schema_v8
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER task_result_transport_envelopes_no_update
    BEFORE UPDATE ON task_result_transport_envelopes
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER task_result_transport_envelopes_no_delete
    BEFORE DELETE ON task_result_transport_envelopes
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER task_result_transport_acks_no_update
    BEFORE UPDATE ON task_result_transport_acks
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER task_result_transport_acks_no_delete
    BEFORE DELETE ON task_result_transport_acks
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
)

MIGRATION_8_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_8).encode("utf-8")
).hexdigest()

CURRENT_TRANSPORT_TABLES = frozenset(
    {
        "current_schema_v8",
        "task_result_transport_envelopes",
        "task_result_transport_acks",
    }
)


__all__ = [
    "CURRENT_TRANSPORT_TABLES",
    "MIGRATION_8",
    "MIGRATION_8_SHA256",
]
