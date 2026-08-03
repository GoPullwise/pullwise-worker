from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
)
from pullwise_worker.agent_kernel_current_migrations import (
    MIGRATION_1,
    MIGRATION_1_SHA256,
    MIGRATION_2,
    MIGRATION_2_SHA256,
    MIGRATION_3,
    MIGRATION_3_SHA256,
    MIGRATION_4,
    MIGRATION_4_SHA256,
    MIGRATION_5,
    MIGRATION_5_SHA256,
    MIGRATION_6,
    MIGRATION_6_SHA256,
    MIGRATION_7,
    MIGRATION_7_SHA256,
    MIGRATION_8,
    MIGRATION_8_SHA256,
    MIGRATION_9_SHA256,
)
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE


class CurrentWorkerDebugMigrationTest(unittest.TestCase):
    def test_existing_v8_database_upgrades_once_without_rewriting_v8(self) -> None:
        with tempfile.TemporaryDirectory(prefix="current-s7-migration-") as scratch:
            root = Path(scratch) / "current"
            root.mkdir(mode=0o700)
            path = root / "agent-kernel-current.sqlite3"
            connection = sqlite3.connect(path)
            migrations = (
                (MIGRATION_1, "current_schema", (1, 1, MIGRATION_1_SHA256)),
                (
                    MIGRATION_2,
                    "current_schema_v2",
                    (1, 2, MIGRATION_1_SHA256, MIGRATION_2_SHA256),
                ),
                (
                    MIGRATION_3,
                    "current_schema_v3",
                    (1, 3, MIGRATION_2_SHA256, MIGRATION_3_SHA256),
                ),
                (
                    MIGRATION_4,
                    "current_schema_v4",
                    (1, 4, MIGRATION_3_SHA256, MIGRATION_4_SHA256),
                ),
                (
                    MIGRATION_5,
                    "current_schema_v5",
                    (1, 5, MIGRATION_4_SHA256, MIGRATION_5_SHA256),
                ),
                (
                    MIGRATION_6,
                    "current_schema_v6",
                    (1, 6, MIGRATION_5_SHA256, MIGRATION_6_SHA256),
                ),
                (
                    MIGRATION_7,
                    "current_schema_v7",
                    (1, 7, MIGRATION_6_SHA256, MIGRATION_7_SHA256),
                ),
                (
                    MIGRATION_8,
                    "current_schema_v8",
                    (1, 8, MIGRATION_7_SHA256, MIGRATION_8_SHA256),
                ),
            )
            for statements, table, values in migrations:
                for statement in statements:
                    connection.execute(statement)
                placeholders = ",".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO {table} VALUES ({placeholders})",
                    values,
                )
            connection.execute("PRAGMA user_version = 8")
            connection.commit()
            connection.close()

            database = CurrentAgentKernelDatabase.open(root, CURRENT_PACKAGE)
            connection = database.connect()
            try:
                self.assertEqual(
                    9,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
                self.assertEqual(
                    (8, MIGRATION_7_SHA256, MIGRATION_8_SHA256),
                    tuple(connection.execute(
                        "SELECT schema_version,previous_migration_sha256,"
                        "migration_sha256 FROM current_schema_v8"
                    ).fetchone()),
                )
                self.assertEqual(
                    (9, MIGRATION_8_SHA256, MIGRATION_9_SHA256),
                    tuple(connection.execute(
                        "SELECT schema_version,previous_migration_sha256,"
                        "migration_sha256 FROM current_schema_v9"
                    ).fetchone()),
                )
                self.assertEqual(
                    (0, 0),
                    (
                        connection.execute(
                            "SELECT COUNT(*) FROM worker_debug_fragments"
                        ).fetchone()[0],
                        connection.execute(
                            "SELECT COUNT(*) FROM worker_debug_descriptors"
                        ).fetchone()[0],
                    ),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE current_schema_v9 SET schema_version=9"
                    )
                self.assertEqual(
                    [],
                    connection.execute("PRAGMA foreign_key_check").fetchall(),
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
