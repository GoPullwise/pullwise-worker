from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable


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


def expected_schema_fingerprint(statements: Iterable[str]) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            connection.execute(statement)
        return schema_fingerprint(connection)
    finally:
        connection.close()


__all__ = ["expected_schema_fingerprint", "schema_fingerprint"]
