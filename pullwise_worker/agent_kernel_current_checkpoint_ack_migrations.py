"""Append-only migration for exact Server checkpoint ACK evidence."""

from __future__ import annotations

import hashlib


MIGRATION_4 = (
    """
    CREATE TABLE current_schema_v4 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 4),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoint_server_ack_documents (
        ack_digest TEXT PRIMARY KEY CHECK (length(ack_digest) = 64),
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
        request_digest TEXT NOT NULL UNIQUE CHECK (length(request_digest) = 64),
        ack_bytes BLOB NOT NULL CHECK (length(ack_bytes) >= 2),
        accepted_at TEXT NOT NULL,
        UNIQUE(task_id, generation),
        FOREIGN KEY(task_id, generation)
            REFERENCES checkpoint_server_acks(task_id, generation)
    ) STRICT
    """,
    """
    CREATE TRIGGER current_schema_v4_no_update
    BEFORE UPDATE ON current_schema_v4
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER current_schema_v4_no_delete
    BEFORE DELETE ON current_schema_v4
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER checkpoint_server_ack_documents_no_update
    BEFORE UPDATE ON checkpoint_server_ack_documents
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER checkpoint_server_ack_documents_no_delete
    BEFORE DELETE ON checkpoint_server_ack_documents
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
)

MIGRATION_4_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_4).encode("utf-8")
).hexdigest()

CURRENT_ACK_TABLES = frozenset(
    {"current_schema_v4", "checkpoint_server_ack_documents"}
)


__all__ = ["CURRENT_ACK_TABLES", "MIGRATION_4", "MIGRATION_4_SHA256"]
