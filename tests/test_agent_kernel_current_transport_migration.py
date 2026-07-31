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
    MIGRATION_8_SHA256,
)
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE


class CurrentTaskResultTransportMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s6-migration-")
        self.root = Path(self.scratch.name) / "current"
        self.root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_existing_v7_database_upgrades_once_without_rewriting_v7(self) -> None:
        path = self.root / "agent-kernel-current.sqlite3"
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
        )
        for statements, table, values in migrations:
            for statement in statements:
                connection.execute(statement)
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO {table} VALUES ({placeholders})", values
            )
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
        connection.close()

        database = CurrentAgentKernelDatabase.open(self.root, CURRENT_PACKAGE)
        connection = database.connect()
        try:
            self.assertEqual(
                8, connection.execute("PRAGMA user_version").fetchone()[0]
            )
            self.assertEqual(
                (7, MIGRATION_6_SHA256, MIGRATION_7_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version,previous_migration_sha256,"
                    "migration_sha256 FROM current_schema_v7"
                ).fetchone()),
            )
            self.assertEqual(
                (8, MIGRATION_7_SHA256, MIGRATION_8_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version,previous_migration_sha256,"
                    "migration_sha256 FROM current_schema_v8"
                ).fetchone()),
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_result_transport_envelopes"
                ).fetchone()[0],
            )
            self.assertEqual(
                [], connection.execute("PRAGMA foreign_key_check").fetchall()
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
