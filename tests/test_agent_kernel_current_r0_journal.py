from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
    CurrentDatabaseError,
)


class _PackageRef:
    def __init__(self, suffix: str = "a") -> None:
        self.package_identity = "test-current-package"
        self.package_version = "1.0.0"
        self.content_sha256 = suffix * 64
        self.root_sha256 = "b" * 64

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (
            self.package_identity,
            self.package_version,
            self.content_sha256,
            self.root_sha256,
        )


class CurrentAgentKernelDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-agent-kernel-")
        self.root = Path(self.scratch.name) / "current"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_new_root_has_strict_sqlite_configuration_and_migration_one(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())

        self.assertEqual(self.root / "agent-kernel-current.sqlite3", database.path)
        self.assertNotIn("shadow", database.path.as_posix())
        with database.connect() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(2, connection.execute("PRAGMA synchronous").fetchone()[0])
            self.assertGreaterEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0], 1_000
            )
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_reopen_fails_closed_on_package_lock_mismatch(self) -> None:
        CurrentAgentKernelDatabase.open(self.root, _PackageRef("a"))

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_PACKAGE_LOCK_MISMATCH"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef("c"))

    def test_reopen_fails_closed_on_unknown_schema_version(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        connection = sqlite3.connect(database.path)
        connection.execute("PRAGMA user_version = 99")
        connection.close()

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_SCHEMA_UNKNOWN"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())


if __name__ == "__main__":
    unittest.main()
