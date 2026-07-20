from __future__ import annotations

import itertools
import unittest

from pullwise_worker.agent_kernel_state import (
    ATTEMPT_STATES,
    ATTEMPT_TRANSITIONS,
    TASK_EVENT_KINDS,
    TASK_LIFECYCLES,
    AttemptState,
    StateTransitionError,
    TaskEvent,
    TaskEventKind,
    TaskState,
    TerminalPublication,
    TransitionFacts,
    reduce_attempt,
    reduce_task,
)


NOW = "2026-07-18T08:00:00.000Z"


def _state(lifecycle: str, *, desired_state: str = "RUN") -> TaskState:
    return TaskState(
        lifecycle=lifecycle,
        desired_state=desired_state,
        task_version=7,
        native_epoch=2,
        current_attempt_id="attempt_" + "a" * 32,
        terminalization_reason=None,
    )


def _event(kind: str) -> TaskEvent:
    publication = TerminalPublication(
        result_ref="cas:result",
        result_digest="b" * 64,
        outcome="COMPLETED",
        published_at=NOW,
        attempt_terminal_state="SUCCEEDED",
    )
    values = {
        "kind": kind,
        "idempotency_key": f"idem-{kind}",
        "occurred_at": NOW,
        "attempt_id": "attempt_" + "c" * 32,
        "interaction_kind": "input",
        "terminalization_reason": "DEADLINE_REACHED",
        "budget_reservation_id": "budget-1",
        "publication": publication,
    }
    return TaskEvent(**values)


class AgentKernelStateReducerTest(unittest.TestCase):
    def test_task_acceptance_is_the_only_transition_without_prior_state(self) -> None:
        accepted = reduce_task(
            None,
            _event(TaskEventKind.TASK_ACCEPTED),
            TransitionFacts.permissive(),
        )

        self.assertEqual(accepted.lifecycle, "QUEUED")
        self.assertEqual(accepted.task_version, 1)
        for kind in TASK_EVENT_KINDS:
            if kind == TaskEventKind.TASK_ACCEPTED:
                continue
            with self.subTest(kind=kind), self.assertRaisesRegex(
                StateTransitionError, "STATE_TRANSITION_INVALID"
            ):
                reduce_task(None, _event(kind), TransitionFacts.permissive())

    def test_cartesian_task_state_event_matrix_accepts_exactly_listed_pairs(self) -> None:
        accepted_pairs = {
            ("QUEUED", TaskEventKind.ATTEMPT_CLAIMED),
            ("ACTIVE", TaskEventKind.INTERACTION_REQUESTED),
            ("ACTIVE", TaskEventKind.COMPLETION_PROPOSED),
            ("WAITING_INPUT", TaskEventKind.INTERACTION_RESPONDED),
            ("WAITING_APPROVAL", TaskEventKind.INTERACTION_RESPONDED),
            ("WAITING_INPUT", TaskEventKind.INTERACTION_EXPIRED),
            ("WAITING_APPROVAL", TaskEventKind.INTERACTION_EXPIRED),
            ("QUEUED", TaskEventKind.TERMINALIZATION_REQUESTED),
            ("ACTIVE", TaskEventKind.TERMINALIZATION_REQUESTED),
            ("WAITING_INPUT", TaskEventKind.TERMINALIZATION_REQUESTED),
            ("WAITING_APPROVAL", TaskEventKind.TERMINALIZATION_REQUESTED),
            ("FINALIZING", TaskEventKind.TERMINALIZATION_REQUESTED),
            ("FINALIZING", TaskEventKind.GATE_REPAIRABLE),
            ("FINALIZING", TaskEventKind.VERIFICATION_INFRASTRUCTURE_RETRY),
            ("FINALIZING", TaskEventKind.RESULT_PUBLISHED),
        }
        for lifecycle in TASK_LIFECYCLES:
            if lifecycle != "TERMINAL":
                accepted_pairs.update(
                    {
                        (lifecycle, TaskEventKind.CANCEL_REQUESTED),
                        (lifecycle, TaskEventKind.CANCEL_FINALIZED),
                        (lifecycle, TaskEventKind.OUTER_LEASE_FENCED),
                    }
                )

        for lifecycle, kind in itertools.product(TASK_LIFECYCLES, TASK_EVENT_KINDS):
            if kind == TaskEventKind.TASK_ACCEPTED:
                expected = False
            else:
                expected = (lifecycle, kind) in accepted_pairs
            with self.subTest(lifecycle=lifecycle, kind=kind):
                if expected:
                    transition = reduce_task(
                        _state(
                            lifecycle,
                            desired_state=(
                                "CANCEL"
                                if kind == TaskEventKind.CANCEL_FINALIZED
                                else "RUN"
                            ),
                        ),
                        _event(kind),
                        TransitionFacts.permissive(),
                    )
                    self.assertEqual(8, transition.task_version)
                else:
                    code = (
                        "TASK_ALREADY_TERMINAL"
                        if lifecycle == "TERMINAL"
                        else "STATE_TRANSITION_INVALID"
                    )
                    with self.assertRaisesRegex(StateTransitionError, code):
                        reduce_task(
                            _state(lifecycle),
                            _event(kind),
                            TransitionFacts.permissive(),
                        )

    def test_transition_outputs_bind_lifecycle_epoch_attempt_and_terminal_kind(self) -> None:
        claim = reduce_task(
            _state("QUEUED"),
            _event(TaskEventKind.ATTEMPT_CLAIMED),
            TransitionFacts.permissive(),
        )
        self.assertEqual(("ACTIVE", 3, "attempt_" + "c" * 32), (
            claim.lifecycle,
            claim.native_epoch,
            claim.current_attempt_id,
        ))
        self.assertEqual("CREATE_LEASED", claim.attempt_action)

        requested = reduce_task(
            _state("ACTIVE"),
            _event(TaskEventKind.INTERACTION_REQUESTED),
            TransitionFacts.permissive(),
        )
        self.assertEqual("WAITING_INPUT", requested.lifecycle)
        self.assertEqual("SUSPEND_CURRENT", requested.attempt_action)

        cancel = reduce_task(
            _state("ACTIVE"),
            _event(TaskEventKind.CANCEL_REQUESTED),
            TransitionFacts.permissive(),
        )
        self.assertEqual(("ACTIVE", "CANCEL"), (cancel.lifecycle, cancel.desired_state))

        abandoned = reduce_task(
            _state("ACTIVE"),
            _event(TaskEventKind.OUTER_LEASE_FENCED),
            TransitionFacts.permissive(),
        )
        self.assertEqual(("TERMINAL", "transport_abandoned"), (
            abandoned.lifecycle,
            abandoned.terminal_kind,
        ))

    def test_guards_and_terminalization_reason_are_fail_closed(self) -> None:
        cases = (
            ("QUEUED", TaskEventKind.ATTEMPT_CLAIMED),
            ("ACTIVE", TaskEventKind.INTERACTION_REQUESTED),
            ("ACTIVE", TaskEventKind.COMPLETION_PROPOSED),
            ("FINALIZING", TaskEventKind.RESULT_PUBLISHED),
        )
        for lifecycle, kind in cases:
            with self.subTest(kind=kind), self.assertRaisesRegex(
                StateTransitionError, "STATE_TRANSITION_INVALID"
            ):
                reduce_task(_state(lifecycle), _event(kind), TransitionFacts())

        invalid_reason = _event(TaskEventKind.TERMINALIZATION_REQUESTED)
        invalid_reason = TaskEvent(
            **{**invalid_reason.as_payload(), "terminalization_reason": "AGENT_GUESSED"}
        )
        with self.assertRaisesRegex(StateTransitionError, "STATE_TRANSITION_INVALID"):
            reduce_task(
                _state("ACTIVE"), invalid_reason, TransitionFacts.permissive()
            )

    def test_finalizing_terminalization_transactions_each_advance_one_version(self) -> None:
        event = _event(TaskEventKind.TERMINALIZATION_REQUESTED)
        fact_only = reduce_task(
            _state("FINALIZING"),
            event,
            TransitionFacts(authoritative_terminalization=True),
        )
        changed = reduce_task(
            _state("FINALIZING"),
            event,
            TransitionFacts(
                authoritative_terminalization=True,
                terminal_outcome_changed=True,
            ),
        )

        self.assertEqual(8, fact_only.task_version)
        self.assertEqual(8, changed.task_version)
        self.assertEqual("FINALIZING", fact_only.lifecycle)
        self.assertIsNone(fact_only.terminalization_reason)

    def test_cartesian_attempt_matrix_matches_the_frozen_edge_set(self) -> None:
        expected_transitions = frozenset(
            {
                (AttemptState.CREATED, AttemptState.LEASED),
                (AttemptState.CREATED, AttemptState.FENCED),
                (AttemptState.LEASED, AttemptState.PREPARING),
                (AttemptState.LEASED, AttemptState.FAILED),
                (AttemptState.LEASED, AttemptState.CANCELLED),
                (AttemptState.LEASED, AttemptState.FENCED),
                (AttemptState.PREPARING, AttemptState.RUNNING),
                (AttemptState.PREPARING, AttemptState.FAILED),
                (AttemptState.PREPARING, AttemptState.CANCELLED),
                (AttemptState.PREPARING, AttemptState.FENCED),
                (AttemptState.RUNNING, AttemptState.VERIFYING),
                (AttemptState.RUNNING, AttemptState.SUSPENDING),
                (AttemptState.RUNNING, AttemptState.FAILED),
                (AttemptState.RUNNING, AttemptState.CANCELLED),
                (AttemptState.RUNNING, AttemptState.FENCED),
                (AttemptState.VERIFYING, AttemptState.SUSPENDING),
                (AttemptState.VERIFYING, AttemptState.RUNNING),
                (AttemptState.VERIFYING, AttemptState.PUBLISHING),
                (AttemptState.VERIFYING, AttemptState.FAILED),
                (AttemptState.VERIFYING, AttemptState.CANCELLED),
                (AttemptState.VERIFYING, AttemptState.FENCED),
                (AttemptState.SUSPENDING, AttemptState.SUSPENDED),
                (AttemptState.PUBLISHING, AttemptState.RUNNING),
                (AttemptState.PUBLISHING, AttemptState.SUCCEEDED),
                (AttemptState.PUBLISHING, AttemptState.FAILED),
                (AttemptState.PUBLISHING, AttemptState.CANCELLED),
                (AttemptState.PUBLISHING, AttemptState.FENCED),
            }
        )
        self.assertEqual(expected_transitions, ATTEMPT_TRANSITIONS)
        for source, target in itertools.product(ATTEMPT_STATES, repeat=2):
            with self.subTest(source=source, target=target):
                if (source, target) in expected_transitions:
                    self.assertEqual(target, reduce_attempt(source, target))
                else:
                    with self.assertRaisesRegex(
                        StateTransitionError, "STATE_TRANSITION_INVALID"
                    ):
                        reduce_attempt(source, target)

        for terminal in (
            AttemptState.SUCCEEDED,
            AttemptState.SUSPENDED,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.FENCED,
        ):
            self.assertFalse(any(source == terminal for source, _ in expected_transitions))


if __name__ == "__main__":
    unittest.main()
