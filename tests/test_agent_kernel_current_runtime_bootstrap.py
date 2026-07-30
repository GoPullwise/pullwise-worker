from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
    CurrentRuntimeBootstrapError,
)
from pullwise_worker.agent_kernel_current_database import CurrentAgentKernelDatabase
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from tests.current_runtime_bootstrap_support import (
    bootstrap_bytes,
    changed_same_task_bootstrap,
    golden_runtime_bootstrap,
)


BOOTSTRAP_TABLES = (
    "runtime_bootstraps",
    "runtime_task_records",
    "runtime_task_heads",
    "runtime_attempt_records",
    "runtime_attempt_heads",
    "runtime_owner_records",
    "runtime_owner_heads",
    "authority_history",
    "authority_heads",
    "dispatch_budgets",
)


class CurrentRuntimeBootstrapConsumerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="runtime-bootstrap-")
        self.database = CurrentAgentKernelDatabase.open(
            Path(self.scratch.name) / "current", CURRENT_PACKAGE
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def counts(self) -> tuple[int, ...]:
        with self.database.connect() as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in BOOTSTRAP_TABLES
            )

    def test_exact_bootstrap_is_one_atomic_replayable_runtime_root(self) -> None:
        raw = bootstrap_bytes()
        consumer = CurrentRuntimeBootstrapConsumer(self.database)

        first = consumer.ingest(raw)
        replay = consumer.ingest(raw)

        expected = golden_runtime_bootstrap()
        self.assertEqual(expected["authority"]["authority_digest"], first.digest)
        self.assertEqual(first, replay)
        self.assertEqual((1,) * len(BOOTSTRAP_TABLES), self.counts())
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT bootstrap_bytes, task_record_bytes, attempt_record_bytes, "
                "owner_record_bytes FROM runtime_bootstraps"
            ).fetchone()
        self.assertEqual(raw, row["bootstrap_bytes"])

    def test_noncanonical_and_same_task_conflict_fail_before_partial_state(self) -> None:
        consumer = CurrentRuntimeBootstrapConsumer(self.database)
        raw = bootstrap_bytes()
        with self.assertRaises(CurrentRuntimeBootstrapError) as raised:
            consumer.ingest(raw + b"\n")
        self.assertEqual("RUNTIME_BOOTSTRAP_NONCANONICAL", raised.exception.code)
        self.assertEqual((0,) * len(BOOTSTRAP_TABLES), self.counts())

        consumer.ingest(raw)
        before = self.counts()
        with self.assertRaises(CurrentRuntimeBootstrapError) as conflict:
            consumer.ingest(bootstrap_bytes(changed_same_task_bootstrap()))
        self.assertEqual("RUNTIME_BOOTSTRAP_REPLAY_CONFLICT", conflict.exception.code)
        self.assertEqual(before, self.counts())

    def test_every_write_stage_rolls_back_as_one_transaction(self) -> None:
        stages = (
            "after_bootstrap",
            "after_task",
            "after_attempt",
            "after_owner",
            "after_authority",
            "before_commit",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                def inject(actual: str, *, selected: str = stage) -> None:
                    if actual == selected:
                        raise RuntimeError(f"injected:{selected}")

                consumer = CurrentRuntimeBootstrapConsumer(
                    self.database, fault_hook=inject
                )
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    consumer.ingest(bootstrap_bytes())
                self.assertEqual((0,) * len(BOOTSTRAP_TABLES), self.counts())


if __name__ == "__main__":
    unittest.main()
