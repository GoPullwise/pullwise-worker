"""Typed records shared by the Agent Kernel Task Store boundary."""

from __future__ import annotations

from dataclasses import dataclass


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

    @classmethod
    def from_task(cls, task: TaskSnapshot) -> "ActorFence":
        return cls(
            task_version=task.task_version,
            deletion_version=task.deletion_version,
            lease_id=task.lease_id,
            transport_epoch=task.transport_epoch,
            attempt_id=task.current_attempt_id,
            native_epoch=task.native_epoch,
            owner_id=task.owner_id,
            owner_epoch=task.owner_epoch,
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
