"""Task-scoped exact replay queries for the current dispatch journal."""

from __future__ import annotations

import hashlib
import sqlite3

from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_journal_types import CurrentJournalError
from .agent_kernel_gateway import ReplayState


def probe_current_replay(
    database: CurrentAgentKernelDatabase,
    *,
    task_id: str,
    idempotency_key: str,
    invocation_digest: str,
) -> ReplayState:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT intent_id, invocation_digest, state FROM dispatch_intents "
            "WHERE task_id = ? AND idempotency_key = ?",
            (task_id, idempotency_key),
        ).fetchone()
        if row is None:
            return ReplayState.new()
        if row["invocation_digest"] != invocation_digest:
            raise CurrentJournalError("IDEMPOTENCY_CONFLICT")
        return replay_state(connection, row["intent_id"], row["state"])


def replay_state(
    connection: sqlite3.Connection,
    intent_id: str,
    state: str,
) -> ReplayState:
    if state in {"INTENT", "DISPATCHED"}:
        return ReplayState.pending()
    table = {
        "SETTLED": "dispatch_settlements",
        "ABANDONED": "dispatch_abandonments",
    }.get(state)
    if table is None:
        raise CurrentJournalError("CURRENT_INTENT_STATE_INVALID")
    row = connection.execute(
        f"SELECT replay_bytes, replay_sha256 FROM {table} WHERE intent_id = ?",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise CurrentJournalError("CURRENT_REPLAY_MISSING")
    payload = bytes(row[0])
    if hashlib.sha256(payload).hexdigest() != row[1]:
        raise CurrentJournalError("CURRENT_REPLAY_CORRUPT")
    return ReplayState.completed(payload)


__all__ = ["probe_current_replay", "replay_state"]
