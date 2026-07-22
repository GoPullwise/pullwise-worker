"""Isolated SQLite root for the clean current Agent Kernel journal."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .agent_kernel_current_migrations import (
    CURRENT_SCHEMA_VERSION,
    CURRENT_TABLES,
    MIGRATION_1,
    MIGRATION_1_SHA256,
)


DEFAULT_BUSY_TIMEOUT_MS = 5_000
DATABASE_NAME = "agent-kernel-current.sqlite3"


class CurrentDatabaseError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class CurrentAgentKernelDatabase:
    def __init__(
        self,
        root: Path,
        package_tuple: tuple[str, str, str, str],
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if (
            not isinstance(root, Path)
            or isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 1_000
        ):
            raise CurrentDatabaseError("CURRENT_DATABASE_CONFIG_INVALID")
        if len(package_tuple) != 4 or any(
            not isinstance(item, str) or not item for item in package_tuple
        ):
            raise CurrentDatabaseError("CURRENT_PACKAGE_LOCK_INVALID")
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise CurrentDatabaseError("CURRENT_DATABASE_ROOT_INVALID")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.path = root / DATABASE_NAME
        self.package_tuple = package_tuple
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    @classmethod
    def open(
        cls,
        root: Path,
        package_ref: object,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> "CurrentAgentKernelDatabase":
        try:
            package_tuple = package_ref.as_tuple()
        except (AttributeError, TypeError) as exc:
            raise CurrentDatabaseError("CURRENT_PACKAGE_LOCK_INVALID") from exc
        if not isinstance(package_tuple, tuple):
            raise CurrentDatabaseError("CURRENT_PACKAGE_LOCK_INVALID")
        return cls(root, package_tuple, busy_timeout_ms=busy_timeout_ms)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_ms / 1_000,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise CurrentDatabaseError("CURRENT_DATABASE_WAL_REQUIRED")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = self._table_names(connection)
            if version == 0 and not tables:
                for statement in MIGRATION_1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO current_schema VALUES (1, ?, ?)",
                    (CURRENT_SCHEMA_VERSION, MIGRATION_1_SHA256),
                )
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            elif version != CURRENT_SCHEMA_VERSION:
                raise CurrentDatabaseError("CURRENT_SCHEMA_UNKNOWN", str(version))
            self._validate_schema(connection)
            self._lock_package(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        return {row[0] for row in rows}

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        if self._table_names(connection) != set(CURRENT_TABLES):
            raise CurrentDatabaseError("CURRENT_SCHEMA_UNKNOWN", "table set")
        row = connection.execute(
            "SELECT schema_version, migration_sha256 FROM current_schema WHERE singleton = 1"
        ).fetchone()
        if row != (CURRENT_SCHEMA_VERSION, MIGRATION_1_SHA256):
            raise CurrentDatabaseError("CURRENT_SCHEMA_UNKNOWN", "migration lock")

    def _lock_package(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT package_identity, package_version, content_sha256, root_sha256 "
            "FROM current_package_lock WHERE singleton = 1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO current_package_lock VALUES (1, ?, ?, ?, ?)",
                self.package_tuple,
            )
        elif tuple(row) != self.package_tuple:
            raise CurrentDatabaseError("CURRENT_PACKAGE_LOCK_MISMATCH")


__all__ = ["CurrentAgentKernelDatabase", "CurrentDatabaseError"]
