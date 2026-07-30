"""Append-only migration for one immutable terminalization candidate."""

from __future__ import annotations

import hashlib


MIGRATION_6 = (
    """
    CREATE TABLE current_schema_v6 (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 6),
        previous_migration_sha256 TEXT NOT NULL
            CHECK (length(previous_migration_sha256) = 64),
        migration_sha256 TEXT NOT NULL CHECK (length(migration_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE terminalization_candidates (
        result_digest TEXT PRIMARY KEY CHECK (length(result_digest) = 64),
        result_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL UNIQUE,
        outcome TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        published_from_version INTEGER NOT NULL
            CHECK (published_from_version >= 1),
        terminal_task_version INTEGER NOT NULL
            CHECK (terminal_task_version = published_from_version + 1),
        selector_input_digest TEXT NOT NULL
            CHECK (length(selector_input_digest) = 64),
        input_snapshot_sha256 TEXT NOT NULL
            CHECK (length(input_snapshot_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        root_set_sha256 TEXT NOT NULL CHECK (length(root_set_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        pre_gate_closure_sha256 TEXT NOT NULL
            CHECK (length(pre_gate_closure_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        gate_decision_sha256 TEXT NOT NULL
            CHECK (length(gate_decision_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        evidence_closure_sha256 TEXT NOT NULL
            CHECK (length(evidence_closure_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        effect_ledger_sha256 TEXT NOT NULL
            CHECK (length(effect_ledger_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        budget_summary_sha256 TEXT NOT NULL
            CHECK (length(budget_summary_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        task_result_core_sha256 TEXT NOT NULL
            CHECK (length(task_result_core_sha256) = 64)
            REFERENCES checkpoint_objects(sha256),
        frozen_at TEXT NOT NULL,
        UNIQUE(task_id, published_from_version)
    ) STRICT
    """,
    """
    CREATE TRIGGER current_schema_v6_no_update
    BEFORE UPDATE ON current_schema_v6
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER current_schema_v6_no_delete
    BEFORE DELETE ON current_schema_v6
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER terminalization_candidates_no_update
    BEFORE UPDATE ON terminalization_candidates
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_UPDATE_FORBIDDEN');
    END
    """,
    """
    CREATE TRIGGER terminalization_candidates_no_delete
    BEFORE DELETE ON terminalization_candidates
    BEGIN
        SELECT RAISE(ABORT, 'CURRENT_IMMUTABLE_DELETE_FORBIDDEN');
    END
    """,
)

MIGRATION_6_SHA256 = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_6).encode("utf-8")
).hexdigest()

CURRENT_TERMINALIZATION_TABLES = frozenset(
    {"current_schema_v6", "terminalization_candidates"}
)


__all__ = [
    "CURRENT_TERMINALIZATION_TABLES",
    "MIGRATION_6",
    "MIGRATION_6_SHA256",
]
