"""SQLite CAS owner for Agent Kernel Task, Attempt, version, and epoch state."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from .agent_kernel_attempt_store import (
    advance_attempt_tx,
    apply_task_attempt_action,
    attempt_required,
)
from .agent_kernel_canonical import canonical_bytes, canonical_sha256
from .agent_kernel_database import AgentKernelDatabase
from .agent_kernel_event_log import append_task_event, idempotent_event_version
from .agent_kernel_state import (
    ATTEMPT_TERMINAL_STATES,
    AttemptState,
    StateTransitionError,
    TaskEvent,
    TaskEventKind,
    TaskState,
    TaskTransition,
    TransitionFacts,
    reduce_attempt,
    reduce_task,
)
from .agent_kernel_task_records import (
    ActorFence,
    AttemptSnapshot,
    OwnerReceipt,
    TaskSnapshot,
    TaskStoreError,
    TransitionReceipt,
)


TASK_COLUMNS = (
    "task_id", "task_type", "scan_id", "request_ref", "request_digest",
    "policy_ref", "policy_digest", "policy_version", "protocol_mode", "lifecycle",
    "desired_state", "task_version", "deletion_version", "outer_job_id", "run_id",
    "lease_id", "transport_epoch", "native_epoch", "current_attempt_id", "owner_id",
    "owner_epoch", "ledger_version", "ledger_head_digest", "charter_version",
    "charter_ref", "current_checkpoint_generation", "current_checkpoint_hash",
    "quality_risk", "absolute_deadline_at", "terminalization_reserve_ms",
    "completion_proposal_ref", "final_observation_manifest_ref", "terminal_kind",
    "result_ref", "result_digest", "outcome", "created_at", "updated_at",
    "terminal_at", "terminalization_reason",
)

REFERENCE_COLUMNS = {
    "request_ref", "policy_ref", "charter_ref", "completion_proposal_ref",
    "final_observation_manifest_ref", "result_ref",
}

OWNER_EVENT = "owner.incarnation_started"


def _ref_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return canonical_bytes(value).decode("utf-8")


def _task(row: sqlite3.Row) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(row["task_id"]), lifecycle=str(row["lifecycle"]),
        desired_state=str(row["desired_state"]), task_version=int(row["task_version"]),
        deletion_version=int(row["deletion_version"]), lease_id=row["lease_id"],
        transport_epoch=row["transport_epoch"], native_epoch=int(row["native_epoch"]),
        current_attempt_id=row["current_attempt_id"], owner_id=str(row["owner_id"]),
        owner_epoch=int(row["owner_epoch"]), terminal_kind=row["terminal_kind"],
        result_ref=row["result_ref"], result_digest=row["result_digest"],
        outcome=row["outcome"], terminalization_reason=row["terminalization_reason"],
        terminal_at=row["terminal_at"],
    )


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
                self._validate_genesis(record, accepted)
                values = dict(record)
                values.update({"scan_id": scan_id, "terminalization_reason": None})
                for column in REFERENCE_COLUMNS:
                    values[column] = _ref_text(values.get(column))
                connection.execute(
                    f"INSERT INTO tasks({','.join(TASK_COLUMNS)}) VALUES({','.join('?' for _ in TASK_COLUMNS)})",
                    tuple(values.get(column) for column in TASK_COLUMNS),
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
                    self._assert_fence(current, fence)
                transition = self._reduce(current, event, facts)
                apply_task_attempt_action(connection, current, transition, event)
                publication = event.publication if transition.terminal_kind == "task_result" else None
                if publication is not None:
                    self._validate_publication(event, publication.attempt_terminal_state)
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
                if fence is not None:
                    self._assert_fence(task, fence)
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
                    connection.commit()
                    return OwnerReceipt(task, task.owner_epoch, session_id, False)
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
                connection.execute(
                    """UPDATE tasks SET owner_epoch=?,task_version=?,updated_at=?
                       WHERE task_id=? AND task_version=?""",
                    (owner_epoch, next_version, occurred_at, task_id, expected_task_version),
                )
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
            self._assert_fence(task, fence)
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
    def _validate_genesis(record: Mapping[str, object], transition: TaskTransition) -> None:
        expected = (
            transition.lifecycle, transition.desired_state, transition.task_version,
            transition.native_epoch, transition.current_attempt_id,
        )
        observed = tuple(record.get(key) for key in (
            "lifecycle", "desired_state", "task_version", "native_epoch",
            "current_attempt_id",
        ))
        if observed != expected:
            raise TaskStoreError("CONTRACT_INVALID", "task genesis state")

    @staticmethod
    def _reduce(
        current: TaskSnapshot, event: TaskEvent, facts: TransitionFacts
    ) -> TaskTransition:
        try:
            return reduce_task(
                TaskState(
                    current.lifecycle, current.desired_state, current.task_version,
                    current.native_epoch, current.current_attempt_id,
                    current.terminalization_reason,
                ),
                event,
                facts,
            )
        except StateTransitionError as exc:
            raise TaskStoreError(exc.code, exc.detail) from exc

    @staticmethod
    def _validate_publication(event: TaskEvent, target: str) -> None:
        publication = event.publication
        if publication is None or (
            len(publication.result_digest) != 64
            or publication.result_digest.lower() != publication.result_digest
            or not publication.result_ref
            or not publication.outcome
            or target not in ATTEMPT_TERMINAL_STATES
        ):
            raise TaskStoreError("CONTRACT_INVALID", "terminal publication")
        if event.kind == TaskEventKind.CANCEL_FINALIZED and target != AttemptState.CANCELLED:
            raise TaskStoreError("CONTRACT_INVALID", "cancel attempt outcome")

    @staticmethod
    def _assert_fence(task: TaskSnapshot, fence: ActorFence) -> None:
        if task.lifecycle == "TERMINAL":
            raise TaskStoreError("TASK_ALREADY_TERMINAL")
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

    @staticmethod
    def _task_required(connection: sqlite3.Connection, task_id: str) -> TaskSnapshot:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskStoreError("CONTRACT_INVALID", "task not found")
        return _task(row)

__all__ = [
    "ActorFence", "AttemptSnapshot", "OwnerReceipt", "TaskSnapshot",
    "TaskStore", "TaskStoreError", "TransitionReceipt",
]
