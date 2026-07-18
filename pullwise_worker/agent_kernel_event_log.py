"""Append-only idempotency journal used inside Task Store transactions."""

from __future__ import annotations

import sqlite3

from .agent_kernel_task_records import TaskStoreError


def idempotent_event_version(
    connection: sqlite3.Connection,
    task_id: str,
    event_type: str,
    idempotency_key: str,
    digest: str,
) -> int | None:
    row = connection.execute(
        """SELECT task_id,event_type,event_digest,task_version FROM task_events
           WHERE idempotency_key=?""",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if (
        row["task_id"] != task_id
        or row["event_type"] != event_type
        or row["event_digest"] != digest
    ):
        raise TaskStoreError("IDEMPOTENCY_CONFLICT")
    return int(row["task_version"])


def append_task_event(
    connection: sqlite3.Connection,
    task_id: str,
    event_type: str,
    idempotency_key: str,
    digest: str,
    task_version: int,
    created_at: str,
) -> None:
    event_seq = int(
        connection.execute(
            "SELECT COALESCE(MAX(event_seq),0)+1 FROM task_events WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO task_events(
           task_id,event_seq,idempotency_key,event_type,task_version,payload_ref,
           created_at,event_digest) VALUES(?,?,?,?,?,NULL,?,?)""",
        (
            task_id,
            event_seq,
            idempotency_key,
            event_type,
            task_version,
            created_at,
            digest,
        ),
    )
