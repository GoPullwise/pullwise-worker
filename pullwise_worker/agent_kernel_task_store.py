"""SQLite CAS owner for Agent Kernel Task, Attempt, version, and epoch state."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from .agent_kernel_attempt_store import (
    advance_attempt_tx,
    apply_task_attempt_action,
    attempt_required,
)
from .agent_kernel_canonical import canonical_sha256
from .agent_kernel_database import AgentKernelDatabase
from .agent_kernel_event_log import append_task_event, idempotent_event_version
from .agent_kernel_fencing import assert_actor_fence
from .agent_kernel_state import (
    TaskEvent,
    TaskEventKind,
    TransitionFacts,
    reduce_task,
)
from .agent_kernel_task_records import (
    ActorFence,
    AttemptSnapshot,
    OwnerReceipt,
    TaskSnapshot,
    TaskStoreError,
    TransitionReceipt,
    TASK_COLUMNS,
    task_from_row,
    task_insert_values,
)
from .agent_kernel_task_validation import (
    reduce_task_snapshot,
    validate_task_genesis,
    validate_terminal_publication,
)


OWNER_EVENT = "owner.incarnation_started"


class TaskStore:
    def __init__(self, database: AgentKernelDatabase) -> None:
        self.database = database

    def accept_task(
        self,
        record: Mapping[str, object],
        *,
        idempotency_key: str,
        scan_id: str | None = None,
    ) -> TransitionReceipt:
        task_id = str(record.get("task_id") or "")
        digest = canonical_sha256(dict(record))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                retry = idempotent_event_version(
                    connection, task_id, TaskEventKind.TASK_ACCEPTED,
                    idempotency_key, digest,
                )
                if retry is not None:
                    receipt = TransitionReceipt(
                        self._task_required(connection, task_id), retry, False
                    )
                    connection.commit()
                    return receipt
                if connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone():
                    raise TaskStoreError("TASK_ID_COLLISION")
                accepted = reduce_task(
                    None,
                    TaskEvent(
                        TaskEventKind.TASK_ACCEPTED, idempotency_key,
                        str(record.get("created_at") or ""),
                    ),
                    TransitionFacts(request_policy_ledger_durable=True),
                )
                validate_task_genesis(record, accepted)
                connection.execute(
                    f"INSERT INTO tasks({','.join(TASK_COLUMNS)}) VALUES({','.join('?' for _ in TASK_COLUMNS)})",
                    task_insert_values(record, scan_id),
                )
                append_task_event(
                    connection, task_id, TaskEventKind.TASK_ACCEPTED,
                    idempotency_key, digest, 1, str(record.get("created_at") or ""),
                )
                receipt = TransitionReceipt(self._task_required(connection, task_id), 1, True)
                connection.commit()
                return receipt
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise TaskStoreError("CONTRACT_INVALID", str(exc)) from exc
            except BaseException:
                connection.rollback()
                raise

    def apply_event(
        self,
        task_id: str,
        *,
        expected_task_version: int,
        event: TaskEvent,
        facts: TransitionFacts,
        fence: ActorFence | None = None,
    ) -> TransitionReceipt:
        digest = canonical_sha256(event.digest_payload())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                retry = idempotent_event_version(
                    connection, task_id, event.kind, event.idempotency_key, digest
                )
                if retry is not None:
                    receipt = TransitionReceipt(
                        self._task_required(connection, task_id), retry, False
                    )
                    connection.commit()
                    return receipt
                current = self._task_required(connection, task_id)
                if current.lifecycle == "TERMINAL":
                    raise TaskStoreError("TASK_ALREADY_TERMINAL")
                if current.task_version != expected_task_version:
                    raise TaskStoreError("TASK_VERSION_STALE")
                if fence is not None:
                    assert_actor_fence(connection, current, fence)
                transition = reduce_task_snapshot(current, event, facts)
                if transition.task_version != current.task_version + 1:
                    raise TaskStoreError(
                        "STATE_TRANSITION_INVALID",
                        "control transaction must advance task_version exactly once",
                    )
                publication = event.publication if transition.terminal_kind == "task_result" else None
                if publication is not None:
                    validate_terminal_publication(event)
                apply_task_attempt_action(connection, current, transition, event)
                terminal_at = (
                    publication.published_at if publication is not None
                    else event.occurred_at if transition.terminal_kind else None
                )
                result_ref = publication.result_ref if publication is not None else None
                result_digest = publication.result_digest if publication is not None else None
                outcome = publication.outcome if publication is not None else None
                cursor = connection.execute(
                    """UPDATE tasks SET lifecycle=?,desired_state=?,task_version=?,native_epoch=?,
                       current_attempt_id=?,terminal_kind=?,result_ref=?,result_digest=?,outcome=?,
                       updated_at=?,terminal_at=?,terminalization_reason=?
                       WHERE task_id=? AND task_version=?""",
                    (
                        transition.lifecycle, transition.desired_state,
                        transition.task_version, transition.native_epoch,
                        transition.current_attempt_id, transition.terminal_kind,
                        result_ref, result_digest, outcome, event.occurred_at,
                        terminal_at, transition.terminalization_reason,
                        task_id, expected_task_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskStoreError("TASK_VERSION_STALE")
                if publication is not None:
                    connection.execute(
                        """INSERT INTO result_publications(
                           task_id,result_digest,result_ref,published_from_version,
                           terminal_task_version,created_at) VALUES(?,?,?,?,?,?)""",
                        (
                            task_id, publication.result_digest, publication.result_ref,
                            expected_task_version, transition.task_version,
                            publication.published_at,
                        ),
                    )
                append_task_event(
                    connection, task_id, event.kind, event.idempotency_key, digest,
                    transition.task_version, event.occurred_at,
                )
                receipt = TransitionReceipt(
                    self._task_required(connection, task_id),
                    transition.task_version,
                    True,
                )
                connection.commit()
                return receipt
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                code = "RESULT_CONFLICT" if event.publication is not None else "CONTRACT_INVALID"
                raise TaskStoreError(code, str(exc)) from exc
            except BaseException:
                connection.rollback()
                raise

    def advance_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        expected_state_version: int,
        target_state: str,
        occurred_at: str,
        termination_reason: str | None = None,
        fence: ActorFence | None = None,
    ) -> AttemptSnapshot:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = self._task_required(connection, task_id)
                if task.lifecycle == "TERMINAL":
                    raise TaskStoreError("TASK_ALREADY_TERMINAL")
                if fence is not None:
                    assert_actor_fence(connection, task, fence)
                current_attempt = attempt_required(connection, attempt_id)
                if (
                    task.current_attempt_id != attempt_id
                    or task.native_epoch != current_attempt.native_epoch
                ):
                    raise TaskStoreError("NATIVE_EPOCH_STALE")
                result = advance_attempt_tx(
                    connection, attempt_id, expected_state_version, target_state,
                    occurred_at, termination_reason,
                )
                if result.task_id != task_id:
                    raise TaskStoreError("NATIVE_EPOCH_STALE")
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def begin_owner_incarnation(
        self,
        task_id: str,
        *,
        expected_task_version: int,
        attempt_id: str,
        native_epoch: int,
        session_id: str,
        idempotency_key: str,
        occurred_at: str,
    ) -> OwnerReceipt:
        payload = {
            "event": OWNER_EVENT, "attempt_id": attempt_id,
            "native_epoch": native_epoch, "session_id": session_id,
        }
        digest = canonical_sha256(payload)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                retry = idempotent_event_version(
                    connection, task_id, OWNER_EVENT, idempotency_key, digest
                )
                if retry is not None:
                    task = self._task_required(connection, task_id)
                    owner = connection.execute(
                        """SELECT owner_epoch FROM owner_incarnations
                           WHERE session_id=? AND task_id=?""",
                        (session_id, task_id),
                    ).fetchone()
                    if owner is None:
                        raise TaskStoreError("OWNER_EPOCH_STALE")
                    connection.commit()
                    return OwnerReceipt(task, int(owner["owner_epoch"]), session_id, False)
                task = self._task_required(connection, task_id)
                if task.lifecycle == "TERMINAL":
                    raise TaskStoreError("TASK_ALREADY_TERMINAL")
                if task.task_version != expected_task_version:
                    raise TaskStoreError("TASK_VERSION_STALE")
                if (
                    task.lifecycle != "ACTIVE" or task.desired_state != "RUN"
                    or task.current_attempt_id != attempt_id
                    or task.native_epoch != native_epoch
                ):
                    raise TaskStoreError("NATIVE_EPOCH_STALE")
                owner_epoch = task.owner_epoch + 1
                next_version = task.task_version + 1
                fenced = connection.execute(
                    """UPDATE owner_incarnations SET state='FENCED',terminal_at=?
                       WHERE task_id=? AND state='ACTIVE'""",
                    (occurred_at, task_id),
                )
                if fenced.rowcount > 1:
                    raise TaskStoreError(
                        "STATE_TRANSITION_INVALID", "multiple live owners"
                    )
                connection.execute(
                    """INSERT INTO owner_incarnations(
                       session_id,task_id,owner_id,owner_epoch,state,started_at,terminal_at)
                       VALUES(?,?,?,?,?,?,NULL)""",
                    (session_id, task_id, task.owner_id, owner_epoch, "ACTIVE", occurred_at),
                )
                changed = connection.execute(
                    "UPDATE attempts SET owner_session_id=? WHERE attempt_id=? AND native_epoch=?",
                    (session_id, attempt_id, native_epoch),
                )
                if changed.rowcount != 1:
                    raise TaskStoreError("NATIVE_EPOCH_STALE")
                task_changed = connection.execute(
                    """UPDATE tasks SET owner_epoch=?,task_version=?,updated_at=?
                       WHERE task_id=? AND task_version=?""",
                    (owner_epoch, next_version, occurred_at, task_id, expected_task_version),
                )
                if task_changed.rowcount != 1:
                    raise TaskStoreError("TASK_VERSION_STALE")
                append_task_event(
                    connection, task_id, OWNER_EVENT, idempotency_key, digest,
                    next_version, occurred_at,
                )
                result = OwnerReceipt(
                    self._task_required(connection, task_id), owner_epoch, session_id, True
                )
                connection.commit()
                return result
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise TaskStoreError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
            except BaseException:
                connection.rollback()
                raise

    def assert_fresh_actor(self, task_id: str, fence: ActorFence) -> TaskSnapshot:
        with self.database.connect() as connection:
            task = self._task_required(connection, task_id)
            assert_actor_fence(connection, task, fence)
            return task

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self.database.connect() as connection:
            return self._task_required(connection, task_id)

    def get_attempt(self, attempt_id: str) -> AttemptSnapshot:
        with self.database.connect() as connection:
            return attempt_required(connection, attempt_id)

    def count_attempts(self, task_id: str) -> int:
        return self._count("attempts", task_id)

    def count_publications(self, task_id: str) -> int:
        return self._count("result_publications", task_id)

    def _count(self, table: str, task_id: str) -> int:
        if table not in {"attempts", "result_publications"}:
            raise ValueError("unsupported count table")
        with self.database.connect() as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (task_id,)
            ).fetchone()[0])

    @staticmethod
    def _task_required(connection: sqlite3.Connection, task_id: str) -> TaskSnapshot:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskStoreError("CONTRACT_INVALID", "task not found")
        return task_from_row(row)

__all__ = [
    "ActorFence", "AttemptSnapshot", "OwnerReceipt", "TaskSnapshot",
    "TaskStore", "TaskStoreError", "TransitionReceipt",
]
