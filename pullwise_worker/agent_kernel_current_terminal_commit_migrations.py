"""Append-only migration for the atomic terminal TaskResult commit."""

from __future__ import annotations

import hashlib


MIGRATION_7 = (
    """
    CREATE TABLE current_schema_v7 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 7),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    "DROP TRIGGER runtime_task_records_no_update",
    "DROP TRIGGER runtime_task_records_no_delete",
    "DROP TRIGGER runtime_task_heads_no_delete",
    "DROP TRIGGER runtime_task_heads_identity_guard",
    "ALTER TABLE runtime_task_records RENAME TO runtime_task_records_v6",
    "ALTER TABLE runtime_task_heads RENAME TO runtime_task_heads_v6",
    """
    CREATE TABLE runtime_task_records (
        task_id TEXT NOT NULL,
        task_version INTEGER NOT NULL CHECK (task_version >= 1),
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'BOOTSTRAP','CHECKPOINT','RECOVERY','TERMINALIZATION'
            )
        ),
        source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
        lifecycle TEXT NOT NULL,
        desired_state TEXT NOT NULL,
        current_attempt_id TEXT,
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 0),
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 0),
        checkpoint_generation INTEGER NOT NULL
            CHECK (checkpoint_generation >= 0),
        checkpoint_hash TEXT CHECK (
            checkpoint_hash IS NULL OR length(checkpoint_hash) = 64
        ),
        record_bytes BLOB NOT NULL,
        PRIMARY KEY(task_id, task_version),
        UNIQUE(task_id, task_version, record_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE runtime_task_heads (
        task_id TEXT PRIMARY KEY,
        task_version INTEGER NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY(task_id, task_version, record_sha256)
            REFERENCES runtime_task_records(
                task_id, task_version, record_sha256
            )
    ) STRICT
    """,
    """
    INSERT INTO runtime_task_records
    SELECT * FROM runtime_task_records_v6
    """,
    """
    INSERT INTO runtime_task_heads
    SELECT * FROM runtime_task_heads_v6
    """,
    "DROP TABLE runtime_task_heads_v6",
    "DROP TABLE runtime_task_records_v6",
    """
    CREATE TRIGGER runtime_task_records_no_update
    BEFORE UPDATE ON runtime_task_records
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER runtime_task_records_no_delete
    BEFORE DELETE ON runtime_task_records
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER runtime_task_heads_no_delete
    BEFORE DELETE ON runtime_task_heads
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_RUNTIME_HEAD_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER runtime_task_heads_identity_guard
    BEFORE UPDATE ON runtime_task_heads
    WHEN NEW.task_id IS NOT OLD.task_id
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_RUNTIME_HEAD_UPDATE_INVALID');
    END
    """,
    """
    CREATE TABLE terminalization_commits (
        result_digest TEXT PRIMARY KEY CHECK (length(result_digest) = 64)
            REFERENCES terminalization_candidates(result_digest),
        task_id TEXT NOT NULL UNIQUE,
        base_task_version INTEGER NOT NULL CHECK (base_task_version >= 1),
        published_from_version INTEGER NOT NULL CHECK (
            published_from_version = base_task_version + 1
        ),
        terminal_task_version INTEGER NOT NULL CHECK (
            terminal_task_version = published_from_version + 1
        ),
        base_task_record_sha256 TEXT NOT NULL
            CHECK (length(base_task_record_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        finalizing_task_record_sha256 TEXT NOT NULL
            CHECK (length(finalizing_task_record_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        terminal_task_record_sha256 TEXT NOT NULL
            CHECK (length(terminal_task_record_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        terminalization_event_sha256 TEXT NOT NULL
            CHECK (length(terminalization_event_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        publication_event_sha256 TEXT NOT NULL
            CHECK (length(publication_event_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_version_authority_sha256 TEXT NOT NULL
            CHECK (length(task_version_authority_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        committed_at TEXT NOT NULL,
        UNIQUE(task_id, published_from_version)
    ) STRICT
    """,
    """
    CREATE TRIGGER current_schema_v7_no_update
    BEFORE UPDATE ON current_schema_v7
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER current_schema_v7_no_delete
    BEFORE DELETE ON current_schema_v7
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER terminalization_commits_no_update
    BEFORE UPDATE ON terminalization_commits
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER terminalization_commits_no_delete
    BEFORE DELETE ON terminalization_commits
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
)

MIGRATION_7_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_7).encode("utf-8")
).hexdigest()

CURRENT_TERMINAL_COMMIT_TABLES = frozenset(
    {"current_schema_v7", "terminalization_commits"}
)


__all__ = [
    "CURRENT_TERMINAL_COMMIT_TABLES",
    "MIGRATION_7",
    "MIGRATION_7_SHA256",
]
