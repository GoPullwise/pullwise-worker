"""Package-typed two-dimensional dispatch budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3

from .agent_kernel_current_budget_documents import (
    budget_identifier,
    make_budget_ledger,
    make_budget_reservation,
)
from .agent_kernel_current_package import (
    canonical_validated_current_bytes,
    seal_current_document,
    verify_current_document_digest,
)


class CurrentBudgetError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BudgetState:
    task_id: str
    grant_digest: str
    elapsed_limit_ms: int
    consumed_ms: int
    reserved_ms: int
    tool_call_limit: int
    calls_consumed: int
    calls_reserved: int
    ledger_bytes: bytes
    ledger_digest: str


@dataclass(frozen=True)
class ReservationPlan:
    task_id: str
    grant_digest: str
    invocation_digest: str
    reservation_id: str
    reserved_ms: int
    started_at: str
    document: dict[str, object]
    canonical_bytes: bytes

    @property
    def digest(self) -> str:
        return str(self.document["reservation_digest"])


@dataclass(frozen=True)
class BudgetSettlement:
    document: dict[str, object]
    canonical_bytes: bytes
    ledger: BudgetState

    @property
    def digest(self) -> str:
        return str(self.document["settlement_digest"])


def initialize_budget(connection: sqlite3.Connection, envelope: object) -> None:
    task_id = getattr(envelope, "task_id")
    grant_digest = getattr(envelope, "grant_digest")
    grant = getattr(envelope, "grant")
    ledger, ledger_bytes = make_budget_ledger(
        task_id=task_id,
        grant_digest=grant_digest,
        elapsed_limit_ms=grant.elapsed_limit_ms,
        consumed_ms=0,
        reserved_ms=0,
        tool_call_limit=grant.tool_call_limit,
        calls_consumed=0,
        calls_reserved=0,
    )
    connection.execute(
        "INSERT OR IGNORE INTO dispatch_budgets "
        "(task_id, grant_digest, elapsed_limit_ms, consumed_ms, reserved_ms, "
        "tool_call_limit, calls_consumed, calls_reserved, ledger_bytes, ledger_digest) "
        "VALUES (?, ?, ?, 0, 0, ?, 0, 0, ?, ?)",
        (
            task_id,
            grant_digest,
            grant.elapsed_limit_ms,
            grant.tool_call_limit,
            ledger_bytes,
            ledger["ledger_digest"],
        ),
    )
    state = load_budget(connection, task_id, grant_digest)
    if (
        state.elapsed_limit_ms != grant.elapsed_limit_ms
        or state.tool_call_limit != grant.tool_call_limit
        or state.consumed_ms != 0
        or state.reserved_ms != 0
        or state.calls_consumed != 0
        or state.calls_reserved != 0
        or state.ledger_bytes != ledger_bytes
    ):
        raise CurrentBudgetError("AUTHORITY_BUDGET_CONFLICT")


def plan_reservation(
    connection: sqlite3.Connection,
    *,
    envelope: object,
    call: object,
    started_at: str,
) -> ReservationPlan:
    state = load_budget(
        connection,
        getattr(envelope, "task_id"),
        getattr(envelope, "grant_digest"),
    )
    remaining_ms = state.elapsed_limit_ms - state.consumed_ms - state.reserved_ms
    remaining_calls = (
        state.tool_call_limit - state.calls_consumed - state.calls_reserved
    )
    if remaining_ms < 1 or remaining_calls < 1:
        raise CurrentBudgetError("BUDGET_EXHAUSTED")
    invocation_digest = getattr(call, "invocation_digest")
    reservation_id = budget_identifier(
        "reserve", state.task_id, state.grant_digest, invocation_digest
    )
    document, encoded = make_budget_reservation(
        reservation_id=reservation_id,
        state=state,
        attempt_id=getattr(call, "attempt_id"),
        invocation_digest=invocation_digest,
        reserved_ms=remaining_ms,
        started_at=started_at,
    )
    return ReservationPlan(
        task_id=state.task_id,
        grant_digest=state.grant_digest,
        invocation_digest=invocation_digest,
        reservation_id=reservation_id,
        reserved_ms=remaining_ms,
        started_at=started_at,
        document=document,
        canonical_bytes=encoded,
    )


def reserve_budget(
    connection: sqlite3.Connection,
    *,
    envelope: object,
    call: object,
    plan: ReservationPlan,
) -> BudgetState:
    if not isinstance(plan, ReservationPlan):
        raise CurrentBudgetError("BUDGET_RESERVATION_INVALID")
    state = load_budget(connection, plan.task_id, plan.grant_digest)
    remaining_ms = state.elapsed_limit_ms - state.consumed_ms - state.reserved_ms
    remaining_calls = (
        state.tool_call_limit - state.calls_consumed - state.calls_reserved
    )
    if remaining_ms < 1 or remaining_calls < 1:
        raise CurrentBudgetError("BUDGET_RESERVATION_STALE")
    expected_document, expected_bytes = make_budget_reservation(
        reservation_id=budget_identifier(
            "reserve", state.task_id, state.grant_digest,
            getattr(call, "invocation_digest"),
        ),
        state=state,
        attempt_id=getattr(call, "attempt_id"),
        invocation_digest=getattr(call, "invocation_digest"),
        reserved_ms=remaining_ms,
        started_at=plan.started_at,
    )
    if (
        plan.task_id != getattr(envelope, "task_id")
        or plan.grant_digest != getattr(envelope, "grant_digest")
        or plan.invocation_digest != getattr(call, "invocation_digest")
        or plan.reserved_ms != remaining_ms
        or plan.reservation_id != expected_document["reservation_id"]
        or plan.document != expected_document
        or plan.canonical_bytes != expected_bytes
    ):
        raise CurrentBudgetError("BUDGET_RESERVATION_STALE")
    return _write_state(
        connection,
        state,
        consumed_ms=state.consumed_ms,
        reserved_ms=state.reserved_ms + remaining_ms,
        calls_consumed=state.calls_consumed,
        calls_reserved=state.calls_reserved + 1,
    )


def settle_budget(
    connection: sqlite3.Connection,
    *,
    intent: sqlite3.Row,
    elapsed_ms: int,
    outcome: str,
) -> BudgetSettlement:
    if (
        isinstance(elapsed_ms, bool)
        or not isinstance(elapsed_ms, int)
        or elapsed_ms < 0
        or outcome not in {"settled", "abandoned"}
    ):
        raise CurrentBudgetError("BUDGET_SETTLEMENT_INVALID")
    state = load_budget(connection, intent["task_id"], intent["grant_digest"])
    reserved = intent["reserved_ms"]
    consumed = elapsed_ms if outcome == "settled" else 0
    consumed_calls = 1 if outcome == "settled" else 0
    released_calls = 1 - consumed_calls
    if (
        consumed > reserved
        or state.reserved_ms < reserved
        or state.calls_reserved < 1
    ):
        raise CurrentBudgetError("BUDGET_SETTLEMENT_OVERFLOW")
    resulting = _write_state(
        connection,
        state,
        consumed_ms=state.consumed_ms + consumed,
        reserved_ms=state.reserved_ms - reserved,
        calls_consumed=state.calls_consumed + consumed_calls,
        calls_reserved=state.calls_reserved - 1,
    )
    document = seal_current_document(
        "elapsed-budget-settlement/v1",
        {
            "schema_id": "elapsed-budget-settlement/v1",
            "reservation_id": intent["reservation_id"],
            "invocation_digest": intent["invocation_digest"],
            "elapsed_ms": elapsed_ms,
            "consumed_ms": consumed,
            "released_ms": reserved - consumed,
            "consumed_calls": consumed_calls,
            "released_calls": released_calls,
            "resulting_consumed_ms": resulting.consumed_ms,
            "resulting_reserved_ms": resulting.reserved_ms,
            "resulting_calls_consumed": resulting.calls_consumed,
            "resulting_calls_reserved": resulting.calls_reserved,
            "outcome": outcome,
        },
    )
    encoded = canonical_validated_current_bytes(
        "elapsed-budget-settlement/v1", document
    )
    return BudgetSettlement(document, encoded, resulting)


def load_budget(
    connection: sqlite3.Connection, task_id: str, grant_digest: str
) -> BudgetState:
    row = connection.execute(
        "SELECT task_id, grant_digest, elapsed_limit_ms, consumed_ms, reserved_ms, "
        "tool_call_limit, calls_consumed, calls_reserved, ledger_bytes, ledger_digest "
        "FROM dispatch_budgets WHERE task_id = ? AND grant_digest = ?",
        (task_id, grant_digest),
    ).fetchone()
    if row is None:
        raise CurrentBudgetError("BUDGET_LEDGER_NOT_FOUND")
    state = BudgetState(*tuple(row))
    try:
        document = verify_current_document_digest(
            "elapsed-budget-ledger/v1", json.loads(state.ledger_bytes)
        )
        encoded = canonical_validated_current_bytes(
            "elapsed-budget-ledger/v1", document
        )
    except Exception as exc:
        raise CurrentBudgetError("BUDGET_LEDGER_CORRUPT") from exc
    expected = {
        "task_id": state.task_id,
        "grant_digest": state.grant_digest,
        "elapsed_limit_ms": state.elapsed_limit_ms,
        "consumed_ms": state.consumed_ms,
        "reserved_ms": state.reserved_ms,
        "tool_call_limit": state.tool_call_limit,
        "calls_consumed": state.calls_consumed,
        "calls_reserved": state.calls_reserved,
        "ledger_digest": state.ledger_digest,
    }
    if encoded != state.ledger_bytes or any(
        document.get(key) != value for key, value in expected.items()
    ):
        raise CurrentBudgetError("BUDGET_LEDGER_CORRUPT")
    return state


def _write_state(
    connection: sqlite3.Connection,
    old: BudgetState,
    *,
    consumed_ms: int,
    reserved_ms: int,
    calls_consumed: int,
    calls_reserved: int,
) -> BudgetState:
    if (
        consumed_ms + reserved_ms > old.elapsed_limit_ms
        or calls_consumed + calls_reserved > old.tool_call_limit
    ):
        raise CurrentBudgetError("BUDGET_EXHAUSTED")
    document, encoded = make_budget_ledger(
        task_id=old.task_id,
        grant_digest=old.grant_digest,
        elapsed_limit_ms=old.elapsed_limit_ms,
        consumed_ms=consumed_ms,
        reserved_ms=reserved_ms,
        tool_call_limit=old.tool_call_limit,
        calls_consumed=calls_consumed,
        calls_reserved=calls_reserved,
    )
    cursor = connection.execute(
        "UPDATE dispatch_budgets SET consumed_ms = ?, reserved_ms = ?, "
        "calls_consumed = ?, calls_reserved = ?, ledger_bytes = ?, ledger_digest = ? "
        "WHERE task_id = ? AND grant_digest = ? AND consumed_ms = ? AND reserved_ms = ? "
        "AND calls_consumed = ? AND calls_reserved = ? AND ledger_digest = ?",
        (
            consumed_ms,
            reserved_ms,
            calls_consumed,
            calls_reserved,
            encoded,
            document["ledger_digest"],
            old.task_id,
            old.grant_digest,
            old.consumed_ms,
            old.reserved_ms,
            old.calls_consumed,
            old.calls_reserved,
            old.ledger_digest,
        ),
    )
    if cursor.rowcount != 1:
        raise CurrentBudgetError("BUDGET_RESERVATION_STALE")
    return BudgetState(
        old.task_id,
        old.grant_digest,
        old.elapsed_limit_ms,
        consumed_ms,
        reserved_ms,
        old.tool_call_limit,
        calls_consumed,
        calls_reserved,
        encoded,
        str(document["ledger_digest"]),
    )


__all__ = [
    "BudgetSettlement",
    "CurrentBudgetError",
    "ReservationPlan",
    "initialize_budget",
    "load_budget",
    "plan_reservation",
    "reserve_budget",
    "settle_budget",
]
