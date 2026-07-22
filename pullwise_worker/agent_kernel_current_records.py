"""Atomic settlement and abandonment records for the current journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Callable

from .agent_kernel_current_budget import settle_budget
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_objects import PublishedCurrentReference
from .agent_kernel_current_package import (
    CURRENT_PACKAGE,
    canonical_validated_current_bytes,
    seal_current_document,
    verify_current_document_digest,
)
from .agent_kernel_current_settlement import (
    SettlementDocuments,
    finalize_observation,
)


class CurrentRecordError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def commit_documents(
    database: CurrentAgentKernelDatabase,
    *,
    capability_sha256: str,
    call: object,
    documents: SettlementDocuments,
    fault_hook: Callable[[str], None],
) -> bytes:
    with database.transaction() as connection:
        intent = _intent_by_capability(connection, capability_sha256)
        _assert_call(intent, call)
        if intent["state"] == "SETTLED":
            return _settled_replay(connection, intent, documents)
        if intent["state"] == "INTENT":
            raise CurrentRecordError("CAPABILITY_NOT_CONSUMED")
        if intent["state"] == "ABANDONED":
            raise CurrentRecordError("SETTLEMENT_AFTER_ABANDONMENT")
        if intent["state"] != "DISPATCHED":
            raise CurrentRecordError("CURRENT_INTENT_STATE_INVALID")
        _assert_current_authority(connection, intent)
        observation_seq = _next_observation_seq(connection)
        observation, observation_bytes = finalize_observation(
            documents, observation_seq
        )

        elapsed_ms = documents.receipt.get("elapsed_ms")
        budget = settle_budget(
            connection,
            intent=intent,
            elapsed_ms=elapsed_ms,
            outcome="settled",
        )
        references = (
            documents.payload.source,
            documents.payload.payload,
            documents.outcome_object,
        )
        for reference in references:
            _bind_reference(connection, intent["intent_id"], reference)
        connection.execute(
            "INSERT INTO dispatch_settlements VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent["intent_id"],
                documents.receipt_bytes,
                documents.receipt["receipt_digest"],
                documents.outcome_bytes,
                documents.outcome_object.sha256,
                documents.outcome_schema_id,
                observation_bytes,
                observation["observation_digest"],
                observation_seq,
                budget.canonical_bytes,
                budget.digest,
                documents.replay_bytes,
                hashlib.sha256(documents.replay_bytes).hexdigest(),
                documents.violation_bytes,
            ),
        )
        _transition(connection, intent["intent_id"], "DISPATCHED", "SETTLED")
        fault_hook("before_settlement_commit")
        return documents.replay_bytes


def abandon_with_capability(
    database: CurrentAgentKernelDatabase,
    *,
    capability_sha256: str,
    reason: str,
    abandoned_at: str,
    fault_hook: Callable[[str], None],
) -> bytes:
    with database.transaction() as connection:
        intent = _intent_by_capability(connection, capability_sha256)
        return _abandon(
            connection,
            intent=intent,
            reason=reason,
            abandoned_at=abandoned_at,
            fault_hook=fault_hook,
        )


def abandon_recovery_intent(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    idempotency_key: str,
    invocation_digest: str,
    reason: str,
    abandoned_at: str,
    fault_hook: Callable[[str], None],
) -> bytes:
    intent = connection.execute(
        "SELECT * FROM dispatch_intents WHERE task_id = ? AND idempotency_key = ?",
        (task_id, idempotency_key),
    ).fetchone()
    if intent is None:
        raise CurrentRecordError("INVOCATION_NOT_FOUND")
    if intent["invocation_digest"] != invocation_digest:
        raise CurrentRecordError("IDEMPOTENCY_CONFLICT")
    return _abandon(
        connection,
        intent=intent,
        reason=reason,
        abandoned_at=abandoned_at,
        fault_hook=fault_hook,
    )


def _abandon(
    connection: sqlite3.Connection,
    *,
    intent: sqlite3.Row,
    reason: str,
    abandoned_at: str,
    fault_hook: Callable[[str], None],
) -> bytes:
    if intent["state"] == "ABANDONED":
        row = connection.execute(
            "SELECT abandonment_bytes, replay_bytes, replay_sha256 "
            "FROM dispatch_abandonments WHERE intent_id = ?",
            (intent["intent_id"],),
        ).fetchone()
        if row is None:
            raise CurrentRecordError("ABANDONMENT_RECORD_MISSING")
        document = verify_current_document_digest(
            "dispatch-abandonment/v1", json.loads(bytes(row[0]))
        )
        if (
            document.get("reason") != reason
            or document.get("invocation_digest") != intent["invocation_digest"]
        ):
            raise CurrentRecordError("ABANDONMENT_REPLAY_CONFLICT")
        return _verified_replay(bytes(row[1]), str(row[2]))
    if intent["state"] == "DISPATCHED":
        raise CurrentRecordError("DISPATCH_AMBIGUOUS")
    if intent["state"] == "SETTLED":
        raise CurrentRecordError("ABANDONMENT_AFTER_SETTLEMENT")
    if intent["state"] != "INTENT":
        raise CurrentRecordError("CURRENT_INTENT_STATE_INVALID")

    budget = settle_budget(
        connection,
        intent=intent,
        elapsed_ms=0,
        outcome="abandoned",
    )
    abandonment = seal_current_document(
        "dispatch-abandonment/v1",
        {
            "schema_id": "dispatch-abandonment/v1",
            "package": CURRENT_PACKAGE.as_document(),
            "intent_id": intent["intent_id"],
            "invocation_digest": intent["invocation_digest"],
            "reservation_id": intent["reservation_id"],
            "reason": reason,
            "budget_settlement_digest": budget.digest,
            "abandoned_at": abandoned_at,
        },
    )
    encoded = canonical_validated_current_bytes(
        "dispatch-abandonment/v1", abandonment
    )
    digest = hashlib.sha256(encoded).hexdigest()
    connection.execute(
        "INSERT INTO dispatch_abandonments VALUES (?,?,?,?,?,?,?)",
        (
            intent["intent_id"],
            encoded,
            digest,
            budget.canonical_bytes,
            budget.digest,
            encoded,
            digest,
        ),
    )
    _transition(connection, intent["intent_id"], "INTENT", "ABANDONED")
    fault_hook("before_abandonment_commit")
    return encoded


def _settled_replay(
    connection: sqlite3.Connection,
    intent: sqlite3.Row,
    documents: SettlementDocuments,
) -> bytes:
    row = connection.execute(
        "SELECT receipt_bytes, outcome_bytes, observation_bytes, observation_seq, "
        "replay_bytes, replay_sha256, violation_bytes "
        "FROM dispatch_settlements WHERE intent_id = ?",
        (intent["intent_id"],),
    ).fetchone()
    if row is None:
        raise CurrentRecordError("SETTLEMENT_RECORD_MISSING")
    stored_violation = None if row[6] is None else bytes(row[6])
    _, expected_observation_bytes = finalize_observation(documents, int(row[3]))
    if (
        bytes(row[0]) != documents.receipt_bytes
        or bytes(row[1]) != documents.outcome_bytes
        or bytes(row[2]) != expected_observation_bytes
        or stored_violation != documents.violation_bytes
    ):
        raise CurrentRecordError("SETTLEMENT_REPLAY_CONFLICT")
    return _verified_replay(bytes(row[4]), str(row[5]))


def _next_observation_seq(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(observation_seq), 0) + 1 "
            "FROM dispatch_settlements"
        ).fetchone()[0]
    )


def _bind_reference(
    connection: sqlite3.Connection,
    intent_id: str,
    reference: PublishedCurrentReference,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO content_objects VALUES (?, ?, ?)",
        (
            reference.object.sha256,
            reference.object.size_bytes,
            reference.object.relative_path,
        ),
    )
    row = connection.execute(
        "SELECT size_bytes, relative_path FROM content_objects WHERE sha256 = ?",
        (reference.object.sha256,),
    ).fetchone()
    if row is None or tuple(row) != (
        reference.object.size_bytes,
        reference.object.relative_path,
    ):
        raise CurrentRecordError("CONTENT_OBJECT_CONFLICT")
    connection.execute(
        "INSERT INTO content_bindings VALUES (?, ?, ?, ?, ?)",
        (
            intent_id,
            reference.content_ref["artifact_id"],
            reference.content_ref["content_schema_id"],
            reference.object.sha256,
            reference.content_ref_bytes,
        ),
    )


def _intent_by_capability(
    connection: sqlite3.Connection, capability_sha256: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM dispatch_intents WHERE capability_sha256 = ?",
        (capability_sha256,),
    ).fetchone()
    if row is None:
        raise CurrentRecordError("DISPATCH_CAPABILITY_INVALID")
    return row


def _assert_call(intent: sqlite3.Row, call: object) -> None:
    if (
        intent["task_id"] != getattr(call, "task_id", None)
        or intent["idempotency_key"] != getattr(call, "idempotency_key", None)
        or intent["invocation_digest"] != getattr(call, "invocation_digest", None)
        or intent["authority_digest"] != getattr(call, "authority_digest", None)
        or intent["grant_digest"] != getattr(call, "grant_digest", None)
        or intent["tool_key"] != getattr(call, "tool_key", None)
    ):
        raise CurrentRecordError("SETTLEMENT_INVOCATION_CONFLICT")


def _assert_current_authority(
    connection: sqlite3.Connection, intent: sqlite3.Row
) -> None:
    head = connection.execute(
        "SELECT heads.projection_digest, history.state "
        "FROM authority_heads AS heads "
        "JOIN authority_history AS history "
        "ON history.projection_digest = heads.projection_digest "
        "WHERE heads.task_id = ?",
        (intent["task_id"],),
    ).fetchone()
    if head is None or tuple(head) != (intent["authority_digest"], "ACTIVE"):
        raise CurrentRecordError("AUTHORITY_FENCED")


def _transition(
    connection: sqlite3.Connection,
    intent_id: str,
    old_state: str,
    new_state: str,
) -> None:
    cursor = connection.execute(
        "UPDATE dispatch_intents SET state = ? WHERE intent_id = ? AND state = ?",
        (new_state, intent_id, old_state),
    )
    if cursor.rowcount != 1:
        raise CurrentRecordError("CURRENT_INTENT_TRANSITION_CONFLICT")


def _verified_replay(payload: bytes, expected_sha256: str) -> bytes:
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise CurrentRecordError("CURRENT_REPLAY_CORRUPT")
    return payload


__all__ = [
    "CurrentRecordError",
    "abandon_recovery_intent",
    "abandon_with_capability",
    "commit_documents",
]
