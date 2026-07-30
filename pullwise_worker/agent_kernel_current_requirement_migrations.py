"""Append-only migration for the current Requirement Ledger head."""

from __future__ import annotations

import hashlib


MIGRATION_5 = (
    """
    CREATE TABLE current_schema_v5 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 5),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE requirement_ledger_versions (
        ledger_digest TEXT PRIMARY KEY CHECK (length(ledger_digest) = 64),
        object_sha256 TEXT NOT NULL UNIQUE
            CHECK (length(object_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_id TEXT NOT NULL,
        ledger_version INTEGER NOT NULL CHECK (ledger_version >= 1),
        previous_ledger_digest TEXT
            CHECK (
                previous_ledger_digest IS NULL
                OR length(previous_ledger_digest) = 64
            )
            REFERENCES requirement_ledger_versions(ledger_digest),
        committed_at TEXT NOT NULL,
        UNIQUE(task_id, ledger_version),
        UNIQUE(task_id, ledger_version, ledger_digest, object_sha256),
        CHECK (
            (ledger_version = 1 AND previous_ledger_digest IS NULL)
            OR (ledger_version > 1 AND previous_ledger_digest IS NOT NULL)
        )
    ) STRICT
    """,
    """
    CREATE TABLE requirement_ledger_heads (
        task_id TEXT PRIMARY KEY,
        ledger_version INTEGER NOT NULL CHECK (ledger_version >= 1),
        ledger_digest TEXT NOT NULL CHECK (length(ledger_digest) = 64),
        object_sha256 TEXT NOT NULL CHECK (length(object_sha256) = 64),
        FOREIGN KEY(task_id, ledger_version, ledger_digest, object_sha256)
            REFERENCES requirement_ledger_versions(
                task_id, ledger_version, ledger_digest, object_sha256
            )
    ) STRICT
    """,
    """
    CREATE TRIGGER current_schema_v5_no_update
    BEFORE UPDATE ON current_schema_v5
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER current_schema_v5_no_delete
    BEFORE DELETE ON current_schema_v5
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER requirement_ledger_versions_no_update
    BEFORE UPDATE ON requirement_ledger_versions
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER requirement_ledger_versions_no_delete
    BEFORE DELETE ON requirement_ledger_versions
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER requirement_ledger_heads_no_delete
    BEFORE DELETE ON requirement_ledger_heads
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_REQUIREMENT_HEAD_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER requirement_ledger_heads_cas_guard
    BEFORE UPDATE ON requirement_ledger_heads
    WHEN NEW.task_id IS NOT OLD.task_id
      OR NEW.ledger_version != OLD.ledger_version + 1
      OR NEW.ledger_digest IS OLD.ledger_digest
      OR NEW.object_sha256 IS OLD.object_sha256
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_REQUIREMENT_HEAD_CAS_INVALID');
    END
    """,
)

MIGRATION_5_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_5).encode("utf-8")
).hexdigest()

CURRENT_REQUIREMENT_TABLES = frozenset(
    {
        "current_schema_v5",
        "requirement_ledger_versions",
        "requirement_ledger_heads",
    }
)


__all__ = [
    "CURRENT_REQUIREMENT_TABLES",
    "MIGRATION_5",
    "MIGRATION_5_SHA256",
]
