"""Task Store contract checks that sit outside SQLite mechanics."""

from __future__ import annotations

import re
from typing import Mapping

from .agent_kernel_state import (
    ATTEMPT_TERMINAL_STATES,
    AttemptState,
    StateTransitionError,
    TaskEvent,
    TaskEventKind,
    TaskState,
    TaskTransition,
    TransitionFacts,
    reduce_task,
)
from .agent_kernel_task_records import TaskSnapshot, TaskStoreError


def validate_task_genesis(
    record: Mapping[str, object], transition: TaskTransition
) -> None:
    expected = (
        transition.lifecycle,
        transition.desired_state,
        transition.task_version,
        transition.native_epoch,
        transition.current_attempt_id,
    )
    observed = tuple(
        record.get(key)
        for key in (
            "lifecycle",
            "desired_state",
            "task_version",
            "native_epoch",
            "current_attempt_id",
        )
    )
    if observed != expected:
        raise TaskStoreError("CONTRACT_INVALID", "task genesis state")


def reduce_task_snapshot(
    current: TaskSnapshot,
    event: TaskEvent,
    facts: TransitionFacts,
) -> TaskTransition:
    try:
        return reduce_task(
            TaskState(
                current.lifecycle,
                current.desired_state,
                current.task_version,
                current.native_epoch,
                current.current_attempt_id,
                current.terminalization_reason,
            ),
            event,
            facts,
        )
    except StateTransitionError as exc:
        raise TaskStoreError(exc.code, exc.detail) from exc


def validate_terminal_publication(event: TaskEvent) -> None:
    publication = event.publication
    if publication is None or (
        re.fullmatch(r"[0-9a-f]{64}", publication.result_digest) is None
        or not publication.result_ref
        or not publication.outcome
        or publication.attempt_terminal_state not in ATTEMPT_TERMINAL_STATES
    ):
        raise TaskStoreError("CONTRACT_INVALID", "terminal publication")
    if (
        event.kind == TaskEventKind.CANCEL_FINALIZED
        and publication.attempt_terminal_state != AttemptState.CANCELLED
    ):
        raise TaskStoreError("CONTRACT_INVALID", "cancel attempt outcome")
