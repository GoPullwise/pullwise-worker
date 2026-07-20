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

    def _seed_v2(self) -> AgentKernelDatabase:
        database = self._seed_v1()
        connection = sqlite3.connect(database.path, isolation_level=None)
        try:
            database._configure(connection)
            connection.execute('BEGIN IMMEDIATE')
            database._apply_migration(connection, MIGRATIONS[1])
            connection.execute('PRAGMA user_version=2')
            connection.commit()
        finally:
            connection.close()
        return database

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _insert_task(connection: sqlite3.Connection, task_id: str) -> None:
        now = '2026-07-20T00:00:00.000Z'
        connection.execute(
            '''
            INSERT INTO tasks (
                task_id, task_type, request_ref, request_digest,
                policy_ref, policy_digest, policy_version, protocol_mode,
                lifecycle, desired_state, task_version, deletion_version,
                native_epoch, owner_id, owner_epoch, ledger_version,
                charter_version, current_checkpoint_generation, quality_risk,
                absolute_deadline_at, terminalization_reserve_ms,
                created_at, updated_at
            ) VALUES (
                ?, 'review', ?, ?, ?, ?, 1, 'legacy_v1',
                'QUEUED', 'RUN', 1, 0, 0, ?, 0, 0, 0, 0, 'Q1', ?, 0, ?, ?
            )
            ''',
            (
                task_id,
                f'request-{task_id}',
                '1' * 64,
                f'policy-{task_id}',
                '2' * 64,
                f'owner-{task_id}',
                now,
                now,
                now,
            ),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        task_id: str,
        idempotency_key: str,
    ) -> None:
        connection.execute(
            '''
            INSERT INTO task_events (
                task_id, event_seq, idempotency_key, event_type,
                task_version, event_digest, created_at
            ) VALUES (?, 1, ?, 'TASK_ACCEPTED', 1, ?, ?)
            ''',
            (
                task_id,
                idempotency_key,
                '3' * 64,
                '2026-07-20T00:00:00.000Z',
            ),
        )

    def test_task_event_idempotency_key_is_unique_across_tasks(self) -> None:
        database = AgentKernelDatabase(self.worker_root)
        database.initialize()

        with database.connect() as connection:
            self._insert_task(connection, 'task-one')
            self._insert_task(connection, 'task-two')
            self._insert_event(connection, 'task-one', 'shared-idempotency-key')

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                'task_events.idempotency_key',
            ):
                self._insert_event(connection, 'task-two', 'shared-idempotency-key')

    def test_v3_upgrade_rejects_duplicate_keys_and_rolls_back_to_v2(self) -> None:
        database = self._seed_v2()
        with database.connect() as connection:
            self._insert_task(connection, 'task-one')
            self._insert_task(connection, 'task-two')
            self._insert_event(connection, 'task-one', 'shared-idempotency-key')
            self._insert_event(connection, 'task-two', 'shared-idempotency-key')

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            'task_events.idempotency_key',
        ):
            database.initialize()

        with sqlite3.connect(database.path) as connection:
            self.assertEqual(
                2,
                connection.execute('PRAGMA user_version').fetchone()[0],
            )
            versions = connection.execute(
                'SELECT version FROM schema_migrations ORDER BY version'
            ).fetchall()
            self.assertEqual([(1,), (2,)], versions)
            self.assertIsNone(
                connection.execute(
                    '''
                    SELECT name FROM sqlite_master
                    WHERE type = 'index'
                      AND name = 'task_events_idempotency_key_unique'
                    '''
                ).fetchone()
            )

    def test_migration_one_and_two_digests_are_frozen(self) -> None:
        database = AgentKernelDatabase(self.worker_root)
        database.initialize()

        with database.connect() as connection:
            rows = connection.execute(
                '''
                SELECT version, name, sha256
                FROM schema_migrations
                WHERE version <= 2
                ORDER BY version
                '''
            ).fetchall()

        self.assertEqual(
            [
                (
                    1,
                    'initial-shadow-store',
                    'e4aee53eed365475cbe173cc0ed96d37d6bde094d4096a1eaae7b345661b18ef',
                ),
                (
                    2,
                    'slice-2-control-state',
                    '4983d15c962ea13e83f3d328a7923b36cdb6ee6f53178ba860315cc217c238be',
                ),
            ],
            [tuple(row) for row in rows],
        )

    def test_v1_database_upgrades_in_place_with_attempt_and_event_contract_fields(self) -> None:
        database = self._seed_v1()

        database.initialize()

        with database.connect() as connection:
            self.assertEqual(LATEST_SCHEMA_VERSION, connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            self.assertEqual(len(MIGRATIONS), connection.execute(
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

    def test_v3_upgrade_crash_rolls_back_then_restarts_once(self) -> None:
        database = self._seed_v2()

        def crash(stage: str) -> None:
            if stage == "before_migration_commit":
                raise InjectedMigrationCrash(stage)

        with self.assertRaisesRegex(InjectedMigrationCrash, "before_migration_commit"):
            AgentKernelDatabase(self.worker_root, stage_hook=crash).initialize()

        with sqlite3.connect(database.path) as connection:
            self.assertEqual(2, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0])
            self.assertIn("state_version", self._columns(connection, "attempts"))

        database.initialize()
        with database.connect() as connection:
            self.assertEqual(len(MIGRATIONS), connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0])
            self.assertIn("state_version", self._columns(connection, "attempts"))


if __name__ == "__main__":
    unittest.main()
