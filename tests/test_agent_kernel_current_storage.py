from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
    CurrentDatabaseError,
)
from pullwise_worker.agent_kernel_current_migrations import (
    CURRENT_SCHEMA_SHA256,
    MIGRATION_1,
    MIGRATION_1_SCHEMA_SHA256,
    MIGRATION_1_SHA256,
    MIGRATION_2,
    MIGRATION_2_SHA256,
    MIGRATION_3,
    MIGRATION_3_SHA256,
    MIGRATION_4_SHA256,
    MIGRATION_5_SHA256,
    MIGRATION_6_SHA256,
    MIGRATION_7_SHA256,
    MIGRATION_8_SHA256,
    MIGRATION_9_SHA256,
)
from pullwise_worker.agent_kernel_current_objects import (
    CurrentObjectError,
    CurrentObjectStore,
    PublishedCurrentObject,
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

    def test_new_root_has_strict_sqlite_configuration_and_append_only_migrations(
        self,
    ) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())

        self.assertEqual(self.root / "agent-kernel-current.sqlite3", database.path)
        self.assertNotIn("shadow", database.path.as_posix())
        with closing(database.connect()) as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(2, connection.execute("PRAGMA synchronous").fetchone()[0])
            self.assertGreaterEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0], 1_000
            )
            self.assertEqual(9, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                (1, MIGRATION_1_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, migration_sha256 FROM current_schema"
                ).fetchone()),
            )
            self.assertEqual(
                (2, MIGRATION_1_SHA256, MIGRATION_2_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v2"
                ).fetchone()),
            )
            self.assertEqual(
                (3, MIGRATION_2_SHA256, MIGRATION_3_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v3"
                ).fetchone()),
            )
            self.assertEqual(
                (4, MIGRATION_3_SHA256, MIGRATION_4_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v4"
                ).fetchone()),
            )
            self.assertEqual(
                (5, MIGRATION_4_SHA256, MIGRATION_5_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v5"
                ).fetchone()),
            )
            self.assertEqual(
                (6, MIGRATION_5_SHA256, MIGRATION_6_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v6"
                ).fetchone()),
            )
            self.assertEqual(
                (7, MIGRATION_6_SHA256, MIGRATION_7_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v7"
                ).fetchone()),
            )
            self.assertEqual(
                (8, MIGRATION_7_SHA256, MIGRATION_8_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v8"
                ).fetchone()),
            )
            self.assertEqual(
                (9, MIGRATION_8_SHA256, MIGRATION_9_SHA256),
                tuple(connection.execute(
                    "SELECT schema_version, previous_migration_sha256, "
                    "migration_sha256 FROM current_schema_v9"
                ).fetchone()),
            )
        if os.name == "posix":
            self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(database.path.stat().st_mode))

    def test_migration_one_bytes_and_schema_digest_remain_frozen(self) -> None:
        self.assertEqual(
            "48b777f8938969239636aade3e0e6fef74229e3332491a28b2ff0dcf74363e6f",
            MIGRATION_1_SHA256,
        )
        self.assertEqual(
            "fc08a2896a328b7e7aeff52bdd61d8a09867fea521a1c07c85e250d0b3b61d08",
            MIGRATION_1_SCHEMA_SHA256,
        )
        self.assertEqual(
            "c764e6b8723eeb4c6bb5e418262ff883aec680e5b2c87052cc8fdef5ae3fc5ae",
            MIGRATION_2_SHA256,
        )
        self.assertEqual(
            "512a8297f3999c9db170f662d8e746c8c680ea8802774c00fe84f07c2b0d67af",
            MIGRATION_3_SHA256,
        )
        self.assertEqual(
            "d7d46ed4447b3a7b00391e4fcab742aa88490c23df942f576bd57fa464a6ac18",
            MIGRATION_4_SHA256,
        )
        self.assertEqual(
            "c64f1e2e2432feed362ac081a2cfecabf210f0ec92d5d8f5ffab085fe711501f",
            MIGRATION_5_SHA256,
        )
        self.assertEqual(
            "cddd53b5e921a4de8b9198b5032dc0af6d263712e5de2737b408cee0cfd123e4",
            MIGRATION_6_SHA256,
        )
        self.assertEqual(
            "78e91b908faa55aedc2c96d295b91ff11892d98174ab60899c51411b9905af0e",
            MIGRATION_7_SHA256,
        )
        self.assertEqual(
            "338d7e618296fba6e1f3e0b39812eb5b1db9a96031e6b15978be251922912b7e",
            MIGRATION_8_SHA256,
        )
        self.assertEqual(
            "e064965cb1bb6e157e18b9d821275b7cddc23d3a3808408d1909164220796ac2",
            MIGRATION_9_SHA256,
        )
        self.assertEqual(
            "028cc25005ce33dd7b16017fe7e5324774205b0b603f2e2582e9930511065e6a",
            CURRENT_SCHEMA_SHA256,
        )

    def test_existing_migration_one_database_upgrades_in_place_once(self) -> None:
        self.root.mkdir(mode=0o700)
        path = self.root / "agent-kernel-current.sqlite3"
        connection = sqlite3.connect(path)
        for statement in MIGRATION_1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO current_schema VALUES (1, 1, ?)",
            (MIGRATION_1_SHA256,),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()

        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        with closing(database.connect()) as upgraded:
            self.assertEqual(9, upgraded.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v2").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v3").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v4").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v5").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v6").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v7").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v8").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v9").fetchone()[0],
            )

    def test_existing_checkpoint_schema_upgrades_to_ack_evidence_once(self) -> None:
        self.root.mkdir(mode=0o700)
        path = self.root / "agent-kernel-current.sqlite3"
        connection = sqlite3.connect(path)
        for statement in MIGRATION_1 + MIGRATION_2 + MIGRATION_3:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO current_schema VALUES (1, 1, ?)",
            (MIGRATION_1_SHA256,),
        )
        connection.execute(
            "INSERT INTO current_schema_v2 VALUES (1, 2, ?, ?)",
            (MIGRATION_1_SHA256, MIGRATION_2_SHA256),
        )
        connection.execute(
            "INSERT INTO current_schema_v3 VALUES (1, 3, ?, ?)",
            (MIGRATION_2_SHA256, MIGRATION_3_SHA256),
        )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
        connection.close()

        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        with closing(database.connect()) as upgraded:
            self.assertEqual(9, upgraded.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v4").fetchone()[0],
            )
            self.assertEqual(
                0,
                upgraded.execute(
                    "SELECT COUNT(*) FROM checkpoint_server_ack_documents"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v5").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v6").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v7").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v8").fetchone()[0],
            )
            self.assertEqual(
                1,
                upgraded.execute("SELECT COUNT(*) FROM current_schema_v9").fetchone()[0],
            )

    def test_package_lock_rejects_non_lowercase_digest(self) -> None:
        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_PACKAGE_LOCK_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef("A"))

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

    def test_reopen_rejects_hardlinked_database_file(self) -> None:
        database = CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        os.link(database.path, self.root / "unexpected-hardlink.sqlite3")

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_DATABASE_FILE_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())

    def test_root_symlink_is_rejected_when_platform_can_create_one(self) -> None:
        target = Path(self.scratch.name) / "target"
        target.mkdir()
        try:
            self.root.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_DATABASE_ROOT_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())

    def test_dangling_database_symlink_is_rejected_without_following(self) -> None:
        self.root.mkdir()
        database_path = self.root / "agent-kernel-current.sqlite3"
        missing_target = Path(self.scratch.name) / "must-not-be-created.sqlite3"
        try:
            database_path.symlink_to(missing_target)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentDatabaseError, "CURRENT_DATABASE_FILE_INVALID"):
            CurrentAgentKernelDatabase.open(self.root, _PackageRef())
        self.assertFalse(missing_target.exists())

    def test_reopen_rejects_any_schema_inventory_change(self) -> None:
        mutations = {
            "dropped index": "DROP INDEX dispatch_intents_task_state",
            "replaced trigger": (
                "DROP TRIGGER current_package_lock_no_update; "
                "CREATE TRIGGER current_package_lock_no_update "
                "BEFORE UPDATE ON current_package_lock BEGIN "
                "SELECT RAISE(ABORT, 'different'); END"
            ),
            "renamed column": (
                "ALTER TABLE current_package_lock "
                "RENAME COLUMN package_version TO package_release"
            ),
        }
        for index, (label, script) in enumerate(mutations.items()):
            with self.subTest(label=label):
                root = Path(self.scratch.name) / f"schema-{index}"
                database = CurrentAgentKernelDatabase.open(root, _PackageRef())
                connection = sqlite3.connect(database.path)
                connection.executescript(script)
                connection.close()

                with self.assertRaisesRegex(
                    CurrentDatabaseError, "CURRENT_SCHEMA_UNKNOWN"
                ):
                    CurrentAgentKernelDatabase.open(root, _PackageRef())

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


class CurrentObjectStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-object-store-")
        self.root = Path(self.scratch.name) / "content"
        self.store = CurrentObjectStore(self.root)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_publish_is_private_verified_and_idempotent(self) -> None:
        first = self.store.publish(b"durable current payload")
        second = self.store.publish(b"durable current payload")

        self.assertEqual(first, second)
        self.assertEqual(b"durable current payload", self.store.read_verified(first))
        path = self.store.path_for(first)
        self.assertEqual(1, path.stat().st_nlink)
        if os.name == "posix":
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_existing_corrupt_object_fails_closed(self) -> None:
        published = self.store.publish(b"original")
        path = self.store.path_for(published)
        path.write_bytes(b"corrupt")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_CORRUPT"):
            self.store.publish(b"original")

    def test_hardlinked_object_is_never_accepted(self) -> None:
        published = self.store.publish(b"one-link-only")
        os.link(self.store.path_for(published), self.root / "extra-link")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_UNSAFE"):
            self.store.read_verified(published)

    def test_symlinked_object_is_never_followed(self) -> None:
        published = self.store.publish(b"original object")
        outside = Path(self.scratch.name) / "outside-object"
        outside.write_bytes(b"original object")
        path = self.store.path_for(published)
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_UNSAFE"):
            self.store.read_verified(published)

    def test_replaced_staging_directory_is_rejected_before_write(self) -> None:
        original = self.root / "staging-original"
        self.store.staging.rename(original)
        outside = Path(self.scratch.name) / "outside-staging"
        outside.mkdir()
        try:
            self.store.staging.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            original.rename(self.store.staging)
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(CurrentObjectError, "CURRENT_OBJECT_ROOT_INVALID"):
            self.store.publish(b"must not escape")
        self.assertEqual([], list(outside.iterdir()))

    def test_forged_object_identity_cannot_escape_or_weaken_cas_checks(self) -> None:
        forged = (
            PublishedCurrentObject("../" + "a" * 61, 1, "objects/../outside"),
            PublishedCurrentObject("A" * 64, 1, f"objects/AA/{'A' * 64}"),
            PublishedCurrentObject("a" * 64, -1, f"objects/aa/{'a' * 64}"),
        )

        for identity in forged:
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(
                    CurrentObjectError, "CURRENT_OBJECT_IDENTITY_INVALID"
                ):
                    self.store.read_verified(identity)


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
