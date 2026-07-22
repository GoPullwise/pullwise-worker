"""Schema owned only by the current Agent Kernel dispatch journal."""

from __future__ import annotations

import hashlib
import sqlite3


CURRENT_SCHEMA_VERSION = 1

MIGRATION_1 = (
    """
    CREATE TABLE current_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE current_package_lock (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
        root_sha256 TEXT NOT NULL CHECK (length(root_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE authority_history (
        authority_digest TEXT PRIMARY KEY CHECK (length(authority_digest) = 64),
        task_id TEXT NOT NULL,
        authority_bytes BLOB NOT NULL,
        grant_bytes BLOB NOT NULL,
        grant_digest TEXT NOT NULL CHECK (length(grant_digest) = 64),
        package_identity TEXT NOT NULL,
        package_version TEXT NOT NULL,
        package_content_sha256 TEXT NOT NULL CHECK (length(package_content_sha256) = 64),
        package_root_sha256 TEXT NOT NULL CHECK (length(package_root_sha256) = 64),
        attempt_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        task_version INTEGER NOT NULL CHECK (task_version >= 1),
        deletion_version INTEGER NOT NULL CHECK (deletion_version >= 0),
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        transport_epoch INTEGER NOT NULL CHECK (transport_epoch >= 1),
        lifecycle TEXT NOT NULL,
        desired_state TEXT NOT NULL,
        elapsed_limit_ms INTEGER NOT NULL CHECK (elapsed_limit_ms >= 1),
        tool_call_limit INTEGER NOT NULL CHECK (tool_call_limit >= 1)
    ) STRICT
    """,
    """
    CREATE TABLE authority_heads (
        task_id TEXT PRIMARY KEY,
        authority_digest TEXT NOT NULL UNIQUE
            REFERENCES authority_history(authority_digest)
    ) STRICT
    """,
    """
    CREATE TABLE dispatch_budgets (
        task_id TEXT NOT NULL REFERENCES authority_heads(task_id),
        grant_digest TEXT NOT NULL,
        elapsed_limit_ms INTEGER NOT NULL CHECK (elapsed_limit_ms >= 1),
        consumed_ms INTEGER NOT NULL DEFAULT 0 CHECK (consumed_ms >= 0),
        reserved_ms INTEGER NOT NULL DEFAULT 0 CHECK (reserved_ms >= 0),
        tool_call_limit INTEGER NOT NULL CHECK (tool_call_limit >= 1),
        calls_consumed INTEGER NOT NULL DEFAULT 0 CHECK (calls_consumed >= 0),
        calls_reserved INTEGER NOT NULL DEFAULT 0 CHECK (calls_reserved >= 0),
        PRIMARY KEY (task_id, grant_digest),
        CHECK (consumed_ms + reserved_ms <= elapsed_limit_ms),
        CHECK (calls_consumed + calls_reserved <= tool_call_limit)
    ) STRICT
    """,
    """
    CREATE TABLE dispatch_intents (
        idempotency_key TEXT NOT NULL,
        invocation_digest TEXT NOT NULL CHECK (length(invocation_digest) = 64),
        intent_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL REFERENCES authority_heads(task_id),
        authority_digest TEXT NOT NULL REFERENCES authority_history(authority_digest),
        grant_digest TEXT NOT NULL CHECK (length(grant_digest) = 64),
        tool_key TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        reservation_id TEXT NOT NULL,
        reserved_ms INTEGER NOT NULL CHECK (reserved_ms >= 1),
        reservation_bytes BLOB NOT NULL,
        reservation_digest TEXT NOT NULL CHECK (length(reservation_digest) = 64),
        intent_bytes BLOB NOT NULL,
        intent_digest TEXT NOT NULL CHECK (length(intent_digest) = 64),
        capability_sha256 TEXT NOT NULL UNIQUE CHECK (length(capability_sha256) = 64),
        state TEXT NOT NULL CHECK (state IN ('INTENT','DISPATCHED','SETTLED','ABANDONED')),
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, idempotency_key)
    ) STRICT
    """,
    """
    CREATE TABLE content_objects (
        sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        relative_path TEXT NOT NULL UNIQUE
    ) STRICT
    """,
    """
    CREATE TABLE content_bindings (
        intent_id TEXT NOT NULL REFERENCES dispatch_intents(intent_id),
        artifact_id TEXT NOT NULL,
        content_schema_id TEXT NOT NULL,
        sha256 TEXT NOT NULL REFERENCES content_objects(sha256),
        content_ref_bytes BLOB NOT NULL,
        PRIMARY KEY (intent_id, artifact_id)
    ) STRICT
    """,
    """
    CREATE TABLE dispatch_settlements (
        intent_id TEXT PRIMARY KEY REFERENCES dispatch_intents(intent_id),
        receipt_bytes BLOB NOT NULL,
        receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
        outcome_bytes BLOB NOT NULL,
        outcome_sha256 TEXT NOT NULL CHECK (length(outcome_sha256) = 64),
        outcome_schema_id TEXT NOT NULL,
        observation_bytes BLOB NOT NULL,
        observation_digest TEXT NOT NULL CHECK (length(observation_digest) = 64),
        budget_settlement_bytes BLOB NOT NULL,
        budget_settlement_digest TEXT NOT NULL CHECK (length(budget_settlement_digest) = 64),
        replay_bytes BLOB NOT NULL,
        replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
        violation_bytes BLOB,
        CHECK (length(outcome_bytes) >= 2)
    ) STRICT
    """,
    """
    CREATE TABLE dispatch_abandonments (
        intent_id TEXT PRIMARY KEY REFERENCES dispatch_intents(intent_id),
        abandonment_bytes BLOB NOT NULL,
        abandonment_sha256 TEXT NOT NULL CHECK (length(abandonment_sha256) = 64),
        budget_settlement_bytes BLOB NOT NULL,
        budget_settlement_digest TEXT NOT NULL CHECK (length(budget_settlement_digest) = 64),
        replay_bytes BLOB NOT NULL,
        replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64)
    ) STRICT
    """,
    "CREATE INDEX dispatch_intents_task_state ON dispatch_intents(task_id, state)",
    """
    CREATE TRIGGER authority_heads_no_delete
    BEFORE DELETE ON authority_heads
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_AUTHORITY_HEAD_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER authority_heads_update_guard
    BEFORE UPDATE ON authority_heads
    WHEN NEW.task_id IS NOT OLD.task_id
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_AUTHORITY_HEAD_UPDATE_INVALID');
    END
    """,
    """
    CREATE TRIGGER dispatch_intents_no_delete
    BEFORE DELETE ON dispatch_intents
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_INTENT_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER dispatch_intents_update_guard
    BEFORE UPDATE ON dispatch_intents
    WHEN NOT (
        NEW.idempotency_key IS OLD.idempotency_key
        AND NEW.invocation_digest IS OLD.invocation_digest
        AND NEW.intent_id IS OLD.intent_id
        AND NEW.task_id IS OLD.task_id
        AND NEW.authority_digest IS OLD.authority_digest
        AND NEW.grant_digest IS OLD.grant_digest
        AND NEW.tool_key IS OLD.tool_key
        AND NEW.relative_path IS OLD.relative_path
        AND NEW.reservation_id IS OLD.reservation_id
        AND NEW.reserved_ms IS OLD.reserved_ms
        AND NEW.reservation_bytes IS OLD.reservation_bytes
        AND NEW.reservation_digest IS OLD.reservation_digest
        AND NEW.intent_bytes IS OLD.intent_bytes
        AND NEW.intent_digest IS OLD.intent_digest
        AND NEW.capability_sha256 IS OLD.capability_sha256
        AND NEW.created_at IS OLD.created_at
        AND (
            (OLD.state = 'INTENT' AND NEW.state IN ('DISPATCHED', 'ABANDONED'))
            OR (OLD.state = 'DISPATCHED' AND NEW.state = 'SETTLED')
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_INTENT_TRANSITION_INVALID');
    END
    """,
)

_IMMUTABLE_TABLES = (
    "current_schema",
    "current_package_lock",
    "authority_history",
    "content_objects",
    "content_bindings",
    "dispatch_settlements",
    "dispatch_abandonments",
)

MIGRATION_1 += tuple(
    f"""
    CREATE TRIGGER {table}_no_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """
    for table in _IMMUTABLE_TABLES
) + tuple(
    f"""
    CREATE TRIGGER {table}_no_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """
    for table in _IMMUTABLE_TABLES
)

MIGRATION_1_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_1).encode("utf-8")
).hexdigest()


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )
    inventory = "\n".join(
        "\x1f".join((kind, name, table, " ".join((sql or "").split())))
        for kind, name, table, sql in rows
    )
    return hashlib.sha256(inventory.encode("utf-8")).hexdigest()


def _expected_schema_fingerprint() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in MIGRATION_1:
            connection.execute(statement)
        return schema_fingerprint(connection)
    finally:
        connection.close()


MIGRATION_1_SCHEMA_SHA256 = _expected_schema_fingerprint()

CURRENT_TABLES = frozenset(
    {
        "current_schema",
        "current_package_lock",
        "authority_history",
        "authority_heads",
        "dispatch_budgets",
        "dispatch_intents",
        "content_objects",
        "content_bindings",
        "dispatch_settlements",
        "dispatch_abandonments",
    }
)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "CURRENT_TABLES",
    "MIGRATION_1",
    "MIGRATION_1_SHA256",
    "MIGRATION_1_SCHEMA_SHA256",
    "schema_fingerprint",
]
