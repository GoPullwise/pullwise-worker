"""SQLite WAL lifecycle for the Agent Kernel shadow store."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import stat
from typing import Callable, Iterator

from .agent_kernel_migrations import MIGRATIONS, Migration


LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version
REQUIRED_TABLES = {
    "schema_migrations",
    "tasks",
    "task_events",
    "attempts",
    "owner_incarnations",
    "agent_sessions",
    "requirements",
    "requirement_events",
    "interactions",
    "budget_entries",
    "observations",
    "checkpoint_index",
    "verifier_slots",
    "gate_decisions",
    "result_publications",
    "content_objects",
    "content_bindings",
}


class AgentKernelStorageError(RuntimeError):
    pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _migration_digest(migration: Migration) -> str:
    material = "\0".join(
        (str(migration.version), migration.name, *migration.statements)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            pass
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentKernelStorageError(f"storage_directory_invalid: {path}")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise AgentKernelStorageError(f"storage_directory_permissions: {path}") from exc


def _ensure_private_database_file(path: Path, *, allow_missing: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        if allow_missing:
            return
        raise AgentKernelStorageError("database_path_invalid: missing") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise AgentKernelStorageError("database_path_invalid: not private regular file")
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise AgentKernelStorageError("database_permissions_failed") from exc


class AgentKernelDatabase:
    def __init__(
        self,
        worker_root: Path,
        *,
        stage_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.worker_root = Path(worker_root)
        self.root = self.worker_root / "agent-kernel"
        self.path = self.root / "state.sqlite3"
        self.stage_hook = stage_hook

    def initialize(self) -> None:
        _ensure_private_directory(self.worker_root)
        _ensure_private_directory(self.root)
        _ensure_private_database_file(self.path, allow_missing=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            self._configure(connection)
            self._migrate(connection)
            self._validate_schema(connection)
        finally:
            connection.close()
        _ensure_private_database_file(self.path, allow_missing=False)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        _ensure_private_database_file(self.path, allow_missing=False)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection)
            yield connection
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        observed = {
            "journal_mode": str(journal_mode).lower(),
            "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
        }
        expected = {
            "journal_mode": "wal",
            "foreign_keys": 1,
            "synchronous": 2,
            "busy_timeout": 5000,
        }
        if observed != expected:
            raise AgentKernelStorageError(f"sqlite_pragma_mismatch: {observed}")

    def _migrate(self, connection: sqlite3.Connection) -> None:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if user_version > LATEST_SCHEMA_VERSION:
            raise AgentKernelStorageError(
                f"schema_version_unsupported: {user_version}>{LATEST_SCHEMA_VERSION}"
            )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        has_history = "schema_migrations" in tables
        if user_version and not has_history:
            raise AgentKernelStorageError("schema_migration_history_missing")
        connection.execute("BEGIN IMMEDIATE")
        try:
            changed = False
            if not has_history:
                if user_version != 0:
                    raise AgentKernelStorageError("schema_migration_state_invalid")
                self._apply_migration(connection, MIGRATIONS[0])
                changed = True
                start = 1
            else:
                start = 0
            applied = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT version, name, sha256 FROM schema_migrations"
                )
            }
            unknown = sorted(set(applied) - {item.version for item in MIGRATIONS})
            if unknown:
                raise AgentKernelStorageError(
                    f"schema_version_unsupported: history={unknown}"
                )
            for migration in MIGRATIONS:
                expected = (migration.name, _migration_digest(migration))
                if migration.version in applied:
                    if applied[migration.version] != expected:
                        raise AgentKernelStorageError(
                            f"schema_migration_digest_mismatch: {migration.version}"
                        )
                    continue
                if migration.version < start + 1:
                    raise AgentKernelStorageError("schema_migration_history_invalid")
                self._apply_migration(connection, migration)
                changed = True
            connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
            if changed:
                self._stage("before_migration_commit")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _apply_migration(
        connection: sqlite3.Connection, migration: Migration
    ) -> None:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version,name,sha256,applied_at) VALUES(?,?,?,?)",
            (
                migration.version,
                migration.name,
                _migration_digest(migration),
                _timestamp(),
            ),
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise AgentKernelStorageError(f"schema_tables_missing: {missing}")

    def _stage(self, name: str) -> None:
        if self.stage_hook is not None:
            self.stage_hook(name)
