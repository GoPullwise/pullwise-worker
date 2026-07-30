"""Server-ACK recording and fail-closed checkpoint recovery."""

from __future__ import annotations

import sqlite3

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import CurrentAuthorityProjectionError
from .agent_kernel_current_checkpoint_contract import (
    MACHINE_SCHEMA,
    MANIFEST_SCHEMA,
    SEMANTIC_SCHEMA,
    CurrentCheckpointError,
    LocalCommittedCheckpoint,
    RecoveredCheckpoint,
    ack_matches,
    authority_matches_commit,
    commit_from_row,
    error_detail,
    parse_exact,
    parse_validated,
    ref_matches,
)


class CheckpointRecoveryMixin:
    def record_server_ack(
        self, item: LocalCommittedCheckpoint
    ) -> LocalCommittedCheckpoint:
        if not isinstance(item, LocalCommittedCheckpoint):
            self._fail("CHECKPOINT_ACK_INVALID")
        try:
            with self.database.transaction() as connection:
                row = self._generation_row(
                    connection, item.task_id, item.generation
                )
                if row is None or commit_from_row(row) != item:
                    self._fail("CHECKPOINT_ACK_INVALID")
                existing = connection.execute(
                    "SELECT * FROM checkpoint_server_acks "
                    "WHERE task_id=? AND generation=?",
                    (item.task_id, item.generation),
                ).fetchone()
                if existing is not None:
                    if ack_matches(existing, item):
                        return item
                    self._fail("CHECKPOINT_ACK_CONFLICT")
                prior = connection.execute(
                    "SELECT generation, manifest_hash FROM checkpoint_server_acks "
                    "WHERE task_id=? ORDER BY generation DESC LIMIT 1",
                    (item.task_id,),
                ).fetchone()
                expected = 1 if prior is None else prior["generation"] + 1
                previous_hash = None if prior is None else prior["manifest_hash"]
                if (
                    item.generation != expected
                    or item.previous_manifest_hash != previous_hash
                ):
                    self._fail("CHECKPOINT_ACK_CAS_CONFLICT")
                authority = self._load_authority(connection, item.task_id)
                if not authority_matches_commit(authority, item):
                    self._fail("AUTHORITY_FENCED")
                connection.execute(
                    "INSERT INTO checkpoint_server_acks VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.task_id,
                        item.generation,
                        item.manifest_hash,
                        item.previous_manifest_hash,
                        item.authority_digest,
                        item.committed_task_version,
                        item.deletion_version,
                        item.transport_epoch,
                        item.native_epoch,
                        self.clock(),
                    ),
                )
                return item
        except CurrentCheckpointError:
            raise
        except (CurrentAuthorityProjectionError, sqlite3.Error) as exc:
            self._fail("CHECKPOINT_ACK_WRITE_FAILED", error_detail(exc))

    def recover(self, task_id: str) -> RecoveredCheckpoint:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT m.*, a.authority_digest, a.deletion_version, "
                "a.transport_epoch FROM checkpoint_server_acks a "
                "JOIN checkpoint_manifests m USING(task_id,generation,manifest_hash) "
                "WHERE a.task_id=? ORDER BY a.generation DESC",
                (task_id,),
            ).fetchall()
            for row in rows:
                try:
                    return self._recover_row(connection, row)
                except CurrentCheckpointError:
                    continue
        finally:
            connection.close()
        self._fail("CHECKPOINT_RECOVERY_UNAVAILABLE")

    def _recover_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> RecoveredCheckpoint:
        manifest_bytes = bytes(row["manifest_bytes"])
        manifest = parse_exact(MANIFEST_SCHEMA, manifest_bytes)
        machine_bytes = self._object_bytes(
            connection, manifest["machine_state_ref"]
        )
        semantic_bytes = self._object_bytes(
            connection, manifest["semantic_state_ref"]
        )
        machine = parse_exact(MACHINE_SCHEMA, machine_bytes)
        semantic = parse_exact(SEMANTIC_SCHEMA, semantic_bytes)
        previous = self._previous_manifest(connection, manifest)
        try:
            contract.verify_committed_checkpoint_context(
                manifest, machine, semantic, previous
            )
        except Exception as exc:
            self._fail("CHECKPOINT_STORAGE_CORRUPT", error_detail(exc))
        for ref in (
            machine["workspace_state_ref"],
            machine["execution_state_ref"],
            semantic["task_request_ref"],
            semantic["requirement_ledger_ref"],
        ):
            self._object_bytes(connection, ref)
        commit = commit_from_row(row)
        return RecoveredCheckpoint(
            commit,
            commit.generation,
            manifest_bytes,
            machine_bytes,
            semantic_bytes,
        )

    def _object_bytes(
        self, connection: sqlite3.Connection, ref: dict[str, object]
    ) -> bytes:
        row = connection.execute(
            "SELECT content_schema_id,size_bytes,object_bytes "
            "FROM checkpoint_objects WHERE sha256=?",
            (ref["sha256"],),
        ).fetchone()
        if row is None:
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        raw = bytes(row["object_bytes"])
        if tuple(row[:2]) != (
            ref["content_schema_id"],
            ref["size_bytes"],
        ) or not ref_matches(ref, row["content_schema_id"], raw):
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        parse_validated(row["content_schema_id"], raw)
        return raw


__all__ = ["CheckpointRecoveryMixin"]
