"""Checkpoint value objects and exact canonical parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_package import ServerAuthorityEnvelope


MANIFEST_SCHEMA = "committed-checkpoint-manifest/v1"
MACHINE_SCHEMA = "machine-checkpoint/v1"
SEMANTIC_SCHEMA = "semantic-checkpoint/v1"


class CurrentCheckpointError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class LocalCommittedCheckpoint:
    task_id: str
    generation: int
    manifest_hash: str
    previous_manifest_hash: str | None
    committed_task_version: int
    native_epoch: int
    attempt_id: str
    owner_epoch: int
    authority_digest: str
    deletion_version: int
    transport_epoch: int
    committed_at: str


@dataclass(frozen=True)
class RecoveredCheckpoint:
    commit: LocalCommittedCheckpoint
    generation: int
    manifest_bytes: bytes
    machine_state_bytes: bytes
    semantic_state_bytes: bytes


def parse_exact(schema_id: str, raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
        if contract.canonical_document_bytes(value) != raw:
            raise CurrentCheckpointError("CHECKPOINT_NONCANONICAL")
        return contract.verify_document_digest(schema_id, value)
    except CurrentCheckpointError:
        raise
    except Exception as exc:
        raise CurrentCheckpointError(
            "CHECKPOINT_DOCUMENT_INVALID", error_detail(exc)
        ) from exc


def parse_validated(schema_id: str, raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
        checked = contract.validate_document(schema_id, value)
        if contract.canonical_validated_bytes(schema_id, checked) != raw:
            raise CurrentCheckpointError("CHECKPOINT_NONCANONICAL")
        return checked
    except CurrentCheckpointError:
        raise
    except Exception as exc:
        raise CurrentCheckpointError(
            "CHECKPOINT_OBJECT_INVALID", error_detail(exc)
        ) from exc


def ref_matches(
    ref: dict[str, object], schema_id: str, raw: bytes
) -> bool:
    return bool(
        ref["content_schema_id"] == schema_id
        and ref["sha256"] == hashlib.sha256(raw).hexdigest()
        and ref["size_bytes"] == len(raw)
        and ref["media_type"] == "application/json"
        and ref["encoding"] == "utf-8"
    )


def commit_identity(
    manifest: dict[str, object], authority: ServerAuthorityEnvelope
) -> LocalCommittedCheckpoint:
    return LocalCommittedCheckpoint(
        task_id=manifest["task_id"],
        generation=manifest["generation"],
        manifest_hash=manifest["manifest_hash"],
        previous_manifest_hash=manifest["previous_manifest_hash"],
        committed_task_version=manifest["committed_task_version"],
        native_epoch=manifest["native_epoch"],
        attempt_id=manifest["attempt_id"],
        owner_epoch=manifest["owner_epoch"],
        authority_digest=authority.digest,
        deletion_version=authority.deletion_version,
        transport_epoch=authority.transport_epoch,
        committed_at=manifest["created_at"],
    )


def commit_from_row(row: sqlite3.Row) -> LocalCommittedCheckpoint:
    return LocalCommittedCheckpoint(
        task_id=row["task_id"],
        generation=row["generation"],
        manifest_hash=row["manifest_hash"],
        previous_manifest_hash=row["previous_manifest_hash"],
        committed_task_version=row["committed_task_version"],
        native_epoch=row["native_epoch"],
        attempt_id=row["attempt_id"],
        owner_epoch=row["owner_epoch"],
        authority_digest=row["authority_digest"],
        deletion_version=row["deletion_version"],
        transport_epoch=row["transport_epoch"],
        committed_at=row["committed_at"],
    )


def authority_matches_commit(
    authority: ServerAuthorityEnvelope, item: LocalCommittedCheckpoint
) -> bool:
    return bool(
        authority.task_id == item.task_id
        and authority.digest == item.authority_digest
        and authority.deletion_version == item.deletion_version
        and authority.transport_epoch == item.transport_epoch
        and authority.native_epoch == item.native_epoch
        and authority.attempt_id == item.attempt_id
        and authority.owner_epoch == item.owner_epoch
    )


def ack_matches(row: sqlite3.Row, item: LocalCommittedCheckpoint) -> bool:
    return tuple(
        row[field]
        for field in (
            "task_id",
            "generation",
            "manifest_hash",
            "previous_manifest_hash",
            "authority_digest",
            "task_version",
            "deletion_version",
            "transport_epoch",
            "native_epoch",
        )
    ) == (
        item.task_id,
        item.generation,
        item.manifest_hash,
        item.previous_manifest_hash,
        item.authority_digest,
        item.committed_task_version,
        item.deletion_version,
        item.transport_epoch,
        item.native_epoch,
    )


def error_detail(error: BaseException) -> str:
    return str(getattr(error, "code", type(error).__name__))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "MACHINE_SCHEMA",
    "MANIFEST_SCHEMA",
    "SEMANTIC_SCHEMA",
    "CurrentCheckpointError",
    "LocalCommittedCheckpoint",
    "RecoveredCheckpoint",
    "ack_matches",
    "authority_matches_commit",
    "commit_from_row",
    "commit_identity",
    "error_detail",
    "parse_exact",
    "parse_validated",
    "ref_matches",
    "utc_now",
]
