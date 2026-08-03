"""Append-only storage for sealed current WorkerDebugFragment captures."""

from __future__ import annotations

import hashlib


MIGRATION_9 = (
    """
    CREATE TABLE current_schema_v9 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 9),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE worker_debug_fragments (
        fragment_sha256 TEXT PRIMARY KEY CHECK (length(fragment_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        transport_attempt_id TEXT NOT NULL,
        transport_epoch INTEGER NOT NULL CHECK (transport_epoch >= 1),
        native_attempt_id TEXT NOT NULL,
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        capture_kind TEXT NOT NULL CHECK (
            capture_kind IN ('startup','checkpoint','terminal','crash')
        ),
        snapshot_seq INTEGER NOT NULL CHECK (snapshot_seq >= 1),
        archive_sha256 TEXT NOT NULL UNIQUE CHECK (length(archive_sha256) = 64)
            REFERENCES content_objects(sha256),
        file_manifest_sha256 TEXT NOT NULL
            CHECK (length(file_manifest_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        redaction_report_sha256 TEXT NOT NULL
            CHECK (length(redaction_report_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_result_core_sha256 TEXT
            CHECK (
                task_result_core_sha256 IS NULL
                OR length(task_result_core_sha256) = 64
            )
            REFERENCES checkpoint_objects(sha256),
        captured_at TEXT NOT NULL,
        UNIQUE (
            task_id,job_id,run_id,lease_id,transport_attempt_id,
            transport_epoch,native_attempt_id,native_epoch,snapshot_seq
        ),
        CHECK (
            (capture_kind = 'terminal' AND task_result_core_sha256 IS NOT NULL)
            OR
            (capture_kind != 'terminal' AND task_result_core_sha256 IS NULL)
        )
    ) STRICT
    """,
    """
    CREATE TABLE worker_debug_descriptors (
        descriptor_sha256 TEXT PRIMARY KEY CHECK (length(descriptor_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        fragment_sha256 TEXT NOT NULL UNIQUE
            REFERENCES worker_debug_fragments(fragment_sha256),
        state TEXT NOT NULL CHECK (state IN ('uploaded','local_only')),
        server_receipt_sha256 TEXT
            CHECK (
                server_receipt_sha256 IS NULL
                OR length(server_receipt_sha256) = 64
            )
            REFERENCES checkpoint_objects(sha256),
        CHECK (
            (state = 'uploaded' AND server_receipt_sha256 IS NOT NULL)
            OR
            (state = 'local_only' AND server_receipt_sha256 IS NULL)
        )
    ) STRICT
    """,
)

for _table in (
    "current_schema_v9",
    "worker_debug_fragments",
    "worker_debug_descriptors",
):
    MIGRATION_9 += (
        f"""
        CREATE TRIGGER {_table}_no_update
        BEFORE UPDATE ON {_table}
        BEGIN
            SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
        END
        """,
        f"""
        CREATE TRIGGER {_table}_no_delete
        BEFORE DELETE ON {_table}
        BEGIN
            SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
        END
        """,
    )

MIGRATION_9_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_9).encode("utf-8")
).hexdigest()

CURRENT_DEBUG_TABLES = frozenset(
    {
        "current_schema_v9",
        "worker_debug_fragments",
        "worker_debug_descriptors",
    }
)


__all__ = [
    "CURRENT_DEBUG_TABLES",
    "MIGRATION_9",
    "MIGRATION_9_SHA256",
]
