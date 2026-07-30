"""Append-only migration for canonical runtime bootstrap roots."""

from __future__ import annotations

import hashlib


MIGRATION_2 = (
    """
    CREATE TABLE current_schema_v2 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 2),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE runtime_bootstraps (
        bootstrap_digest TEXT PRIMARY KEY CHECK (length(bootstrap_digest) = 64),
        task_id TEXT NOT NULL UNIQUE,
        authority_digest TEXT NOT NULL UNIQUE
            CHECK (length(authority_digest) = 64),
        accept_request_digest TEXT NOT NULL
            CHECK (length(accept_request_digest) = 64),
        accept_response_digest TEXT NOT NULL
            CHECK (length(accept_response_digest) = 64),
        task_record_sha256 TEXT NOT NULL CHECK (length(task_record_sha256) = 64),
        attempt_record_sha256 TEXT NOT NULL
            CHECK (length(attempt_record_sha256) = 64),
        owner_record_sha256 TEXT NOT NULL CHECK (length(owner_record_sha256) = 64),
        bootstrap_bytes BLOB NOT NULL,
        task_record_bytes BLOB NOT NULL,
        attempt_record_bytes BLOB NOT NULL,
        owner_record_bytes BLOB NOT NULL,
        FOREIGN KEY(authority_digest)
            REFERENCES authority_history(projection_digest)
            DEFERRABLE INITIALLY DEFERRED
    ) STRICT
    """,
    """
    CREATE TABLE runtime_task_records (
        task_id TEXT NOT NULL,
        task_version INTEGER NOT NULL CHECK (task_version >= 1),
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        source_kind TEXT NOT NULL
            CHECK (source_kind IN ('BOOTSTRAP','CHECKPOINT','RECOVERY')),
        source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
        lifecycle TEXT NOT NULL,
        desired_state TEXT NOT NULL,
        current_attempt_id TEXT,
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 0),
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 0),
        checkpoint_generation INTEGER NOT NULL CHECK (checkpoint_generation >= 0),
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
            REFERENCES runtime_task_records(task_id, task_version, record_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE runtime_attempt_records (
        attempt_id TEXT NOT NULL,
        state_version INTEGER NOT NULL CHECK (state_version >= 1),
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        source_kind TEXT NOT NULL
            CHECK (source_kind IN ('BOOTSTRAP','CHECKPOINT','RECOVERY')),
        source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
        task_id TEXT NOT NULL,
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        state TEXT NOT NULL,
        record_bytes BLOB NOT NULL,
        PRIMARY KEY(attempt_id, state_version),
        UNIQUE(attempt_id, state_version, record_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE runtime_attempt_heads (
        attempt_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY(attempt_id, state_version, record_sha256)
            REFERENCES runtime_attempt_records(
                attempt_id, state_version, record_sha256
            )
    ) STRICT
    """,
    """
    CREATE TABLE runtime_owner_records (
        task_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
        state_version INTEGER NOT NULL CHECK (state_version >= 1),
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        source_kind TEXT NOT NULL
            CHECK (source_kind IN ('BOOTSTRAP','CHECKPOINT','RECOVERY')),
        source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
        owner_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        state TEXT NOT NULL,
        record_bytes BLOB NOT NULL,
        PRIMARY KEY(task_id, owner_epoch, state_version),
        UNIQUE(task_id, owner_epoch, state_version, record_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE runtime_owner_heads (
        task_id TEXT PRIMARY KEY,
        owner_epoch INTEGER NOT NULL,
        state_version INTEGER NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY(task_id, owner_epoch, state_version, record_sha256)
            REFERENCES runtime_owner_records(
                task_id, owner_epoch, state_version, record_sha256
            )
    ) STRICT
    """,
)


IMMUTABLE_RUNTIME_TABLES = (
    "current_schema_v2",
    "runtime_bootstraps",
    "runtime_task_records",
    "runtime_attempt_records",
    "runtime_owner_records",
)

MIGRATION_2 += tuple(
    f"""
    CREATE TRIGGER {table}_no_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """
    for table in IMMUTABLE_RUNTIME_TABLES
) + tuple(
    f"""
    CREATE TRIGGER {table}_no_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """
    for table in IMMUTABLE_RUNTIME_TABLES
)

for _head, _identity in (
    ("runtime_task_heads", "task_id"),
    ("runtime_attempt_heads", "attempt_id"),
    ("runtime_owner_heads", "task_id"),
):
    MIGRATION_2 += (
        f"""
        CREATE TRIGGER {_head}_no_delete
        BEFORE DELETE ON {_head}
        BEGIN
            SELECT RAISE(ABORT, 'CURRENT_RUNTIME_HEAD_DELETE_FORBIDDEN');
        END
        """,
        f"""
        CREATE TRIGGER {_head}_identity_guard
        BEFORE UPDATE ON {_head}
        WHEN NEW.{_identity} IS NOT OLD.{_identity}
        BEGIN
            SELECT RAISE(ABORT, 'CURRENT_RUNTIME_HEAD_UPDATE_INVALID');
        END
        """,
    )

MIGRATION_2_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_2).encode("utf-8")
).hexdigest()

CURRENT_RUNTIME_TABLES = frozenset(
    {
        "current_schema_v2",
        "runtime_bootstraps",
        "runtime_task_records",
        "runtime_task_heads",
        "runtime_attempt_records",
        "runtime_attempt_heads",
        "runtime_owner_records",
        "runtime_owner_heads",
    }
)


__all__ = [
    "CURRENT_RUNTIME_TABLES",
    "MIGRATION_2",
    "MIGRATION_2_SHA256",
]
