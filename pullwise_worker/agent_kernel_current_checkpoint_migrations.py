"""Append-only migration for committed checkpoint chains and ACKs."""

from __future__ import annotations

import hashlib


MIGRATION_3 = (
    """
    CREATE TABLE current_schema_v3 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 3),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoint_objects (
        sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
        content_schema_id TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 2),
        object_bytes BLOB NOT NULL,
        CHECK (length(object_bytes) = size_bytes)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoint_manifests (
        manifest_hash TEXT PRIMARY KEY CHECK (length(manifest_hash) = 64),
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        previous_generation INTEGER NOT NULL CHECK (previous_generation >= 0),
        previous_manifest_hash TEXT CHECK (
            previous_manifest_hash IS NULL OR length(previous_manifest_hash) = 64
        ),
        committed_from_task_version INTEGER NOT NULL
            CHECK (committed_from_task_version >= 1),
        committed_task_version INTEGER NOT NULL CHECK (committed_task_version >= 2),
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        attempt_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
        machine_state_sha256 TEXT NOT NULL
            REFERENCES checkpoint_objects(sha256),
        semantic_state_sha256 TEXT NOT NULL
            REFERENCES checkpoint_objects(sha256),
        manifest_bytes BLOB NOT NULL,
        committed_at TEXT NOT NULL,
        UNIQUE(task_id, generation),
        UNIQUE(task_id, generation, manifest_hash),
        FOREIGN KEY(previous_manifest_hash)
            REFERENCES checkpoint_manifests(manifest_hash)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoint_index (
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
        previous_manifest_hash TEXT CHECK (
            previous_manifest_hash IS NULL OR length(previous_manifest_hash) = 64
        ),
        committed_at TEXT NOT NULL,
        PRIMARY KEY(task_id, generation),
        UNIQUE(task_id, generation, manifest_hash),
        FOREIGN KEY(task_id, generation, manifest_hash)
            REFERENCES checkpoint_manifests(task_id, generation, manifest_hash)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoint_heads (
        task_id TEXT PRIMARY KEY,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
        previous_manifest_hash TEXT CHECK (
            previous_manifest_hash IS NULL OR length(previous_manifest_hash) = 64
        ),
        committed_task_version INTEGER NOT NULL CHECK (committed_task_version >= 2),
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        attempt_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
        FOREIGN KEY(task_id, generation, manifest_hash)
            REFERENCES checkpoint_index(task_id, generation, manifest_hash)
    ) STRICT
    """,
    """
    CREATE TABLE checkpoint_server_acks (
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
        previous_manifest_hash TEXT CHECK (
            previous_manifest_hash IS NULL OR length(previous_manifest_hash) = 64
        ),
        authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
        task_version INTEGER NOT NULL CHECK (task_version >= 2),
        deletion_version INTEGER NOT NULL CHECK (deletion_version >= 0),
        transport_epoch INTEGER NOT NULL CHECK (transport_epoch >= 1),
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        acknowledged_at TEXT NOT NULL,
        PRIMARY KEY(task_id, generation),
        UNIQUE(manifest_hash),
        FOREIGN KEY(task_id, generation, manifest_hash)
            REFERENCES checkpoint_index(task_id, generation, manifest_hash)
    ) STRICT
    """,
    """
    CREATE TRIGGER checkpoint_heads_no_delete
    BEFORE DELETE ON checkpoint_heads
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_CHECKPOINT_HEAD_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER checkpoint_heads_cas_guard
    BEFORE UPDATE ON checkpoint_heads
    WHEN NEW.task_id IS NOT OLD.task_id
      OR NEW.generation != OLD.generation + 1
      OR NEW.previous_manifest_hash IS NOT OLD.manifest_hash
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_CHECKPOINT_HEAD_CAS_INVALID');
    END
    """,
)

_IMMUTABLE_CHECKPOINT_TABLES = (
    "current_schema_v3",
    "checkpoint_objects",
    "checkpoint_manifests",
    "checkpoint_index",
    "checkpoint_server_acks",
)

MIGRATION_3 += tuple(
    f"""
    CREATE TRIGGER {table}_no_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """
    for table in _IMMUTABLE_CHECKPOINT_TABLES
) + tuple(
    f"""
    CREATE TRIGGER {table}_no_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """
    for table in _IMMUTABLE_CHECKPOINT_TABLES
)

MIGRATION_3_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_3).encode("utf-8")
).hexdigest()

CURRENT_CHECKPOINT_TABLES = frozenset(
    {
        "current_schema_v3",
        "checkpoint_objects",
        "checkpoint_manifests",
        "checkpoint_index",
        "checkpoint_heads",
        "checkpoint_server_acks",
    }
)


__all__ = [
    "CURRENT_CHECKPOINT_TABLES",
    "MIGRATION_3",
    "MIGRATION_3_SHA256",
]
