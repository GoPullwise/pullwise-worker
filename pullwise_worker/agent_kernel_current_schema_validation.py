from __future__ import annotations

import sqlite3

from .agent_kernel_current_migrations import (
    ACK_SCHEMA_VERSION,
    BASE_SCHEMA_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    CURRENT_SCHEMA_SHA256,
    CURRENT_SCHEMA_VERSION,
    CURRENT_TABLES,
    MIGRATION_1_SHA256,
    MIGRATION_2_SHA256,
    MIGRATION_3_SHA256,
    MIGRATION_4_SHA256,
    MIGRATION_5_SHA256,
    MIGRATION_6_SHA256,
    MIGRATION_7_SHA256,
    MIGRATION_8_SHA256,
    MIGRATION_9_SHA256,
    REQUIREMENT_SCHEMA_VERSION,
    RUNTIME_SCHEMA_VERSION,
    TERMINAL_COMMIT_SCHEMA_VERSION,
    TERMINALIZATION_SCHEMA_VERSION,
    TRANSPORT_SCHEMA_VERSION,
    schema_fingerprint,
)


def validate_current_schema(connection: sqlite3.Connection) -> str | None:
    if set(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ) != set(CURRENT_TABLES):
        return "table set"
    if schema_fingerprint(connection) != CURRENT_SCHEMA_SHA256:
        return "schema fingerprint"
    row = connection.execute(
        "SELECT schema_version, migration_sha256 FROM current_schema WHERE singleton = 1"
    ).fetchone()
    if row != (BASE_SCHEMA_VERSION, MIGRATION_1_SHA256):
        return "migration lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v2 WHERE singleton = 1"
    ).fetchone()
    if row != (
        RUNTIME_SCHEMA_VERSION,
        MIGRATION_1_SHA256,
        MIGRATION_2_SHA256,
    ):
        return "migration 2 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v3 WHERE singleton = 1"
    ).fetchone()
    if row != (
        CHECKPOINT_SCHEMA_VERSION,
        MIGRATION_2_SHA256,
        MIGRATION_3_SHA256,
    ):
        return "migration 3 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v4 WHERE singleton = 1"
    ).fetchone()
    if row != (ACK_SCHEMA_VERSION, MIGRATION_3_SHA256, MIGRATION_4_SHA256):
        return "migration 4 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v5 WHERE singleton = 1"
    ).fetchone()
    if row != (
        REQUIREMENT_SCHEMA_VERSION,
        MIGRATION_4_SHA256,
        MIGRATION_5_SHA256,
    ):
        return "migration 5 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v6 WHERE singleton = 1"
    ).fetchone()
    if row != (
        TERMINALIZATION_SCHEMA_VERSION,
        MIGRATION_5_SHA256,
        MIGRATION_6_SHA256,
    ):
        return "migration 6 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v7 WHERE singleton = 1"
    ).fetchone()
    if row != (
        TERMINAL_COMMIT_SCHEMA_VERSION,
        MIGRATION_6_SHA256,
        MIGRATION_7_SHA256,
    ):
        return "migration 7 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v8 WHERE singleton = 1"
    ).fetchone()
    if row != (
        TRANSPORT_SCHEMA_VERSION,
        MIGRATION_7_SHA256,
        MIGRATION_8_SHA256,
    ):
        return "migration 8 lock"
    row = connection.execute(
        "SELECT schema_version, previous_migration_sha256, migration_sha256 "
        "FROM current_schema_v9 WHERE singleton = 1"
    ).fetchone()
    if row != (
        CURRENT_SCHEMA_VERSION,
        MIGRATION_8_SHA256,
        MIGRATION_9_SHA256,
    ):
        return "migration 9 lock"
    return None


__all__ = ["validate_current_schema"]