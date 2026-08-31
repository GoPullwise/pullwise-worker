from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from pullwise_worker import _generated_agent_task_contract as contract
from pullwise_worker.agent_kernel_current_bootstrap import (
    CurrentRuntimeBootstrapConsumer,
)
from pullwise_worker.agent_kernel_current_database import (
    CurrentAgentKernelDatabase,
)
from pullwise_worker.agent_kernel_current_debug import (
    CurrentWorkerDebugError,
    CurrentWorkerDebugStore,
)
from pullwise_worker.agent_kernel_current_objects import CurrentObjectStore
from pullwise_worker.agent_kernel_current_package import CURRENT_PACKAGE
from tests.current_runtime_bootstrap_support import (
    bootstrap_bytes,
    golden_runtime_bootstrap,
)


class CurrentWorkerDebugConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="current-s7-race-")
        self.root = Path(self.scratch.name)
        self.database = CurrentAgentKernelDatabase.open(
            self.root / "current", CURRENT_PACKAGE
        )
        self.objects = CurrentObjectStore(self.database.root / "content")
        bootstrap = golden_runtime_bootstrap(
            outer_job_id="job_" + "1" * 32,
            run_id="run_" + "2" * 32,
        )
        self.authority = CurrentRuntimeBootstrapConsumer(
            self.database
        ).ingest(bootstrap_bytes(bootstrap))
        plan = deepcopy(
            contract.fixture("gate_preparation_golden_debug_plan")["document"]
        )
        plan.pop("plan_digest")
        plan["task_id"] = self.authority.task_id
        plan["debug_input_refs"] = []
        plan = contract.seal_document("debug-redaction-plan/v1", plan)
        self.plan_bytes = contract.canonical_validated_bytes(
            "debug-redaction-plan/v1", plan
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def root_with(self, name: str, state: str) -> Path:
        root = self.root / name
        root.mkdir()
        (root / "debug-summary.json").write_text(
            json.dumps({"state": state}),
            encoding="utf-8",
        )
        return root

    def capture(self, root: Path, seq: int):
        return CurrentWorkerDebugStore(
            self.database,
            object_store=self.objects,
        ).capture(
            task_id=self.authority.task_id,
            capture_kind="checkpoint",
            snapshot_seq=seq,
            captured_at=f"2026-07-22T00:00:0{seq}.000Z",
            source_state_id="a" * 64,
            input_root=root,
            redaction_plan_bytes=self.plan_bytes,
            local_event_seq=seq,
            last_server_acked_event_seq=seq - 1,
        )

    def test_concurrent_exact_replays_converge_to_one_row(self) -> None:
        root = self.root_with("same", "same")
        captures = []
        for _wave in range(3):
            with ThreadPoolExecutor(max_workers=4) as executor:
                captures.extend(
                    executor.map(lambda _index: self.capture(root, 1), range(4))
                )
        self.assertEqual(1, len({item.sha256 for item in captures}))
        self.assertEqual(1, self._fragment_count())

    def test_concurrent_changed_bytes_have_one_winner(self) -> None:
        roots = (
            self.root_with("left", "left"),
            self.root_with("right", "right"),
        )

        def attempt(root: Path):
            try:
                return self.capture(root, 2)
            except CurrentWorkerDebugError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, roots))
        winners = [item for item in outcomes if not isinstance(item, Exception)]
        conflicts = [item for item in outcomes if isinstance(item, Exception)]
        self.assertEqual(1, len(winners))
        self.assertEqual(1, len(conflicts))
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflicts[0].code)
        self.assertEqual(1, self._fragment_count())

    def _fragment_count(self) -> int:
        connection = self.database.connect()
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM worker_debug_fragments"
            ).fetchone()[0]
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
