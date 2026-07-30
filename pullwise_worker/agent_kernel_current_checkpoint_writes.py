"""Transactional write primitives for committed checkpoints."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Mapping

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_checkpoint_contract import (
    MACHINE_SCHEMA,
    SEMANTIC_SCHEMA,
)


class CheckpointWriteMixin:
    def _insert_objects(
        self,
        connection: sqlite3.Connection,
        supplied: Mapping[str, tuple[str, bytes]],
    ) -> None:
        for digest, (schema_id, raw) in supplied.items():
            row = connection.execute(
                "SELECT content_schema_id,size_bytes,object_bytes "
                "FROM checkpoint_objects WHERE sha256=?",
                (digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO checkpoint_objects VALUES (?,?,?,?)",
                    (digest, schema_id, len(raw), raw),
                )
            elif tuple(row[:2]) != (schema_id, len(raw)) or bytes(row[2]) != raw:
                self._fail("CHECKPOINT_OBJECT_STORAGE_CORRUPT")

    def _insert_manifest(
        self,
        connection: sqlite3.Connection,
        manifest: dict[str, object],
        manifest_bytes: bytes,
        machine_bytes: bytes,
        semantic_bytes: bytes,
    ) -> None:
        for schema_id, raw in (
            (MACHINE_SCHEMA, machine_bytes),
            (SEMANTIC_SCHEMA, semantic_bytes),
        ):
            digest = hashlib.sha256(raw).hexdigest()
            row = connection.execute(
                "SELECT content_schema_id,size_bytes,object_bytes "
                "FROM checkpoint_objects WHERE sha256=?",
                (digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO checkpoint_objects VALUES (?,?,?,?)",
                    (digest, schema_id, len(raw), raw),
                )
            elif tuple(row[:2]) != (schema_id, len(raw)) or bytes(row[2]) != raw:
                self._fail("CHECKPOINT_OBJECT_STORAGE_CORRUPT")
        connection.execute(
            "INSERT INTO checkpoint_manifests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                manifest[field]
                for field in (
                    "manifest_hash",
                    "task_id",
                    "generation",
                    "previous_generation",
                    "previous_manifest_hash",
                    "committed_from_task_version",
                    "committed_task_version",
                    "native_epoch",
                    "attempt_id",
                    "owner_epoch",
                )
            )
            + (
                manifest["machine_state_ref"]["sha256"],
                manifest["semantic_state_ref"]["sha256"],
                manifest_bytes,
                manifest["created_at"],
            ),
        )

    @staticmethod
    def _insert_index(
        connection: sqlite3.Connection, manifest: dict[str, object]
    ) -> None:
        connection.execute(
            "INSERT INTO checkpoint_index VALUES (?,?,?,?,?)",
            tuple(
                manifest[field]
                for field in (
                    "task_id",
                    "generation",
                    "manifest_hash",
                    "previous_manifest_hash",
                    "created_at",
                )
            ),
        )

    @staticmethod
    def _insert_task_record(
        connection: sqlite3.Connection,
        task: dict[str, object],
        raw: bytes,
        digest: str,
        source_digest: str,
    ) -> None:
        connection.execute(
            "INSERT INTO runtime_task_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task["task_id"],
                task["task_version"],
                digest,
                "CHECKPOINT",
                source_digest,
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

    def _advance_heads(
        self,
        connection: sqlite3.Connection,
        previous: dict[str, object],
        task: dict[str, object],
        task_sha256: str,
        manifest: dict[str, object],
    ) -> None:
        old_sha = hashlib.sha256(
            contract.canonical_validated_bytes("task-record/v1", previous)
        ).hexdigest()
        updated = connection.execute(
            "UPDATE runtime_task_heads SET task_version=?, record_sha256=? "
            "WHERE task_id=? AND task_version=? AND record_sha256=?",
            (
                task["task_version"],
                task_sha256,
                task["task_id"],
                previous["task_version"],
                old_sha,
            ),
        ).rowcount
        if updated != 1:
            self._fail("CHECKPOINT_CAS_CONFLICT")
        head = connection.execute(
            "SELECT generation FROM checkpoint_heads WHERE task_id=?",
            (task["task_id"],),
        ).fetchone()
        values = tuple(
            manifest[field]
            for field in (
                "task_id",
                "generation",
                "manifest_hash",
                "previous_manifest_hash",
                "committed_task_version",
                "native_epoch",
                "attempt_id",
                "owner_epoch",
            )
        )
        if head is None:
            connection.execute(
                "INSERT INTO checkpoint_heads VALUES (?,?,?,?,?,?,?,?)", values
            )
        else:
            changed = connection.execute(
                "UPDATE checkpoint_heads SET generation=?,manifest_hash=?,"
                "previous_manifest_hash=?,committed_task_version=?,native_epoch=?,"
                "attempt_id=?,owner_epoch=? WHERE task_id=? AND generation=?",
                values[1:] + (values[0], manifest["previous_generation"]),
            ).rowcount
            if changed != 1:
                self._fail("CHECKPOINT_CAS_CONFLICT")


__all__ = ["CheckpointWriteMixin"]
