from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import stat
import sqlite3
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

if __name__ == "__main__":
    unittest.main()
