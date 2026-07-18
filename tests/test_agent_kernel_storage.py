from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import random
import sqlite3
import tempfile
import time
import unittest

from pullwise_worker.agent_kernel_database import (
    AgentKernelDatabase,
    AgentKernelStorageError,
    LATEST_SCHEMA_VERSION,
    REQUIRED_TABLES,
)
from pullwise_worker.agent_kernel_object_store import (
    CasCorruptError,
    ContentRefConflictError,
    ObjectStore,
)


class InjectedCrash(RuntimeError):
    pass


class AgentKernelStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-storage-")
        self.worker_root = Path(self.scratch.name) / "worker"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _database(self) -> AgentKernelDatabase:
        database = AgentKernelDatabase(self.worker_root)
        database.initialize()
        return database

    def test_database_initializes_required_pragmas_tables_and_migration(self) -> None:
        database = self._database()

        with database.connect() as connection:
            pragmas = {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            migrations = connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations"
            ).fetchall()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual("wal", str(pragmas["journal_mode"]).lower())
        self.assertEqual(1, pragmas["foreign_keys"])
        self.assertEqual(2, pragmas["synchronous"])
        self.assertEqual(5000, pragmas["busy_timeout"])
        self.assertTrue(REQUIRED_TABLES <= tables)
        self.assertEqual(LATEST_SCHEMA_VERSION, user_version)
        self.assertEqual(2, len(migrations))
        self.assertRegex(migrations[0][2], r"^[0-9a-f]{64}$")
        self.assertEqual(database.path, self.worker_root / "agent-kernel" / "state.sqlite3")

        database.initialize()
        with database.connect() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
            )

    def test_database_rejects_unknown_higher_schema_version(self) -> None:
        database = self._database()
        with sqlite3.connect(database.path) as connection:
            connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION + 1}")

        with self.assertRaisesRegex(AgentKernelStorageError, "schema_version_unsupported"):
            database.initialize()

    def test_database_rejects_link_and_nonregular_database_paths(self) -> None:
        for case in ("hardlink", "symlink", "directory"):
            with self.subTest(case=case):
                database = AgentKernelDatabase(self.worker_root / case)
                database.root.mkdir(parents=True, mode=0o700)
                if case == "directory":
                    database.path.mkdir()
                else:
                    outside = self.worker_root / f"outside-{case}.sqlite3"
                    outside.write_bytes(b"not a worker database")
                    if case == "hardlink":
                        os.link(outside, database.path)
                    else:
                        database.path.symlink_to(outside)

                with self.assertRaisesRegex(
                    AgentKernelStorageError, "database_path_invalid"
                ):
                    database.initialize()

    def test_migration_crash_rolls_back_and_clean_restart_applies_once(self) -> None:
        def crash(stage: str) -> None:
            if stage == "before_migration_commit":
                raise InjectedCrash(stage)

        database = AgentKernelDatabase(self.worker_root, stage_hook=crash)
        with self.assertRaisesRegex(InjectedCrash, "before_migration_commit"):
            database.initialize()

        recovered = AgentKernelDatabase(self.worker_root)
        recovered.initialize()
        with recovered.connect() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
            )
            self.assertEqual(
                LATEST_SCHEMA_VERSION,
                connection.execute("PRAGMA user_version").fetchone()[0],
            )

    def test_put_publishes_durable_bytes_then_database_reference(self) -> None:
        database = self._database()
        store = ObjectStore(database)

        ref = store.put_bytes(
            b'{"schema_id":"example/v1"}',
            task_id="task_" + "1" * 32,
            artifact_id="art_" + "2" * 32,
            media_type="application/json",
            content_schema_id="example/v1",
            encoding="utf-8",
        )

        path = store.path_for_digest(ref["sha256"])
        self.assertEqual(ref["size_bytes"], path.stat().st_size)
        self.assertEqual(b'{"schema_id":"example/v1"}', path.read_bytes())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        with database.connect() as connection:
            object_row = connection.execute(
                "SELECT size_bytes, content_schema_id FROM content_objects WHERE sha256=?",
                (ref["sha256"],),
            ).fetchone()
            binding_row = connection.execute(
                "SELECT sha256 FROM content_bindings WHERE task_id=? AND artifact_id=?",
                ("task_" + "1" * 32, "art_" + "2" * 32),
            ).fetchone()
        self.assertEqual((ref["size_bytes"], "example/v1"), tuple(object_row))
        self.assertEqual(ref["sha256"], binding_row[0])
        self.assertEqual(ref, store.put_bytes(
            path.read_bytes(),
            task_id="task_" + "1" * 32,
            artifact_id="art_" + "2" * 32,
            media_type="application/json",
            content_schema_id="example/v1",
            encoding="utf-8",
        ))

    def test_artifact_identity_cannot_be_rebound_to_different_bytes(self) -> None:
        store = ObjectStore(self._database())
        common = {
            "task_id": "task_" + "3" * 32,
            "artifact_id": "art_" + "4" * 32,
            "media_type": "application/octet-stream",
            "content_schema_id": "opaque-bytes/v1",
            "encoding": "binary",
        }
        store.put_bytes(b"first", **common)

        with self.assertRaisesRegex(ContentRefConflictError, "CONTENT_REF_CONFLICT"):
            store.put_bytes(b"second", **common)

    def test_existing_corrupt_object_is_never_reused(self) -> None:
        store = ObjectStore(self._database())
        expected = store.digest_bytes(b"trusted")
        path = store.path_for_digest(expected)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"corrupt")

        with self.assertRaisesRegex(CasCorruptError, "CAS_CORRUPT"):
            store.put_bytes(
                b"trusted",
                task_id="task_" + "5" * 32,
                artifact_id="art_" + "6" * 32,
                media_type="application/octet-stream",
                content_schema_id="opaque-bytes/v1",
                encoding="binary",
            )

    def test_existing_link_or_overbroad_permissions_are_corruption(self) -> None:
        cases = ("hardlink", "symlink", "permissions")
        for case in cases:
            with self.subTest(case=case):
                database = AgentKernelDatabase(self.worker_root / case)
                database.initialize()
                store = ObjectStore(database)
                payload = case.encode()
                digest = store.digest_bytes(payload)
                path = store.path_for_digest(digest)
                path.parent.mkdir(parents=True, exist_ok=True)
                if case == "hardlink":
                    outside = self.worker_root / f"{case}.bin"
                    outside.parent.mkdir(parents=True, exist_ok=True)
                    outside.write_bytes(payload)
                    os.link(outside, path)
                elif case == "symlink":
                    outside = self.worker_root / f"{case}.bin"
                    outside.parent.mkdir(parents=True, exist_ok=True)
                    outside.write_bytes(payload)
                    path.symlink_to(outside)
                else:
                    path.write_bytes(payload)
                    path.chmod(0o644)
                with self.assertRaisesRegex(CasCorruptError, "CAS_CORRUPT"):
                    store.put_bytes(
                        payload,
                        task_id="task_" + "c" * 32,
                        artifact_id="art_" + "d" * 32,
                        media_type="application/octet-stream",
                        content_schema_id="opaque-bytes/v1",
                        encoding="binary",
                    )

    def test_crash_boundaries_never_publish_database_before_bytes(self) -> None:
        stages = {
            "after_file_fsync": (False, False),
            "after_object_publish": (True, False),
            "after_database_commit": (True, True),
        }
        for stage, (object_exists, row_exists) in stages.items():
            with self.subTest(stage=stage):
                root = self.worker_root / stage
                database = AgentKernelDatabase(root)
                database.initialize()

                def crash(current: str) -> None:
                    if current == stage:
                        raise InjectedCrash(stage)

                store = ObjectStore(database, stage_hook=crash)
                digest = store.digest_bytes(stage.encode())
                with self.assertRaisesRegex(InjectedCrash, stage):
                    store.put_bytes(
                        stage.encode(),
                        task_id="task_" + "7" * 32,
                        artifact_id="art_" + "8" * 32,
                        media_type="application/octet-stream",
                        content_schema_id="opaque-bytes/v1",
                        encoding="binary",
                    )
                self.assertEqual(object_exists, store.path_for_digest(digest).exists())
                with database.connect() as connection:
                    found = connection.execute(
                        "SELECT COUNT(*) FROM content_objects WHERE sha256=?", (digest,)
                    ).fetchone()[0]
                self.assertEqual(row_exists, bool(found))

    def test_idle_gc_removes_only_expired_unreferenced_regular_objects(self) -> None:
        database = self._database()
        store = ObjectStore(database)
        referenced = store.put_bytes(
            b"keep",
            task_id="task_" + "9" * 32,
            artifact_id="art_" + "a" * 32,
            media_type="application/octet-stream",
            content_schema_id="opaque-bytes/v1",
            encoding="binary",
        )
        orphan_digest = store.digest_bytes(b"orphan")
        orphan = store.path_for_digest(orphan_digest)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        os.utime(store.path_for_digest(referenced["sha256"]), (old, old))

        self.assertEqual([], store.collect_orphans(idle=False, older_than_seconds=1))
        removed = store.collect_orphans(idle=True, older_than_seconds=3600)

        self.assertEqual([orphan_digest], removed)
        self.assertTrue(store.path_for_digest(referenced["sha256"]).exists())

    def test_idle_gc_removes_only_expired_private_staging_files(self) -> None:
        store = ObjectStore(self._database())
        old = store.tmp_root / "object-abandoned.tmp"
        young = store.tmp_root / "object-young.tmp"
        unrelated = store.tmp_root / "operator-note.txt"
        linked = store.tmp_root / "object-linked.tmp"
        for path in (old, young, unrelated):
            path.write_bytes(path.name.encode())
            path.chmod(0o600)
        linked.symlink_to(old)
        timestamp = time.time() - 7200
        for path in (old, unrelated):
            os.utime(path, (timestamp, timestamp))

        self.assertEqual([], store.collect_orphans(idle=False, older_than_seconds=1))
        removed = store.collect_orphans(idle=True, older_than_seconds=3600)

        self.assertEqual(["tmp:object-abandoned.tmp"], removed)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(linked.is_symlink())

    def test_random_binary_objects_round_trip_with_matching_content_refs(self) -> None:
        store = ObjectStore(self._database())
        randomizer = random.Random(718)
        for index in range(32):
            payload = randomizer.randbytes(randomizer.randrange(0, 4096))
            ref = store.put_bytes(
                payload,
                task_id="task_" + "b" * 32,
                artifact_id=f"art_{index:032x}",
                media_type="application/octet-stream",
                content_schema_id="opaque-bytes/v1",
                encoding="binary",
            )
            self.assertEqual(payload, store.read_verified(ref))

    def test_concurrent_identical_publishers_converge_on_one_object_and_binding(self) -> None:
        database = self._database()
        store = ObjectStore(database)
        arguments = {
            "task_id": "task_" + "e" * 32,
            "artifact_id": "art_" + "f" * 32,
            "media_type": "application/octet-stream",
            "content_schema_id": "opaque-bytes/v1",
            "encoding": "binary",
        }

        with ThreadPoolExecutor(max_workers=8) as executor:
            refs = list(
                executor.map(
                    lambda _: store.put_bytes(b"same immutable bytes", **arguments),
                    range(24),
                )
            )

        self.assertTrue(all(ref == refs[0] for ref in refs))
        with database.connect() as connection:
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0]
            )
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM content_bindings").fetchone()[0]
            )


if __name__ == "__main__":
    unittest.main()
