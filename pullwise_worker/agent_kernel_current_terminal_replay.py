"""Fail-closed validation of an immutable terminal TaskResult replay."""

from __future__ import annotations

import sqlite3

from .agent_kernel_current_terminal_commit import (
    TerminalCommitDocuments,
    stored_terminal_object,
)
from .agent_kernel_current_terminalization_contract import (
    CurrentTerminalizationError,
    object_sha256,
)


def assert_terminal_commit(
    connection: sqlite3.Connection,
    stored: sqlite3.Row,
    documents: TerminalCommitDocuments,
) -> None:
    actual = tuple(
        stored[field]
        for field in (
            "base_task_version",
            "base_task_record_sha256",
            "finalizing_task_record_sha256",
            "terminal_task_record_sha256",
            "terminalization_event_sha256",
            "publication_event_sha256",
            "task_version_authority_sha256",
        )
    )
    expected = (
        documents.base["task_version"],
        documents.base_sha256,
        documents.finalizing_sha256,
        documents.terminal_sha256,
        documents.requested_event_sha256,
        documents.published_event_sha256,
        documents.proof_sha256,
    )
    if actual != expected:
        raise CurrentTerminalizationError("TERMINALIZATION_REPLAY_CONFLICT")
    for schema_id, raw in documents.object_values():
        if stored_terminal_object(
            connection, object_sha256(raw), schema_id
        ) != raw:
            raise CurrentTerminalizationError(
                "TERMINALIZATION_STORAGE_CORRUPT"
            )
    head = connection.execute(
        "SELECT task_version,record_sha256 FROM runtime_task_heads "
        "WHERE task_id=?",
        (documents.terminal["task_id"],),
    ).fetchone()
    if head is None or tuple(head) != (
        documents.terminal["task_version"],
        documents.terminal_sha256,
    ):
        raise CurrentTerminalizationError("TERMINALIZATION_STORAGE_CORRUPT")
    rows = connection.execute(
        "SELECT task_version,record_sha256,source_kind,source_digest,"
        "record_bytes FROM runtime_task_records WHERE task_id=? "
        "AND task_version IN (?,?) ORDER BY task_version",
        (
            documents.terminal["task_id"],
            documents.finalizing["task_version"],
            documents.terminal["task_version"],
        ),
    ).fetchall()
    expected_rows = (
        (
            documents.finalizing["task_version"],
            documents.finalizing_sha256,
            "TERMINALIZATION",
            documents.proof_sha256,
            documents.finalizing_bytes,
        ),
        (
            documents.terminal["task_version"],
            documents.terminal_sha256,
            "TERMINALIZATION",
            documents.proof_sha256,
            documents.terminal_bytes,
        ),
    )
    if tuple(
        (
            row["task_version"],
            row["record_sha256"],
            row["source_kind"],
            row["source_digest"],
            bytes(row["record_bytes"]),
        )
        for row in rows
    ) != expected_rows:
        raise CurrentTerminalizationError("TERMINALIZATION_STORAGE_CORRUPT")


__all__ = ["assert_terminal_commit"]
