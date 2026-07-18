from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from pullwise_worker.agent_kernel_database import AgentKernelDatabase
from pullwise_worker.agent_kernel_state import (
    TaskEvent,
    TaskEventKind,
    TerminalPublication,
    TransitionFacts,
)
from pullwise_worker.agent_kernel_task_store import TaskStore, TaskStoreError


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-18T08:00:00.000Z"


def _record(task_id: str) -> dict[str, object]:
    fixture_root = ROOT / "contracts" / "agent-task" / "v1" / "fixtures"
    for path in sorted(fixture_root.glob("schema-golden*.json")):
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            if case["schema_id"] == "task-record/v1":
                record = copy.deepcopy(case["valid"])
                record["task_id"] = task_id
                return record
    raise AssertionError("task-record/v1 fixture missing")


class AgentKernelTaskRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-races-")
        database = AgentKernelDatabase(Path(self.scratch.name) / "worker")
        database.initialize()
        self.store = TaskStore(database)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    @staticmethod
    def _event(kind: str, key: str, **values: object) -> TaskEvent:
        return TaskEvent(kind=kind, idempotency_key=key, occurred_at=NOW, **values)

    def _finalizing_task(self, suffix: str) -> tuple[str, int]:
        task_id = "task_" + suffix * 32
        self.store.accept_task(_record(task_id), idempotency_key=f"accept-{suffix}")
        claimed = self.store.apply_event(
            task_id,
            expected_task_version=1,
            event=self._event(
                TaskEventKind.ATTEMPT_CLAIMED,
                f"claim-{suffix}",
                attempt_id="attempt_" + suffix * 32,
                budget_reservation_id=f"budget-{suffix}",
            ),
            facts=TransitionFacts.permissive(),
        )
        finalizing = self.store.apply_event(
            task_id,
            expected_task_version=claimed.task.task_version,
            event=self._event(TaskEventKind.COMPLETION_PROPOSED, f"proposal-{suffix}"),
            facts=TransitionFacts.permissive(),
        )
        return task_id, finalizing.task.task_version

    def test_cancel_cas_first_prevents_stale_success_publication(self) -> None:
        task_id, version = self._finalizing_task("3")
        cancelled = self.store.apply_event(
            task_id,
            expected_task_version=version,
            event=self._event(TaskEventKind.CANCEL_REQUESTED, "cancel-first"),
            facts=TransitionFacts.permissive(),
        )
        self.assertEqual("CANCEL", cancelled.task.desired_state)

        publication = TerminalPublication(
            result_ref="cas:stale-success",
            result_digest="4" * 64,
            outcome="COMPLETED",
            published_at=NOW,
            attempt_terminal_state="SUCCEEDED",
        )
        with self.assertRaisesRegex(TaskStoreError, "TASK_VERSION_STALE"):
            self.store.apply_event(
                task_id,
                expected_task_version=version,
                event=self._event(
                    TaskEventKind.RESULT_PUBLISHED,
                    "publish-stale",
                    publication=publication,
                ),
                facts=TransitionFacts.permissive(),
            )
        self.assertEqual(0, self.store.count_publications(task_id))

    def test_concurrent_publishers_have_one_cas_winner_and_one_publication(self) -> None:
        task_id, version = self._finalizing_task("5")
        barrier = threading.Barrier(8)

        def publish(index: int) -> str:
            barrier.wait()
            try:
                self.store.apply_event(
                    task_id,
                    expected_task_version=version,
                    event=self._event(
                        TaskEventKind.RESULT_PUBLISHED,
                        f"publish-{index}",
                        publication=TerminalPublication(
                            result_ref=f"cas:result-{index}",
                            result_digest=f"{index:x}" * 64,
                            outcome="COMPLETED",
                            published_at=NOW,
                            attempt_terminal_state="SUCCEEDED",
                        ),
                    ),
                    facts=TransitionFacts.permissive(),
                )
                return "won"
            except TaskStoreError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(publish, range(8)))

        self.assertEqual(1, outcomes.count("won"))
        self.assertTrue(all(value in {"won", "TASK_ALREADY_TERMINAL"} for value in outcomes))
        self.assertEqual(1, self.store.count_publications(task_id))
        task = self.store.get_task(task_id)
        self.assertEqual(("TERMINAL", version + 1), (task.lifecycle, task.task_version))


if __name__ == "__main__":
    unittest.main()
