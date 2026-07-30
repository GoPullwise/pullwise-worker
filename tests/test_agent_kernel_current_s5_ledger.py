from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pullwise_worker import _generated_agent_task_contract as contract
from pullwise_worker.agent_kernel_current_authority import (
    CurrentAuthorityProjection,
)
from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
)
from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
)
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from pullwise_worker.agent_kernel_current_requirements import (
    CurrentRequirementLedgerError,
    CurrentRequirementLedgerStore,
)
from tests.current_runtime_bootstrap_support import (
    bootstrap_bytes,
    golden_runtime_bootstrap,
)
from tests.current_s5_support import (
    canonical_bytes,
    fenced_authority,
    object_sha256,
    successor_ledger,
)


class CurrentRequirementLedgerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s5-ledger-")
        self.database = CurrentAgentKernelDatabase.open(
            Path(self.scratch.name) / "current", CURRENT_PACKAGE
        )
        self.bootstrap = CurrentRuntimeBootstrapConsumer(self.database).ingest(
            bootstrap_bytes()
        )
        self.initial = golden_runtime_bootstrap()["accept_request"][
            "requirement_ledger"
        ]

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_bootstrap_seeds_exact_request_policy_and_ledger_root(self) -> None:
        store = CurrentRequirementLedgerStore(self.database)
        current = store.current(self.bootstrap.task_id)
        raw = canonical_bytes("requirement-ledger/v1", self.initial)

        self.assertEqual(1, current.ledger_version)
        self.assertEqual(self.initial["ledger_digest"], current.ledger_digest)
        self.assertEqual(raw, current.canonical_bytes)
        self.assertEqual(object_sha256(raw), current.object_sha256)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT content_schema_id, object_bytes FROM checkpoint_objects "
                "WHERE content_schema_id IN "
                "('task-request/v1','effective-execution-policy/v1',"
                "'requirement-ledger/v1') ORDER BY content_schema_id"
            ).fetchall()
            version = connection.execute(
                "SELECT ledger_version, ledger_digest, object_sha256, "
                "previous_ledger_digest FROM requirement_ledger_versions"
            ).fetchone()
        self.assertEqual(3, len(rows))
        self.assertEqual(
            (1, current.ledger_digest, current.object_sha256, None),
            tuple(version),
        )

    def test_append_is_generated_validated_cas_and_exact_replay(self) -> None:
        store = CurrentRequirementLedgerStore(self.database)
        candidate = successor_ledger(self.initial)
        raw = canonical_bytes("requirement-ledger/v1", candidate)

        first = store.append(
            raw,
            expected_previous_digest=self.initial["ledger_digest"],
        )
        replay = store.append(
            raw,
            expected_previous_digest=self.initial["ledger_digest"],
        )

        self.assertEqual(first, replay)
        self.assertEqual(2, first.ledger_version)
        self.assertEqual(candidate["ledger_digest"], first.ledger_digest)
        self.assertEqual(first, store.current(self.bootstrap.task_id))
        with self.database.connect() as connection:
            counts = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "requirement_ledger_versions",
                    "requirement_ledger_heads",
                )
            )
        self.assertEqual((2, 1), counts)

        with self.assertRaises(CurrentRequirementLedgerError) as stale:
            store.append(
                canonical_bytes(
                    "requirement-ledger/v1",
                    successor_ledger(self.initial, suffix="6"),
                ),
                expected_previous_digest="0" * 64,
            )
        self.assertEqual("REQUIREMENT_LEDGER_CAS_CONFLICT", stale.exception.code)

    def test_history_mutation_and_noncanonical_bytes_are_rejected(self) -> None:
        store = CurrentRequirementLedgerStore(self.database)
        candidate = successor_ledger(self.initial)
        mutated = deepcopy(candidate)
        mutated["entries"][0]["statement"] = "Mutated accepted objective."
        mutated = contract.seal_document("requirement-ledger/v1", mutated)

        with self.assertRaises(CurrentRequirementLedgerError) as history:
            store.append(
                canonical_bytes("requirement-ledger/v1", mutated),
                expected_previous_digest=self.initial["ledger_digest"],
            )
        self.assertEqual(
            "REQUIREMENT_LEDGER_TRANSITION_INVALID", history.exception.code
        )
        valid = canonical_bytes(
            "requirement-ledger/v1", successor_ledger(self.initial)
        )
        with self.assertRaises(CurrentRequirementLedgerError) as noncanonical:
            store.append(
                valid + b"\n",
                expected_previous_digest=self.initial["ledger_digest"],
            )
        self.assertEqual(
            "REQUIREMENT_LEDGER_NONCANONICAL", noncanonical.exception.code
        )

    def test_fence_and_each_append_stage_leave_genesis_unchanged(self) -> None:
        CurrentAuthorityProjection(self.database).record_fenced(
            fenced_authority(self.bootstrap),
            expected_previous_digest=self.bootstrap.digest,
        )
        raw = canonical_bytes(
            "requirement-ledger/v1", successor_ledger(self.initial)
        )
        with self.assertRaises(CurrentRequirementLedgerError) as fenced:
            CurrentRequirementLedgerStore(self.database).append(
                raw,
                expected_previous_digest=self.initial["ledger_digest"],
            )
        self.assertEqual("AUTHORITY_FENCED", fenced.exception.code)

        for index, stage in enumerate(
            (
                "after_object",
                "after_version",
                "before_head_cas",
                "after_head_cas",
                "before_commit",
            ),
            start=1,
        ):
            with self.subTest(stage=stage):
                database = CurrentAgentKernelDatabase.open(
                    Path(self.scratch.name) / f"fault-{index}", CURRENT_PACKAGE
                )
                CurrentRuntimeBootstrapConsumer(database).ingest(
                    bootstrap_bytes()
                )

                def inject(actual: str, selected: str = stage) -> None:
                    if actual == selected:
                        raise RuntimeError(f"injected:{selected}")

                store = CurrentRequirementLedgerStore(
                    database, fault_hook=inject
                )
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    store.append(
                        raw,
                        expected_previous_digest=self.initial["ledger_digest"],
                    )
                current = CurrentRequirementLedgerStore(database).current(
                    self.bootstrap.task_id
                )
                self.assertEqual(1, current.ledger_version)
                with database.connect() as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM requirement_ledger_versions"
                    ).fetchone()[0]
                self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
