from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from pullwise_worker.agent_kernel_database import AgentKernelDatabase
from pullwise_worker.agent_kernel_state import (
    AttemptState,
    TaskEvent,
    TaskEventKind,
    TransitionFacts,
)
from pullwise_worker.agent_kernel_task_store import ActorFence, TaskStore, TaskStoreError


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-18T08:00:00.000Z"


def _record() -> dict[str, object]:
    fixtures = ROOT / "contracts" / "agent-task" / "v1" / "fixtures"
    for path in sorted(fixtures.glob("schema-golden*.json")):
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            if case["schema_id"] == "task-record/v1":
                return copy.deepcopy(case["valid"])
    raise AssertionError("task-record/v1 fixture missing")


class AgentKernelOwnerFencingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-owner-")
        database = AgentKernelDatabase(Path(self.scratch.name) / "worker")
        database.initialize()
        self.store = TaskStore(database)
        record = _record()
        self.task_id = str(record["task_id"])
        self.store.accept_task(record, idempotency_key="accept-owner")
        self.claimed = self.store.apply_event(
            self.task_id,
            expected_task_version=1,
            event=TaskEvent(
                kind=TaskEventKind.ATTEMPT_CLAIMED,
                idempotency_key="claim-owner",
                occurred_at=NOW,
                attempt_id="attempt_" + "1" * 32,
                budget_reservation_id="budget-owner",
            ),
            facts=TransitionFacts.permissive(),
        )

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_owner_incarnations_produce_monotonic_exact_session_fences(self) -> None:
        attempt_id = str(self.claimed.task.current_attempt_id)
        preparing = self.store.advance_attempt(
            self.task_id,
            attempt_id,
            expected_state_version=1,
            target_state=AttemptState.PREPARING,
            occurred_at=NOW,
        )
        running = self.store.advance_attempt(
            self.task_id,
            attempt_id,
            expected_state_version=2,
            target_state=AttemptState.RUNNING,
            occurred_at=NOW,
        )
        self.assertEqual((2, 3), (preparing.state_version, running.state_version))

        owner = self.store.begin_owner_incarnation(
            self.task_id,
            expected_task_version=self.claimed.task.task_version,
            attempt_id=attempt_id,
            native_epoch=1,
            session_id="session_" + "2" * 32,
            idempotency_key="owner-1",
            occurred_at=NOW,
        )
        retry = self.store.begin_owner_incarnation(
            self.task_id,
            expected_task_version=self.claimed.task.task_version,
            attempt_id=attempt_id,
            native_epoch=1,
            session_id="session_" + "2" * 32,
            idempotency_key="owner-1",
            occurred_at=NOW,
        )
        self.assertEqual((1, 3, False), (
            retry.owner_epoch,
            owner.task.task_version,
            retry.applied,
        ))
        fence = ActorFence.from_task(
            owner.task, owner_session_id="session_" + "2" * 32
        )
        self.store.assert_fresh_actor(self.task_id, fence)

        stale_cases = (
            ("owner_epoch", 0, "OWNER_EPOCH_STALE"),
            ("native_epoch", 0, "NATIVE_EPOCH_STALE"),
            ("lease_id", "lease-stale", "LEASE_INVALID"),
            ("owner_session_id", "session_" + "3" * 32, "OWNER_EPOCH_STALE"),
        )
        for field, value, code in stale_cases:
            with self.subTest(field=field), self.assertRaisesRegex(TaskStoreError, code):
                self.store.assert_fresh_actor(
                    self.task_id, ActorFence(**{**fence.as_dict(), field: value})
                )

        replacement = self.store.begin_owner_incarnation(
            self.task_id,
            expected_task_version=owner.task.task_version,
            attempt_id=attempt_id,
            native_epoch=1,
            session_id="session_" + "4" * 32,
            idempotency_key="owner-2",
            occurred_at="2026-07-18T08:00:01.000Z",
        )
        self.assertEqual((2, 4), (
            replacement.owner_epoch,
            replacement.task.task_version,
        ))
        with self.store.database.connect() as connection:
            states = {
                row["session_id"]: row["state"]
                for row in connection.execute(
                    "SELECT session_id,state FROM owner_incarnations WHERE task_id=?",
                    (self.task_id,),
                )
            }
        self.assertEqual("FENCED", states["session_" + "2" * 32])
        self.assertEqual("ACTIVE", states["session_" + "4" * 32])
        self.store.assert_fresh_actor(
            self.task_id,
            ActorFence.from_task(
                replacement.task, owner_session_id="session_" + "4" * 32
            ),
        )


if __name__ == "__main__":
    unittest.main()
