from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
    CurrentDatabaseError,
)
from tests.test_agent_kernel_current_storage import _PackageRef


class CurrentAgentKernelDatabaseSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-agent-kernel-safety-")
        self.root = Path(self.scratch.name) / "current"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_sql_cannot_mutate_locks_or_skip_intent_state_machine(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        with database.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE current_package_lock SET package_version = 'changed'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM current_package_lock")
            _seed_intent(connection, task_id="task_one", capability="a" * 64)
            _seed_intent(connection, task_id="task_two", capability="b" * 64)

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE dispatch_intents SET invocation_digest = ? "
                    "WHERE task_id = ? AND idempotency_key = ?",
                    ("f" * 64, "task_one", "same-key"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE dispatch_intents SET state = 'SETTLED' "
                    "WHERE task_id = ? AND idempotency_key = ?",
                    ("task_one", "same-key"),
                )
            connection.execute(
                "UPDATE dispatch_intents SET state = 'DISPATCHED' "
                "WHERE task_id = ? AND idempotency_key = ?",
                ("task_one", "same-key"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE dispatch_intents SET state = 'INTENT' "
                    "WHERE task_id = ? AND idempotency_key = ?",
                    ("task_one", "same-key"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM dispatch_intents WHERE task_id = ?",
                    ("task_one",),
                )

def _seed_intent(
    connection: sqlite3.Connection, *, task_id: str, capability: str
) -> None:
    authority_digest = capability
    connection.execute(
        "INSERT INTO authority_history VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            authority_digest,
            "ACTIVE",
            task_id,
            b"authority",
            b"grant",
            "c" * 64,
            "test-current-package",
            "1.0.0",
            "a" * 64,
            "b" * 64,
            "attempt",
            "session",
            "owner",
            "grant",
            "lease",
            None,
            1,
            0,
            1,
            1,
            1,
            "ACTIVE",
            "ACTIVE",
            "RUN",
            None,
            None,
            1_000,
            2,
        ),
    )
    connection.execute(
        "INSERT INTO authority_heads VALUES (?, ?)",
        (task_id, authority_digest),
    )
    connection.execute(
        "INSERT INTO dispatch_intents VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "same-key",
            "d" * 64,
            f"intent_{task_id}",
            task_id,
            authority_digest,
            "c" * 64,
            "internal.read_source",
            "README.md",
            f"reservation_{task_id}",
            1_000,
            b"reservation",
            "e" * 64,
            b"intent",
            "f" * 64,
            capability,
            "INTENT",
            "2026-01-01T00:00:00.000000Z",
        ),
    )
if __name__ == "__main__":
    unittest.main()