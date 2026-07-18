"""Typed records shared by the Agent Kernel Task Store boundary."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Mapping

from .agent_kernel_canonical import canonical_bytes


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


class TaskStoreError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    lifecycle: str
    desired_state: str
    task_version: int
    deletion_version: int
    lease_id: str | None
    transport_epoch: int | None
    native_epoch: int
    current_attempt_id: str | None
    owner_id: str
    owner_epoch: int
    terminal_kind: str | None
    result_ref: str | None
    result_digest: str | None
    outcome: str | None
    terminalization_reason: str | None
    terminal_at: str | None


@dataclass(frozen=True)
class AttemptSnapshot:
    attempt_id: str
    task_id: str
    native_epoch: int
    state: str
    state_version: int
    transport_epoch: int | None
    predecessor_checkpoint_generation: int | None
    owner_session_id: str | None
    lease_acquired_at: str | None
    started_at: str | None
    ended_at: str | None
    termination_reason: str | None
    budget_reservation_id: str | None


@dataclass(frozen=True)
class ActorFence:
    task_version: int
    deletion_version: int
    lease_id: str | None
    transport_epoch: int | None
    attempt_id: str | None
    native_epoch: int
    owner_id: str
    owner_epoch: int
    owner_session_id: str

    @classmethod
    def from_task(
        cls, task: TaskSnapshot, *, owner_session_id: str
    ) -> "ActorFence":
        return cls(
            task_version=task.task_version,
            deletion_version=task.deletion_version,
            lease_id=task.lease_id,
            transport_epoch=task.transport_epoch,
            attempt_id=task.current_attempt_id,
            native_epoch=task.native_epoch,
            owner_id=task.owner_id,
            owner_epoch=task.owner_epoch,
            owner_session_id=owner_session_id,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "task_version": self.task_version,
            "deletion_version": self.deletion_version,
            "lease_id": self.lease_id,
            "transport_epoch": self.transport_epoch,
            "attempt_id": self.attempt_id,
            "native_epoch": self.native_epoch,
            "owner_id": self.owner_id,
            "owner_epoch": self.owner_epoch,
            "owner_session_id": self.owner_session_id,
        }


@dataclass(frozen=True)
class TransitionReceipt:
    task: TaskSnapshot
    event_task_version: int
    applied: bool


@dataclass(frozen=True)
class OwnerReceipt:
    task: TaskSnapshot
    owner_epoch: int
    session_id: str
    applied: bool


def task_from_row(row: sqlite3.Row) -> TaskSnapshot:
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


def task_insert_values(
    record: Mapping[str, object], scan_id: str | None
) -> tuple[object, ...]:
    values = dict(record)
    values.update({"scan_id": scan_id, "terminalization_reason": None})
    for column in REFERENCE_COLUMNS:
        value = values.get(column)
        if value is not None and not isinstance(value, str):
            values[column] = canonical_bytes(value).decode("utf-8")
    return tuple(values.get(column) for column in TASK_COLUMNS)
