"""Isolated SQLite root for the clean current Agent Kernel journal."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterator

from .agent_kernel_current_migrations import (
    CURRENT_SCHEMA_VERSION,
    CURRENT_TABLES,
    MIGRATION_1,
    MIGRATION_1_SHA256,
)


DEFAULT_BUSY_TIMEOUT_MS = 5_000
DATABASE_NAME = "agent-kernel-current.sqlite3"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
        if not all(DIGEST_RE.fullmatch(item) for item in package_tuple[2:]):
            raise CurrentDatabaseError("CURRENT_PACKAGE_LOCK_INVALID")
        if root.exists() and not self._is_private_directory(root):
            raise CurrentDatabaseError("CURRENT_DATABASE_ROOT_INVALID")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
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
        self._verify_database_file()
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_ms / 1_000,
        )
        try:
            connection.row_factory = sqlite3.Row
            self._configure(connection)
            self._verify_database_file()
            return connection
        except BaseException:
            connection.close()
            raise

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
        self._create_database_file()
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_ms / 1_000,
        )
        try:
            self._configure(connection)
            self._verify_database_file()
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

    def _create_database_file(self) -> None:
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
        self._verify_database_file()
        os.chmod(self.path, 0o600)

    def _verify_database_file(self) -> None:
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise CurrentDatabaseError("CURRENT_DATABASE_FILE_INVALID") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or self._is_reparse(info)
        ):
            raise CurrentDatabaseError("CURRENT_DATABASE_FILE_INVALID")

    def _configure(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        actual = (
            str(mode).lower(),
            connection.execute("PRAGMA foreign_keys").fetchone()[0],
            connection.execute("PRAGMA synchronous").fetchone()[0],
            connection.execute("PRAGMA busy_timeout").fetchone()[0],
        )
        if actual != ("wal", 1, 2, self.busy_timeout_ms):
            raise CurrentDatabaseError("CURRENT_DATABASE_PRAGMA_INVALID", repr(actual))

    @classmethod
    def _is_private_directory(cls, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and not cls._is_reparse(info)

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & marker)

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
