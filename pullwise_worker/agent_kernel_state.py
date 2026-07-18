"""Typed, side-effect-free Task and Attempt transition reducers."""

from __future__ import annotations

from dataclasses import dataclass, fields


class TaskEventKind:
    TASK_ACCEPTED = "task.accepted"
    ATTEMPT_CLAIMED = "attempt.claimed"
    INTERACTION_REQUESTED = "interaction.requested"
    INTERACTION_RESPONDED = "interaction.responded"
    INTERACTION_EXPIRED = "interaction.expired"
    COMPLETION_PROPOSED = "completion.proposed"
    TERMINALIZATION_REQUESTED = "supervisor.terminalization_requested"
    GATE_REPAIRABLE = "gate.repairable"
    VERIFICATION_INFRASTRUCTURE_RETRY = "verification.infrastructure_retry"
    RESULT_PUBLISHED = "result.published"
    CANCEL_REQUESTED = "cancel.requested"
    CANCEL_FINALIZED = "cancel.finalized"
    OUTER_LEASE_FENCED = "outer_lease.fenced"


TASK_EVENT_KINDS = (
    TaskEventKind.TASK_ACCEPTED,
    TaskEventKind.ATTEMPT_CLAIMED,
    TaskEventKind.INTERACTION_REQUESTED,
    TaskEventKind.INTERACTION_RESPONDED,
    TaskEventKind.INTERACTION_EXPIRED,
    TaskEventKind.COMPLETION_PROPOSED,
    TaskEventKind.TERMINALIZATION_REQUESTED,
    TaskEventKind.GATE_REPAIRABLE,
    TaskEventKind.VERIFICATION_INFRASTRUCTURE_RETRY,
    TaskEventKind.RESULT_PUBLISHED,
    TaskEventKind.CANCEL_REQUESTED,
    TaskEventKind.CANCEL_FINALIZED,
    TaskEventKind.OUTER_LEASE_FENCED,
)

TASK_LIFECYCLES = (
    "QUEUED",
    "ACTIVE",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "FINALIZING",
    "TERMINAL",
)

TERMINALIZATION_REASONS = (
    "DEADLINE_REACHED",
    "BUDGET_EXHAUSTED",
    "INTERACTION_UNAVAILABLE",
    "CAPABILITY_UNAVAILABLE",
    "RUNTIME_FAILURE",
    "STORAGE_FAILURE",
    "PROTOCOL_FAILURE",
    "POLICY_INVARIANT_BROKEN",
)


class AttemptState:
    CREATED = "CREATED"
    LEASED = "LEASED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUSPENDING = "SUSPENDING"
    PUBLISHING = "PUBLISHING"
    SUCCEEDED = "SUCCEEDED"
    SUSPENDED = "SUSPENDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    FENCED = "FENCED"


ATTEMPT_STATES = (
    AttemptState.CREATED,
    AttemptState.LEASED,
    AttemptState.PREPARING,
    AttemptState.RUNNING,
    AttemptState.VERIFYING,
    AttemptState.SUSPENDING,
    AttemptState.PUBLISHING,
    AttemptState.SUCCEEDED,
    AttemptState.SUSPENDED,
    AttemptState.FAILED,
    AttemptState.CANCELLED,
    AttemptState.FENCED,
)

ATTEMPT_TERMINAL_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.SUSPENDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.FENCED,
    }
)

ATTEMPT_TRANSITIONS = frozenset(
    {
        (AttemptState.CREATED, AttemptState.LEASED),
        (AttemptState.CREATED, AttemptState.FENCED),
        (AttemptState.LEASED, AttemptState.PREPARING),
        (AttemptState.PREPARING, AttemptState.RUNNING),
        (AttemptState.RUNNING, AttemptState.VERIFYING),
        (AttemptState.RUNNING, AttemptState.SUSPENDING),
        (AttemptState.VERIFYING, AttemptState.SUSPENDING),
        (AttemptState.SUSPENDING, AttemptState.SUSPENDED),
        (AttemptState.VERIFYING, AttemptState.RUNNING),
        (AttemptState.VERIFYING, AttemptState.PUBLISHING),
        (AttemptState.PUBLISHING, AttemptState.RUNNING),
        (AttemptState.PUBLISHING, AttemptState.SUCCEEDED),
        *{
            (source, target)
            for source in (AttemptState.LEASED, AttemptState.PREPARING)
            for target in (
                AttemptState.FAILED,
                AttemptState.CANCELLED,
                AttemptState.FENCED,
            )
        },
        *{
            (source, target)
            for source in (
                AttemptState.RUNNING,
                AttemptState.VERIFYING,
                AttemptState.PUBLISHING,
            )
            for target in (
                AttemptState.FAILED,
                AttemptState.CANCELLED,
                AttemptState.FENCED,
            )
        },
    }
)


class StateTransitionError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class TerminalPublication:
    result_ref: str
    result_digest: str
    outcome: str
    published_at: str
    attempt_terminal_state: str

    def as_payload(self) -> dict[str, object]:
        return {
            "result_ref": self.result_ref,
            "result_digest": self.result_digest,
            "outcome": self.outcome,
            "published_at": self.published_at,
            "attempt_terminal_state": self.attempt_terminal_state,
        }


@dataclass(frozen=True)
class TaskEvent:
    kind: str
    idempotency_key: str
    occurred_at: str
    attempt_id: str | None = None
    interaction_kind: str | None = None
    terminalization_reason: str | None = None
    budget_reservation_id: str | None = None
    predecessor_checkpoint_generation: int | None = None
    publication: TerminalPublication | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "idempotency_key": self.idempotency_key,
            "occurred_at": self.occurred_at,
            "attempt_id": self.attempt_id,
            "interaction_kind": self.interaction_kind,
            "terminalization_reason": self.terminalization_reason,
            "budget_reservation_id": self.budget_reservation_id,
            "predecessor_checkpoint_generation": self.predecessor_checkpoint_generation,
            "publication": self.publication,
        }

    def digest_payload(self) -> dict[str, object]:
        payload = self.as_payload()
        if self.publication is not None:
            payload["publication"] = self.publication.as_payload()
        return payload


@dataclass(frozen=True)
class TaskState:
    lifecycle: str
    desired_state: str
    task_version: int
    native_epoch: int
    current_attempt_id: str | None
    terminalization_reason: str | None


@dataclass(frozen=True)
class TransitionFacts:
    request_policy_ledger_durable: bool = False
    outer_lease_valid: bool = False
    budget_reserved: bool = False
    channel_supported: bool = False
    no_in_flight_write: bool = False
    response_valid: bool = False
    response_not_expired: bool = False
    deadline_reached: bool = False
    proposal_fresh: bool = False
    source_frozen: bool = False
    authoritative_terminalization: bool = False
    terminal_outcome_changed: bool = False
    tools_stopped_or_fenced: bool = False
    repair_budget_available: bool = False
    deadline_available: bool = False
    same_outer_lease: bool = False
    attempt_budget_available: bool = False
    terminal_cas_valid: bool = False
    no_in_flight_tool: bool = False
    effects_empty: bool = False
    outer_lease_invalid: bool = False

    @classmethod
    def permissive(cls) -> "TransitionFacts":
        return cls(**{item.name: True for item in fields(cls)})


@dataclass(frozen=True)
class TaskTransition:
    lifecycle: str
    desired_state: str
    task_version: int
    native_epoch: int
    current_attempt_id: str | None
    attempt_action: str
    terminal_kind: str | None = None
    terminalization_reason: str | None = None


def _invalid(detail: str) -> None:
    raise StateTransitionError("STATE_TRANSITION_INVALID", detail)


def _require(condition: bool, detail: str) -> None:
    if not condition:
        _invalid(detail)


def reduce_attempt(source: str, target: str) -> str:
    if source not in ATTEMPT_STATES or target not in ATTEMPT_STATES:
        _invalid(f"unknown attempt state: {source}->{target}")
    if (source, target) not in ATTEMPT_TRANSITIONS:
        _invalid(f"attempt edge: {source}->{target}")
    return target


def reduce_task(
    state: TaskState | None,
    event: TaskEvent,
    facts: TransitionFacts,
) -> TaskTransition:
    if state is None:
        if event.kind != TaskEventKind.TASK_ACCEPTED:
            _invalid(f"no task for {event.kind}")
        _require(facts.request_policy_ledger_durable, "acceptance roots are not durable")
        return TaskTransition("QUEUED", "RUN", 1, 0, None, "NONE")
    if state.lifecycle == "TERMINAL":
        raise StateTransitionError("TASK_ALREADY_TERMINAL")
    if event.kind == TaskEventKind.TASK_ACCEPTED:
        _invalid("task already exists")
    if state.lifecycle not in TASK_LIFECYCLES or state.desired_state not in {"RUN", "CANCEL"}:
        _invalid("invalid current task state")

    lifecycle = state.lifecycle
    desired = state.desired_state
    native_epoch = state.native_epoch
    attempt_id = state.current_attempt_id
    action = "NONE"
    terminal_kind = None
    reason = state.terminalization_reason

    if event.kind == TaskEventKind.ATTEMPT_CLAIMED:
        _require(lifecycle == "QUEUED" and desired == "RUN", "claim source")
        _require(facts.outer_lease_valid and facts.budget_reserved, "claim guards")
        _require(bool(event.attempt_id and event.budget_reservation_id), "claim identity")
        lifecycle = "ACTIVE"
        native_epoch += 1
        attempt_id = event.attempt_id
        action = "CREATE_LEASED"
    elif event.kind == TaskEventKind.INTERACTION_REQUESTED:
        _require(lifecycle == "ACTIVE" and desired == "RUN", "interaction source")
        _require(facts.channel_supported and facts.no_in_flight_write, "interaction guards")
        _require(event.interaction_kind in {"input", "approval"}, "interaction kind")
        lifecycle = "WAITING_INPUT" if event.interaction_kind == "input" else "WAITING_APPROVAL"
        action = "SUSPEND_CURRENT"
    elif event.kind == TaskEventKind.INTERACTION_RESPONDED:
        _require(lifecycle in {"WAITING_INPUT", "WAITING_APPROVAL"}, "response source")
        _require(desired == "RUN" and facts.response_valid and facts.response_not_expired, "response guards")
        lifecycle = "QUEUED"
    elif event.kind == TaskEventKind.INTERACTION_EXPIRED:
        _require(lifecycle in {"WAITING_INPUT", "WAITING_APPROVAL"}, "expiry source")
        _require(desired == "RUN" and facts.deadline_reached, "expiry guard")
        lifecycle = "FINALIZING"
        reason = "INTERACTION_UNAVAILABLE"
    elif event.kind == TaskEventKind.COMPLETION_PROPOSED:
        _require(lifecycle == "ACTIVE" and desired == "RUN", "proposal source")
        _require(facts.proposal_fresh and facts.source_frozen, "proposal guards")
        lifecycle = "FINALIZING"
        action = "KEEP_CURRENT"
    elif event.kind == TaskEventKind.TERMINALIZATION_REQUESTED:
        _require(
            lifecycle in {
                "QUEUED", "ACTIVE", "WAITING_INPUT", "WAITING_APPROVAL", "FINALIZING"
            },
            "terminalization source",
        )
        _require(event.terminalization_reason in TERMINALIZATION_REASONS, "terminalization reason")
        _require(facts.authoritative_terminalization, "terminalization authority")
        if lifecycle == "FINALIZING" and not facts.terminal_outcome_changed:
            return TaskTransition(
                lifecycle=lifecycle,
                desired_state=desired,
                task_version=state.task_version,
                native_epoch=native_epoch,
                current_attempt_id=attempt_id,
                attempt_action="NONE",
                terminalization_reason=reason,
            )
        if lifecycle == "ACTIVE":
            _require(facts.tools_stopped_or_fenced, "active tools not stopped")
            action = "KEEP_CURRENT"
        lifecycle = "FINALIZING"
        reason = event.terminalization_reason
    elif event.kind == TaskEventKind.GATE_REPAIRABLE:
        _require(lifecycle == "FINALIZING" and desired == "RUN", "repair source")
        _require(facts.repair_budget_available and facts.deadline_available, "repair guards")
        lifecycle = "ACTIVE"
        action = "KEEP_CURRENT"
    elif event.kind == TaskEventKind.VERIFICATION_INFRASTRUCTURE_RETRY:
        _require(lifecycle == "FINALIZING" and desired == "RUN", "retry source")
        _require(facts.same_outer_lease and facts.attempt_budget_available, "retry guards")
        lifecycle = "QUEUED"
        action = "FENCE_CURRENT"
    elif event.kind == TaskEventKind.RESULT_PUBLISHED:
        _require(lifecycle == "FINALIZING" and desired == "RUN", "publish source")
        _require(facts.terminal_cas_valid and event.publication is not None, "publish guard")
        lifecycle = "TERMINAL"
        action = "TERMINALIZE_CURRENT"
        terminal_kind = "task_result"
    elif event.kind == TaskEventKind.CANCEL_REQUESTED:
        _require(desired == "RUN", "cancel already requested")
        desired = "CANCEL"
    elif event.kind == TaskEventKind.CANCEL_FINALIZED:
        _require(desired == "CANCEL", "cancel was not requested")
        _require(facts.no_in_flight_tool and facts.effects_empty, "cancel guards")
        _require(event.publication is not None, "cancel result missing")
        lifecycle = "TERMINAL"
        action = "TERMINALIZE_CURRENT"
        terminal_kind = "task_result"
    elif event.kind == TaskEventKind.OUTER_LEASE_FENCED:
        _require(facts.outer_lease_invalid, "outer lease is not fenced")
        lifecycle = "TERMINAL"
        action = "FENCE_CURRENT"
        terminal_kind = "transport_abandoned"
    else:
        _invalid(f"unknown event: {event.kind}")

    return TaskTransition(
        lifecycle=lifecycle,
        desired_state=desired,
        task_version=state.task_version + 1,
        native_epoch=native_epoch,
        current_attempt_id=attempt_id,
        attempt_action=action,
        terminal_kind=terminal_kind,
        terminalization_reason=reason,
    )
