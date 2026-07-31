"""Deterministic documents and writes for one terminal TaskResult CAS."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import sqlite3

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import CurrentAuthorityProjection
from .agent_kernel_current_terminal_result import ValidatedTerminalResult
from .agent_kernel_current_terminalization_contract import (
    CurrentTerminalizationError,
    PreparedTerminalization,
    canonical_bytes,
    content_ref,
    object_sha256,
    parse_exact,
)


@dataclass(frozen=True)
class TerminalCommitDocuments:
    base: dict[str, object]
    base_bytes: bytes
    base_sha256: str
    finalizing: dict[str, object]
    finalizing_bytes: bytes
    finalizing_sha256: str
    terminal: dict[str, object]
    terminal_bytes: bytes
    terminal_sha256: str
    requested_event: dict[str, object]
    requested_event_bytes: bytes
    requested_event_sha256: str
    published_event: dict[str, object]
    published_event_bytes: bytes
    published_event_sha256: str
    proof: dict[str, object]
    proof_bytes: bytes
    proof_sha256: str

    def object_values(self) -> tuple[tuple[str, bytes], ...]:
        return (
            ("task-record/v1", self.base_bytes),
            ("task-record/v1", self.finalizing_bytes),
            ("task-record/v1", self.terminal_bytes),
            ("task-control-event/v1", self.requested_event_bytes),
            ("task-control-event/v1", self.published_event_bytes),
            ("task-version-authority-proof/v1", self.proof_bytes),
        )


def _load_base(
    connection: sqlite3.Connection,
    task_id: str,
    stored: sqlite3.Row | None,
) -> tuple[dict[str, object], bytes, str]:
    if stored is None:
        row = connection.execute(
            "SELECT r.record_bytes,r.record_sha256 FROM runtime_task_heads h "
            "JOIN runtime_task_records r "
            "ON r.task_id=h.task_id AND r.task_version=h.task_version "
            "AND r.record_sha256=h.record_sha256 WHERE h.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise CurrentTerminalizationError("TERMINALIZATION_TASK_NOT_FOUND")
        raw = bytes(row["record_bytes"])
        digest = row["record_sha256"]
    else:
        digest = stored["base_task_record_sha256"]
        raw = _stored_object(connection, digest, "task-record/v1")
    if object_sha256(raw) != digest:
        raise CurrentTerminalizationError("TERMINALIZATION_STORAGE_CORRUPT")
    return (
        parse_exact(
            "task-record/v1",
            raw,
            code="TERMINALIZATION_TASK_CORRUPT",
        ),
        raw,
        digest,
    )


def _stored_object(
    connection: sqlite3.Connection,
    digest: str,
    schema_id: str,
) -> bytes:
    row = connection.execute(
        "SELECT content_schema_id,size_bytes,object_bytes "
        "FROM checkpoint_objects WHERE sha256=?",
        (digest,),
    ).fetchone()
    if row is None:
        raise CurrentTerminalizationError("TERMINALIZATION_STORAGE_CORRUPT")
    raw = bytes(row["object_bytes"])
    if (
        row["content_schema_id"] != schema_id
        or row["size_bytes"] != len(raw)
        or object_sha256(raw) != digest
    ):
        raise CurrentTerminalizationError("TERMINALIZATION_STORAGE_CORRUPT")
    return raw


def _fence(authority: object) -> dict[str, object]:
    return contract.seal_document(
        "task-fence/v1",
        {
            "schema_id": "task-fence/v1",
            **{
                field: getattr(authority, field)
                for field in (
                    "task_id",
                    "attempt_id",
                    "session_id",
                    "owner_id",
                    "lease_id",
                    "task_version",
                    "deletion_version",
                    "owner_epoch",
                    "native_epoch",
                    "transport_epoch",
                )
            },
        },
    )


def _event(
    *,
    kind: str,
    authority: object,
    fence: dict[str, object],
    previous: dict[str, object],
    current: dict[str, object],
    input_schema_id: str,
    input_document: dict[str, object],
    occurred_at: str,
) -> dict[str, object]:
    input_ref = content_ref(input_schema_id, input_document)
    seed = (
        f"{authority.task_id}:{kind}:{previous['task_version']}:"
        f"{current['task_version']}:{input_ref['sha256']}"
    )
    document = contract.seal_document(
        "task-control-event/v1",
        {
            "schema_id": "task-control-event/v1",
            "package": authority.package.as_document(),
            "event_id": "event_" + hashlib.sha256(seed.encode()).hexdigest()[:32],
            "event_kind": kind,
            "idempotency_key": seed,
            "authority_digest": authority.digest,
            "grant_digest": authority.grant_digest,
            "full_fence": deepcopy(fence),
            "task_id": authority.task_id,
            "previous_task_version": previous["task_version"],
            "task_version": current["task_version"],
            "input_ref": input_ref,
            "previous_task_record_ref": content_ref(
                "task-record/v1", previous
            ),
            "task_record_ref": content_ref("task-record/v1", current),
            "occurred_at": occurred_at,
        },
    )
    contract.verify_task_control_event_context(
        document,
        authority.as_document(),
        previous,
        current,
        input_document,
    )
    return document


def build_terminal_commit(
    connection: sqlite3.Connection,
    authority_store: CurrentAuthorityProjection,
    prepared: PreparedTerminalization,
    result: ValidatedTerminalResult,
    *,
    stored: sqlite3.Row | None = None,
) -> TerminalCommitDocuments:
    authority = authority_store.load_head(connection, prepared.task_id)
    authority_store.assert_runnable(authority)
    base, base_raw, base_sha = _load_base(
        connection, prepared.task_id, stored
    )
    if (
        base["task_version"] != authority.task_version
        or base["lifecycle"] != "ACTIVE"
    ):
        raise CurrentTerminalizationError("TASK_VERSION_STALE")
    snapshot = parse_exact(
        "terminalization-input-snapshot/v1",
        prepared.terminalization_input_bytes,
    )
    finalizing = deepcopy(base)
    finalizing.update(
        {
            "task_version": snapshot["task_version"],
            "lifecycle": "FINALIZING",
            "updated_at": snapshot["trusted_wall_time_at"],
        }
    )
    finalizing = contract.validate_task_record_transition(base, finalizing)
    finalizing_raw = canonical_bytes("task-record/v1", finalizing)
    terminal = deepcopy(finalizing)
    terminal.update(
        {
            "task_version": result.document["terminal_task_version"],
            "lifecycle": "TERMINAL",
            "terminal_kind": "task_result",
            "result_ref": content_ref("task-result/v1", result.document),
            "result_digest": result.result_digest,
            "outcome": result.document["outcome"],
            "updated_at": result.document["terminal_at"],
            "terminal_at": result.document["terminal_at"],
        }
    )
    terminal = contract.validate_task_result_publication(
        finalizing, terminal, result.document
    )
    terminal_raw = canonical_bytes("task-record/v1", terminal)
    fence = _fence(authority)
    requested = _event(
        kind="terminalization_requested",
        authority=authority,
        fence=fence,
        previous=base,
        current=finalizing,
        input_schema_id="terminalization-input-snapshot/v1",
        input_document=snapshot,
        occurred_at=finalizing["updated_at"],
    )
    published = _event(
        kind="task_result_published",
        authority=authority,
        fence=fence,
        previous=finalizing,
        current=terminal,
        input_schema_id="task-result/v1",
        input_document=result.document,
        occurred_at=terminal["updated_at"],
    )
    requested_raw = canonical_bytes("task-control-event/v1", requested)
    published_raw = canonical_bytes("task-control-event/v1", published)
    proof = contract.seal_document(
        "task-version-authority-proof/v1",
        {
            "schema_id": "task-version-authority-proof/v1",
            "package": authority.package.as_document(),
            "task_id": authority.task_id,
            "authority_digest": authority.digest,
            "grant_digest": authority.grant_digest,
            "full_fence": fence,
            "base_task_record_ref": content_ref("task-record/v1", base),
            "version_chain": [
                {
                    "transition_kind": "terminalization_requested",
                    "previous_task_version": base["task_version"],
                    "task_version": finalizing["task_version"],
                    "transition_ref": content_ref(
                        "task-control-event/v1", requested
                    ),
                    "task_record_ref": content_ref(
                        "task-record/v1", finalizing
                    ),
                },
                {
                    "transition_kind": "task_result_published",
                    "previous_task_version": finalizing["task_version"],
                    "task_version": terminal["task_version"],
                    "transition_ref": content_ref(
                        "task-control-event/v1", published
                    ),
                    "task_record_ref": content_ref(
                        "task-record/v1", terminal
                    ),
                },
            ],
            "published_from_version": result.document[
                "published_from_version"
            ],
            "terminal_task_version": result.document[
                "terminal_task_version"
            ],
            "task_result_ref": content_ref(
                "task-result/v1", result.document
            ),
        },
    )
    contract.verify_task_version_authority_proof(
        proof, authority.as_document(), result.document
    )
    proof_raw = canonical_bytes("task-version-authority-proof/v1", proof)
    return TerminalCommitDocuments(
        base,
        base_raw,
        base_sha,
        finalizing,
        finalizing_raw,
        object_sha256(finalizing_raw),
        terminal,
        terminal_raw,
        object_sha256(terminal_raw),
        requested,
        requested_raw,
        object_sha256(requested_raw),
        published,
        published_raw,
        object_sha256(published_raw),
        proof,
        proof_raw,
        object_sha256(proof_raw),
    )


def insert_runtime_records(
    connection: sqlite3.Connection,
    documents: TerminalCommitDocuments,
) -> None:
    for task, raw, digest in (
        (
            documents.finalizing,
            documents.finalizing_bytes,
            documents.finalizing_sha256,
        ),
        (
            documents.terminal,
            documents.terminal_bytes,
            documents.terminal_sha256,
        ),
    ):
        connection.execute(
            "INSERT INTO runtime_task_records VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task["task_id"],
                task["task_version"],
                digest,
                "TERMINALIZATION",
                documents.proof_sha256,
                task["lifecycle"],
                task["desired_state"],
                task["current_attempt_id"],
                task["native_epoch"],
                task["owner_epoch"],
                task["current_checkpoint_generation"],
                task["current_checkpoint_hash"],
                raw,
            ),
        )


def advance_runtime_head(
    connection: sqlite3.Connection,
    documents: TerminalCommitDocuments,
) -> None:
    updated = connection.execute(
        "UPDATE runtime_task_heads SET task_version=?,record_sha256=? "
        "WHERE task_id=? AND task_version=? AND record_sha256=?",
        (
            documents.terminal["task_version"],
            documents.terminal_sha256,
            documents.base["task_id"],
            documents.base["task_version"],
            documents.base_sha256,
        ),
    ).rowcount
    if updated != 1:
        raise CurrentTerminalizationError("TASK_VERSION_STALE")


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
        if _stored_object(
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


__all__ = [
    "TerminalCommitDocuments",
    "advance_runtime_head",
    "assert_terminal_commit",
    "build_terminal_commit",
    "insert_runtime_records",
]
