from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from pullwise_worker.agent_kernel_database import (
    AgentKernelDatabase,
    LATEST_SCHEMA_VERSION,
)
from pullwise_worker.agent_kernel_migrations import MIGRATIONS


class InjectedMigrationCrash(RuntimeError):
    pass


class AgentKernelSlice2MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-v1-upgrade-")
        self.worker_root = Path(self.scratch.name) / "worker"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _seed_v1(self) -> AgentKernelDatabase:
        database = AgentKernelDatabase(self.worker_root)
        database.root.mkdir(parents=True, mode=0o700)
        connection = sqlite3.connect(database.path, isolation_level=None)
        try:
            database._configure(connection)
            connection.execute("BEGIN IMMEDIATE")
            database._apply_migration(connection, MIGRATIONS[0])
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()
        return database

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    def test_v1_database_upgrades_in_place_with_attempt_and_event_contract_fields(self) -> None:
        database = self._seed_v1()

        database.initialize()

        with database.connect() as connection:
            self.assertEqual(LATEST_SCHEMA_VERSION, connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0])
            self.assertTrue(
                {
                    "event_digest",
                }
                <= self._columns(connection, "task_events")
            )
            self.assertTrue(
                {
                    "transport_binding",
                    "state_version",
                    "predecessor_checkpoint_generation",
                    "owner_session_id",
                    "lease_acquired_at",
                    "budget_reservation_id",
                }
                <= self._columns(connection, "attempts")
            )
            self.assertIn("terminalization_reason", self._columns(connection, "tasks"))

    def test_v2_upgrade_crash_rolls_back_then_restarts_once(self) -> None:
        database = self._seed_v1()

        def crash(stage: str) -> None:
            if stage == "before_migration_commit":
                raise InjectedMigrationCrash(stage)

        with self.assertRaisesRegex(InjectedMigrationCrash, "before_migration_commit"):
            AgentKernelDatabase(self.worker_root, stage_hook=crash).initialize()

        with sqlite3.connect(database.path) as connection:
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0])
            self.assertNotIn("state_version", self._columns(connection, "attempts"))

        database.initialize()
        with database.connect() as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0])
            self.assertIn("state_version", self._columns(connection, "attempts"))


if __name__ == "__main__":
    unittest.main()
