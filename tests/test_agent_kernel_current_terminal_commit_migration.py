from __future__ import annotations

from contextlib import closing
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
)


class _PackageRef:
    def as_tuple(self) -> tuple[str, str, str, str]:
        return ("test-current-package", "1.0.0", "a" * 64, "b" * 64)


class CurrentTerminalCommitMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(
            prefix="current-terminal-migration-"
        )
        self.root = Path(self.scratch.name) / "current"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_populated_v6_runtime_head_upgrades_without_identity_drift(
        self,
    ) -> None:
        self.root.mkdir(mode=0o700)
        path = self.root / "agent-kernel-current.sqlite3"
        connection = sqlite3.connect(path)
        for statement in (
            MIGRATION_1
            + MIGRATION_2
            + MIGRATION_3
            + MIGRATION_4
            + MIGRATION_5
            + MIGRATION_6
        ):
            connection.execute(statement)
        locks = (
            ("current_schema", (1, 1, MIGRATION_1_SHA256)),
            (
                "current_schema_v2",
                (1, 2, MIGRATION_1_SHA256, MIGRATION_2_SHA256),
            ),
            (
                "current_schema_v3",
                (1, 3, MIGRATION_2_SHA256, MIGRATION_3_SHA256),
            ),
            (
                "current_schema_v4",
                (1, 4, MIGRATION_3_SHA256, MIGRATION_4_SHA256),
            ),
            (
                "current_schema_v5",
                (1, 5, MIGRATION_4_SHA256, MIGRATION_5_SHA256),
            ),
            (
                "current_schema_v6",
                (1, 6, MIGRATION_5_SHA256, MIGRATION_6_SHA256),
            ),
        )
        for table, values in locks:
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO {table} VALUES ({placeholders})", values
            )
        task_row = (
            "task_migration",
            2,
            "a" * 64,
            "BOOTSTRAP",
            "b" * 64,
            "ACTIVE",
            "RUN",
            "attempt_migration",
            1,
            1,
            0,
            None,
            b"preserved-runtime-task-record",
        )
        connection.execute(
            "INSERT INTO runtime_task_records VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            task_row,
        )
        connection.execute(
            "INSERT INTO runtime_task_heads VALUES (?,?,?)",
            (task_row[0], task_row[1], task_row[2]),
        )
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
        connection.close()

        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        with closing(database.connect()) as upgraded:
            self.assertEqual(
                task_row,
                tuple(
                    upgraded.execute(
                        "SELECT * FROM runtime_task_records"
                    ).fetchone()
                ),
            )
            self.assertEqual(
                task_row[:3],
                tuple(
                    upgraded.execute(
                        "SELECT * FROM runtime_task_heads"
                    ).fetchone()
                ),
            )
            self.assertEqual(
                [],
                upgraded.execute("PRAGMA foreign_key_check").fetchall(),
            )
            table_sql = upgraded.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='runtime_task_records'"
            ).fetchone()[0]
            self.assertIn("'TERMINALIZATION'", table_sql)


if __name__ == "__main__":
    unittest.main()
