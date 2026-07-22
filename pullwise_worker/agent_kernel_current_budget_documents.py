"""Package document constructors for current dispatch budgets."""

from __future__ import annotations

import hashlib

from .agent_kernel_current_package import (
    canonical_validated_current_bytes,
    seal_current_document,
)


def make_budget_ledger(**values: object) -> tuple[dict[str, object], bytes]:
    document = seal_current_document(
        "elapsed-budget-ledger/v1",
        {"schema_id": "elapsed-budget-ledger/v1", **values},
    )
    encoded = canonical_validated_current_bytes(
        "elapsed-budget-ledger/v1", document
    )
    return document, encoded


def make_budget_reservation(
    *,
    reservation_id: str,
    state: object,
    attempt_id: str,
    invocation_digest: str,
    reserved_ms: int,
    started_at: str,
) -> tuple[dict[str, object], bytes]:
    document = seal_current_document(
        "elapsed-budget-reservation/v1",
        {
            "schema_id": "elapsed-budget-reservation/v1",
            "reservation_id": reservation_id,
            "task_id": state.task_id,
            "attempt_id": attempt_id,
            "invocation_digest": invocation_digest,
            "reserved_ms": reserved_ms,
            "reserved_calls": 1,
            "previous_consumed_ms": state.consumed_ms,
            "previous_reserved_ms": state.reserved_ms,
            "previous_calls_consumed": state.calls_consumed,
            "previous_calls_reserved": state.calls_reserved,
            "started_at": started_at,
        },
    )
    encoded = canonical_validated_current_bytes(
        "elapsed-budget-reservation/v1", document
    )
    return document, encoded


def budget_identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(
        ("pullwise:current-budget:" + "\x00".join(parts)).encode("utf-8")
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"


__all__ = [
    "budget_identifier",
    "make_budget_ledger",
    "make_budget_reservation",
]
