"""Schema owned only by the current Agent Kernel dispatch journal."""

from __future__ import annotations

import hashlib


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
    CREATE TABLE tool_call_budgets (
        task_id TEXT NOT NULL REFERENCES authority_heads(task_id),
        grant_digest TEXT NOT NULL,
        hard_limit INTEGER NOT NULL CHECK (hard_limit >= 1),
        consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
        active_reserved INTEGER NOT NULL DEFAULT 0 CHECK (active_reserved >= 0),
        PRIMARY KEY (task_id, grant_digest),
        CHECK (consumed + active_reserved <= hard_limit)
    ) STRICT
    """,
    """
    CREATE TABLE dispatch_intents (
        idempotency_key TEXT PRIMARY KEY,
        invocation_digest TEXT NOT NULL CHECK (length(invocation_digest) = 64),
        intent_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL REFERENCES authority_heads(task_id),
        authority_digest TEXT NOT NULL REFERENCES authority_history(authority_digest),
        grant_digest TEXT NOT NULL CHECK (length(grant_digest) = 64),
        reservation_id TEXT NOT NULL,
        reservation_bytes BLOB NOT NULL,
        reservation_digest TEXT NOT NULL CHECK (length(reservation_digest) = 64),
        intent_bytes BLOB NOT NULL,
        intent_digest TEXT NOT NULL CHECK (length(intent_digest) = 64),
        capability_sha256 TEXT NOT NULL UNIQUE CHECK (length(capability_sha256) = 64),
        state TEXT NOT NULL CHECK (state IN ('INTENT','DISPATCHED','SETTLED','ABANDONED')),
        created_at TEXT NOT NULL
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
        result_bytes BLOB,
        result_digest TEXT,
        observation_bytes BLOB NOT NULL,
        observation_digest TEXT NOT NULL CHECK (length(observation_digest) = 64),
        budget_settlement_bytes BLOB NOT NULL,
        budget_settlement_digest TEXT NOT NULL CHECK (length(budget_settlement_digest) = 64),
        replay_bytes BLOB NOT NULL,
        replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
        violation_bytes BLOB,
        CHECK ((result_bytes IS NULL) = (result_digest IS NULL)),
        CHECK (result_digest IS NULL OR length(result_digest) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE dispatch_abandonments (
        intent_id TEXT PRIMARY KEY REFERENCES dispatch_intents(intent_id),
        abandonment_bytes BLOB NOT NULL,
        abandonment_sha256 TEXT NOT NULL CHECK (length(abandonment_sha256) = 64),
        replay_bytes BLOB NOT NULL,
        replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64)
    ) STRICT
    """,
    "CREATE INDEX dispatch_intents_task_state ON dispatch_intents(task_id, state)",
)

MIGRATION_1_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_1).encode("utf-8")
).hexdigest()

CURRENT_TABLES = frozenset(
    {
        "current_schema",
        "current_package_lock",
        "authority_history",
        "authority_heads",
        "tool_call_budgets",
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
]
