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
    TerminalPublication,
    TransitionFacts,
)
from pullwise_worker.agent_kernel_task_store import (
    ActorFence,
    TaskStore,
    TaskStoreError,
)


CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "agent-task" / "v1"
NOW = "2026-07-18T08:00:00.000Z"


def _record() -> dict[str, object]:
    for path in sorted((CONTRACT_ROOT / "fixtures").glob("schema-golden*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            if case["schema_id"] == "task-record/v1":
                return copy.deepcopy(case["valid"])
    raise AssertionError("task-record/v1 fixture missing")


def _event(kind: str, key: str, **values: object) -> TaskEvent:
    return TaskEvent(kind=kind, idempotency_key=key, occurred_at=NOW, **values)


class AgentKernelTaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="agent-kernel-task-store-")
        database = AgentKernelDatabase(Path(self.scratch.name) / "worker")
        database.initialize()
        self.store = TaskStore(database)
        self.record = _record()
        self.task_id = str(self.record["task_id"])

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _accept_and_claim(self) -> tuple[object, object]:
        accepted = self.store.accept_task(
            self.record, idempotency_key="accept-1", scan_id="scan-original"
        )
        claimed = self.store.apply_event(
            self.task_id,
            expected_task_version=1,
            event=_event(
                TaskEventKind.ATTEMPT_CLAIMED,
                "claim-1",
                attempt_id="attempt_" + "1" * 32,
                budget_reservation_id="budget-1",
            ),
            facts=TransitionFacts.permissive(),
        )
        return accepted, claimed

    def test_accept_and_claim_are_atomic_idempotent_versioned_mutations(self) -> None:
        accepted, claimed = self._accept_and_claim()

        self.assertTrue(accepted.applied)
        self.assertEqual(("QUEUED", 1, 0), (
            accepted.task.lifecycle,
            accepted.task.task_version,
            accepted.task.native_epoch,
        ))
        self.assertEqual(("ACTIVE", 2, 1), (
            claimed.task.lifecycle,
            claimed.task.task_version,
            claimed.task.native_epoch,
        ))
        self.assertEqual("LEASED", self.store.get_attempt(
            "attempt_" + "1" * 32
        ).state)

        retried = self.store.apply_event(
            self.task_id,
            expected_task_version=1,
            event=_event(
                TaskEventKind.ATTEMPT_CLAIMED,
                "claim-1",
                attempt_id="attempt_" + "1" * 32,
                budget_reservation_id="budget-1",
            ),
            facts=TransitionFacts.permissive(),
        )
        self.assertFalse(retried.applied)
        self.assertEqual(2, retried.event_task_version)
        self.assertEqual(1, self.store.count_attempts(self.task_id))

        with self.assertRaisesRegex(TaskStoreError, "IDEMPOTENCY_CONFLICT"):
            self.store.apply_event(
                self.task_id,
                expected_task_version=2,
                event=_event(
                    TaskEventKind.ATTEMPT_CLAIMED,
                    "claim-1",
                    attempt_id="attempt_" + "2" * 32,
                    budget_reservation_id="budget-2",
                ),
                facts=TransitionFacts.permissive(),
            )

    def test_attempt_edges_and_owner_incarnation_produce_monotonic_fences(self) -> None:
        _, claimed = self._accept_and_claim()
        attempt_id = "attempt_" + "1" * 32
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
            expected_task_version=claimed.task.task_version,
            attempt_id=attempt_id,
            native_epoch=1,
            session_id="session_" + "2" * 32,
            idempotency_key="owner-1",
            occurred_at=NOW,
        )
        self.assertEqual((1, 3), (owner.owner_epoch, owner.task.task_version))
        owner_retry = self.store.begin_owner_incarnation(
            self.task_id,
            expected_task_version=2,
            attempt_id=attempt_id,
            native_epoch=1,
            session_id="session_" + "2" * 32,
            idempotency_key="owner-1",
            occurred_at=NOW,
        )
        self.assertFalse(owner_retry.applied)
        self.assertEqual(1, owner_retry.owner_epoch)
        fence = ActorFence.from_task(
            owner.task, owner_session_id="session_" + "2" * 32
        )
        self.store.assert_fresh_actor(self.task_id, fence)

        stale_owner = ActorFence(
            **{**fence.as_dict(), "owner_epoch": 0}
        )
        with self.assertRaisesRegex(TaskStoreError, "OWNER_EPOCH_STALE"):
            self.store.assert_fresh_actor(self.task_id, stale_owner)
        stale_native = ActorFence(
            **{**fence.as_dict(), "native_epoch": 0}
        )
        with self.assertRaisesRegex(TaskStoreError, "NATIVE_EPOCH_STALE"):
            self.store.assert_fresh_actor(self.task_id, stale_native)
        stale_lease = ActorFence(**{**fence.as_dict(), "lease_id": "lease-stale"})
        with self.assertRaisesRegex(TaskStoreError, "LEASE_INVALID"):
            self.store.assert_fresh_actor(self.task_id, stale_lease)
        stale_session = ActorFence(
            **{**fence.as_dict(), "owner_session_id": "session_" + "3" * 32}
        )
        with self.assertRaisesRegex(TaskStoreError, "OWNER_EPOCH_STALE"):
            self.store.assert_fresh_actor(self.task_id, stale_session)

    def test_waiting_transition_requires_attempt_to_finish_suspending(self) -> None:
        _, claimed = self._accept_and_claim()
        attempt_id = str(claimed.task.current_attempt_id)
        for expected, target in (
            (1, AttemptState.PREPARING),
            (2, AttemptState.RUNNING),
            (3, AttemptState.SUSPENDING),
        ):
            self.store.advance_attempt(
                self.task_id,
                attempt_id,
                expected_state_version=expected,
                target_state=target,
                occurred_at=NOW,
            )

        waiting = self.store.apply_event(
            self.task_id,
            expected_task_version=2,
            event=_event(
                TaskEventKind.INTERACTION_REQUESTED,
                "interaction-1",
                interaction_kind="approval",
            ),
            facts=TransitionFacts.permissive(),
        )
        self.assertEqual("WAITING_APPROVAL", waiting.task.lifecycle)
        attempt = self.store.get_attempt(attempt_id)
        self.assertEqual((AttemptState.SUSPENDED, 5), (
            attempt.state,
            attempt.state_version,
        ))

    def test_publication_is_one_terminal_transaction_and_abandonment_is_not_result(self) -> None:
        _, claimed = self._accept_and_claim()
        attempt_id = str(claimed.task.current_attempt_id)
        for expected, target in (
            (1, AttemptState.PREPARING),
            (2, AttemptState.RUNNING),
            (3, AttemptState.VERIFYING),
            (4, AttemptState.PUBLISHING),
        ):
            self.store.advance_attempt(
                self.task_id,
                attempt_id,
                expected_state_version=expected,
                target_state=target,
                occurred_at=NOW,
            )
        finalizing = self.store.apply_event(
            self.task_id,
            expected_task_version=2,
            event=_event(TaskEventKind.COMPLETION_PROPOSED, "proposal-1"),
            facts=TransitionFacts.permissive(),
        )
        publication = TerminalPublication(
            result_ref="cas:result-1",
            result_digest="a" * 64,
            outcome="COMPLETED",
            published_at=NOW,
            attempt_terminal_state=AttemptState.SUCCEEDED,
        )
        terminal = self.store.apply_event(
            self.task_id,
            expected_task_version=finalizing.task.task_version,
            event=_event(
                TaskEventKind.RESULT_PUBLISHED,
                "publish-1",
                publication=publication,
            ),
            facts=TransitionFacts.permissive(),
        )
        self.assertEqual(("TERMINAL", "task_result"), (
            terminal.task.lifecycle,
            terminal.task.terminal_kind,
        ))
        self.assertEqual(AttemptState.SUCCEEDED, self.store.get_attempt(attempt_id).state)
        self.assertEqual(1, self.store.count_publications(self.task_id))

        with self.assertRaisesRegex(TaskStoreError, "TASK_ALREADY_TERMINAL"):
            self.store.apply_event(
                self.task_id,
                expected_task_version=terminal.task.task_version,
                event=_event(TaskEventKind.CANCEL_REQUESTED, "cancel-late"),
                facts=TransitionFacts.permissive(),
            )

    def test_finalizing_terminalization_fact_append_does_not_invent_version_change(self) -> None:
        _, claimed = self._accept_and_claim()
        finalizing = self.store.apply_event(
            self.task_id,
            expected_task_version=claimed.task.task_version,
            event=_event(TaskEventKind.COMPLETION_PROPOSED, "proposal-fact"),
            facts=TransitionFacts.permissive(),
        )
        fact = _event(
            TaskEventKind.TERMINALIZATION_REQUESTED,
            "terminal-fact-1",
            terminalization_reason="DEADLINE_REACHED",
        )
        appended = self.store.apply_event(
            self.task_id,
            expected_task_version=finalizing.task.task_version,
            event=fact,
            facts=TransitionFacts(authoritative_terminalization=True),
        )
        retried = self.store.apply_event(
            self.task_id,
            expected_task_version=finalizing.task.task_version,
            event=fact,
            facts=TransitionFacts(authoritative_terminalization=True),
        )

        self.assertTrue(appended.applied)
        self.assertFalse(retried.applied)
        self.assertEqual(finalizing.task.task_version, appended.task.task_version)


if __name__ == "__main__":
    unittest.main()
