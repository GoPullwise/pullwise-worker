"""AttemptRecord persistence and legal edge application."""

from __future__ import annotations

import sqlite3

from .agent_kernel_canonical import canonical_bytes
from .agent_kernel_state import (
    ATTEMPT_TERMINAL_STATES,
    AttemptState,
    StateTransitionError,
    TaskEvent,
    TaskTransition,
    reduce_attempt,
)
from .agent_kernel_task_records import (
    AttemptSnapshot,
    TaskSnapshot,
    TaskStoreError,
)


def attempt_from_row(row: sqlite3.Row) -> AttemptSnapshot:
    return AttemptSnapshot(
        attempt_id=str(row["attempt_id"]),
        task_id=str(row["task_id"]),
        native_epoch=int(row["native_epoch"]),
        state=str(row["state"]),
        state_version=int(row["state_version"]),
        transport_epoch=row["transport_epoch"],
        predecessor_checkpoint_generation=row["predecessor_checkpoint_generation"],
        owner_session_id=row["owner_session_id"],
        lease_acquired_at=row["lease_acquired_at"],
        started_at=row["started_at"],
        ended_at=row["terminal_at"],
        termination_reason=row["terminal_reason"],
        budget_reservation_id=row["budget_reservation_id"],
    )


def attempt_required(
    connection: sqlite3.Connection, attempt_id: str
) -> AttemptSnapshot:
    row = connection.execute(
        "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise TaskStoreError("ATTEMPT_NOT_STARTED")
    return attempt_from_row(row)


def advance_attempt_tx(
    connection: sqlite3.Connection,
    attempt_id: str,
    expected_state_version: int,
    target_state: str,
    occurred_at: str,
    termination_reason: str | None,
) -> AttemptSnapshot:
    current = attempt_required(connection, attempt_id)
    if current.state_version != expected_state_version:
        raise TaskStoreError("TASK_VERSION_STALE", "attempt state version")
    try:
        reduce_attempt(current.state, target_state)
    except StateTransitionError as exc:
        raise TaskStoreError(exc.code, exc.detail) from exc
    terminal = target_state in ATTEMPT_TERMINAL_STATES
    started_at = current.started_at or (
        occurred_at if target_state == AttemptState.RUNNING else None
    )
    ended_at = occurred_at if terminal else None
    reason = (termination_reason or target_state) if terminal else None
    changed = connection.execute(
        """UPDATE attempts SET state=?,state_version=?,started_at=?,terminal_at=?,
           terminal_reason=? WHERE attempt_id=? AND state_version=?""",
        (
            target_state,
            expected_state_version + 1,
            started_at,
            ended_at,
            reason,
            attempt_id,
            expected_state_version,
        ),
    )
    if changed.rowcount != 1:
        raise TaskStoreError("TASK_VERSION_STALE", "attempt state version")
    return attempt_required(connection, attempt_id)


def apply_task_attempt_action(
    connection: sqlite3.Connection,
    task: TaskSnapshot,
    transition: TaskTransition,
    event: TaskEvent,
) -> None:
    if transition.attempt_action == "CREATE_LEASED":
        binding = canonical_bytes(
            {"lease_id": task.lease_id, "transport_epoch": task.transport_epoch}
        ).decode("utf-8")
        connection.execute(
            """INSERT INTO attempts(
               attempt_id,task_id,native_epoch,state,transport_epoch,started_at,
               terminal_at,terminal_reason,transport_binding,state_version,
               predecessor_checkpoint_generation,owner_session_id,lease_acquired_at,
               budget_reservation_id) VALUES(?,?,?,?,?,NULL,NULL,NULL,?,1,?,NULL,?,?)""",
            (
                event.attempt_id,
                task.task_id,
                transition.native_epoch,
                AttemptState.LEASED,
                task.transport_epoch,
                binding,
                event.predecessor_checkpoint_generation,
                event.occurred_at,
                event.budget_reservation_id,
            ),
        )
        return
    attempt_id = task.current_attempt_id
    if transition.attempt_action in {"NONE", "KEEP_CURRENT"} or attempt_id is None:
        return
    current = attempt_required(connection, attempt_id)
    if current.state in ATTEMPT_TERMINAL_STATES:
        return
    if transition.attempt_action == "SUSPEND_CURRENT":
        target, reason = AttemptState.SUSPENDED, "INTERACTION_REQUESTED"
    elif transition.attempt_action == "FENCE_CURRENT":
        target, reason = (
            AttemptState.FENCED,
            event.terminalization_reason or "FENCED",
        )
    elif transition.attempt_action == "TERMINALIZE_CURRENT":
        if event.publication is None:
            raise TaskStoreError("CONTRACT_INVALID", "attempt terminal target")
        target = event.publication.attempt_terminal_state
        reason = target
    else:
        raise TaskStoreError("STATE_TRANSITION_INVALID", "attempt action")
    advance_attempt_tx(
        connection,
        attempt_id,
        current.state_version,
        target,
        event.occurred_at,
        reason,
    )
