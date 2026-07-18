"""Fail-closed Task, lease, native Attempt, and Owner epoch checks."""

from __future__ import annotations

import sqlite3

from .agent_kernel_state import ATTEMPT_TERMINAL_STATES
from .agent_kernel_task_records import ActorFence, TaskSnapshot, TaskStoreError


def assert_actor_fence(
    connection: sqlite3.Connection,
    task: TaskSnapshot,
    fence: ActorFence,
) -> None:
    if task.lifecycle == "TERMINAL":
        raise TaskStoreError("TASK_ALREADY_TERMINAL")
    if task.desired_state != "RUN":
        raise TaskStoreError("TASK_VERSION_STALE", "task no longer desires RUN")
    if (
        task.task_version != fence.task_version
        or task.deletion_version != fence.deletion_version
    ):
        raise TaskStoreError("TASK_VERSION_STALE")
    if task.lease_id != fence.lease_id or task.transport_epoch != fence.transport_epoch:
        raise TaskStoreError("LEASE_INVALID")
    if (
        task.native_epoch != fence.native_epoch
        or task.current_attempt_id != fence.attempt_id
    ):
        raise TaskStoreError("NATIVE_EPOCH_STALE")
    if task.owner_id != fence.owner_id or task.owner_epoch != fence.owner_epoch:
        raise TaskStoreError("OWNER_EPOCH_STALE")
    attempt = connection.execute(
        "SELECT state,owner_session_id FROM attempts WHERE attempt_id=?",
        (task.current_attempt_id,),
    ).fetchone()
    if (
        attempt is None
        or attempt["state"] in ATTEMPT_TERMINAL_STATES
        or attempt["owner_session_id"] != fence.owner_session_id
    ):
        raise TaskStoreError("OWNER_EPOCH_STALE")
    owner = connection.execute(
        """SELECT state FROM owner_incarnations
           WHERE session_id=? AND task_id=? AND owner_id=? AND owner_epoch=?""",
        (
            fence.owner_session_id,
            task.task_id,
            task.owner_id,
            task.owner_epoch,
        ),
    ).fetchone()
    if owner is None or owner["state"] != "ACTIVE":
        raise TaskStoreError("OWNER_EPOCH_STALE")
