"""Server-ACK recording and fail-closed checkpoint recovery."""

from __future__ import annotations

import sqlite3

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import CurrentAuthorityProjectionError
from .agent_kernel_current_checkpoint_ack import ServerCheckpointAck
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
        self, ack_bytes: bytes
    ) -> LocalCommittedCheckpoint:
        ack = ServerCheckpointAck.from_canonical_bytes(ack_bytes)
        try:
            with self.database.transaction() as connection:
                row = self._generation_row(
                    connection, ack.task_id, ack.generation
                )
                if row is None:
                    self._fail("CHECKPOINT_ACK_INVALID")
                item = commit_from_row(row)
                if not self._ack_matches_local(ack, item, row):
                    self._fail("CHECKPOINT_ACK_INVALID")
                indexed = connection.execute(
                    "SELECT * FROM checkpoint_server_acks "
                    "WHERE task_id=? AND generation=?",
                    (item.task_id, item.generation),
                ).fetchone()
                document = connection.execute(
                    "SELECT * FROM checkpoint_server_ack_documents "
                    "WHERE task_id=? AND generation=?",
                    (item.task_id, item.generation),
                ).fetchone()
                if indexed is not None or document is not None:
                    if (
                        indexed is not None
                        and document is not None
                        and ack_matches(indexed, item)
                        and self._stored_ack_matches(document, ack)
                    ):
                        return item
                    code = (
                        "CHECKPOINT_ACK_CONFLICT"
                        if indexed is not None and document is not None
                        else "CHECKPOINT_ACK_STORAGE_CORRUPT"
                    )
                    self._fail(code)
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
                if not self._ack_matches_authority(ack, authority, item):
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
                        ack.accepted_at,
                    ),
                )
                self.fault_hook("ack.after_index")
                connection.execute(
                    "INSERT INTO checkpoint_server_ack_documents VALUES "
                    "(?,?,?,?,?,?,?)",
                    (
                        ack.ack_digest,
                        ack.task_id,
                        ack.generation,
                        ack.manifest_hash,
                        ack.request_digest,
                        ack.canonical_bytes,
                        ack.accepted_at,
                    ),
                )
                self.fault_hook("ack.after_document")
                self.fault_hook("ack.before_commit")
                return item
        except CurrentCheckpointError:
            raise
        except CurrentAuthorityProjectionError as exc:
            self._fail("AUTHORITY_FENCED", error_detail(exc))
        except sqlite3.Error as exc:
            self._fail("CHECKPOINT_ACK_WRITE_FAILED", error_detail(exc))

    @staticmethod
    def _stored_ack_matches(
        row: sqlite3.Row, ack: ServerCheckpointAck
    ) -> bool:
        return tuple(
            row[field]
            for field in (
                "ack_digest", "task_id", "generation", "manifest_hash",
                "request_digest", "accepted_at",
            )
        ) == (
            ack.ack_digest,
            ack.task_id,
            ack.generation,
            ack.manifest_hash,
            ack.request_digest,
            ack.accepted_at,
        ) and bytes(row["ack_bytes"]) == ack.canonical_bytes

    @staticmethod
    def _ack_matches_local(
        ack: ServerCheckpointAck,
        item: LocalCommittedCheckpoint,
        row: sqlite3.Row,
    ) -> bool:
        return (
            (
                ack.task_id,
                ack.generation,
                ack.manifest_hash,
                ack.previous_manifest_hash,
                ack.committed_from_task_version,
                ack.committed_task_version,
                ack.native_epoch,
                ack.attempt_id,
                ack.owner_epoch,
            )
            == (
                row["task_id"],
                row["generation"],
                row["manifest_hash"],
                row["previous_manifest_hash"],
                row["committed_from_task_version"],
                row["committed_task_version"],
                row["native_epoch"],
                row["attempt_id"],
                row["owner_epoch"],
            )
            and ack.authority_digest == item.authority_digest
            and ack.deletion_version == item.deletion_version
            and ack.transport_epoch == item.transport_epoch
        )

    @staticmethod
    def _ack_matches_authority(
        ack: ServerCheckpointAck,
        authority: object,
        item: LocalCommittedCheckpoint,
    ) -> bool:
        return bool(
            authority_matches_commit(authority, item)
            and ack.owner_id == authority.owner_id
            and ack.lease_id == authority.lease_id
            and ack.grant_id == authority.grant.grant_id
            and ack.grant_digest == authority.grant_digest
        )

    def recover(self, task_id: str) -> RecoveredCheckpoint:
        connection = self.database.connect()
        try:
            try:
                authority = self._load_authority(connection, task_id)
            except CurrentAuthorityProjectionError as exc:
                self._fail("AUTHORITY_FENCED", error_detail(exc))
            rows = connection.execute(
                self._acknowledged_generation_sql()
                + " WHERE a.task_id=? ORDER BY a.generation DESC",
                (task_id,),
            ).fetchall()
            for row in rows:
                try:
                    return self._recover_row(connection, row, authority)
                except CurrentCheckpointError:
                    continue
        except sqlite3.Error as exc:
            self._fail("CHECKPOINT_STORAGE_CORRUPT", error_detail(exc))
        finally:
            connection.close()
        self._fail("CHECKPOINT_RECOVERY_UNAVAILABLE")

    @staticmethod
    def _acknowledged_generation_sql() -> str:
        return (
            "SELECT m.*, a.authority_digest, a.deletion_version, "
            "a.transport_epoch, a.task_version AS acknowledged_task_version, "
            "d.ack_digest AS stored_ack_digest, "
            "d.request_digest AS stored_request_digest, "
            "d.accepted_at AS stored_accepted_at, d.ack_bytes "
            "FROM checkpoint_server_acks a "
            "JOIN checkpoint_server_ack_documents d "
            "USING(task_id,generation,manifest_hash) "
            "JOIN checkpoint_manifests m USING(task_id,generation,manifest_hash)"
        )

    def _acknowledged_predecessor(
        self,
        connection: sqlite3.Connection,
        manifest: dict[str, object],
    ) -> sqlite3.Row:
        row = connection.execute(
            self._acknowledged_generation_sql()
            + " WHERE m.task_id=? AND m.generation=? AND m.manifest_hash=?",
            (
                manifest["task_id"],
                manifest["generation"] - 1,
                manifest["previous_manifest_hash"],
            ),
        ).fetchone()
        if row is None:
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        return row

    def _recover_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row, authority: object
    ) -> RecoveredCheckpoint:
        candidate: RecoveredCheckpoint | None = None
        current = row
        while True:
            manifest_bytes = bytes(current["manifest_bytes"])
            manifest = parse_exact(MANIFEST_SCHEMA, manifest_bytes)
            previous_row = None
            previous = None
            if manifest["generation"] > 1:
                previous_row = self._acknowledged_predecessor(
                    connection, manifest
                )
                previous = parse_exact(
                    MANIFEST_SCHEMA, bytes(previous_row["manifest_bytes"])
                )
            machine_bytes, semantic_bytes = self._validate_generation(
                connection, current, manifest, previous, authority
            )
            if candidate is None:
                commit = commit_from_row(current)
                candidate = RecoveredCheckpoint(
                    commit,
                    commit.generation,
                    manifest_bytes,
                    machine_bytes,
                    semantic_bytes,
                )
            if previous_row is None:
                return candidate
            current = previous_row

    def _validate_generation(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        manifest: dict[str, object],
        previous: dict[str, object] | None,
        authority: object,
    ) -> tuple[bytes, bytes]:
        metadata = (
            "task_id", "generation", "manifest_hash", "previous_generation",
            "previous_manifest_hash", "committed_from_task_version",
            "committed_task_version", "native_epoch", "attempt_id", "owner_epoch",
        )
        if tuple(row[field] for field in metadata) != tuple(
            manifest[field] for field in metadata
        ):
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        if manifest["generation"] == 1 and (
            manifest["previous_generation"] != 0
            or manifest["previous_manifest_hash"] is not None
        ):
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        machine_bytes = self._object_bytes(
            connection, manifest["machine_state_ref"]
        )
        semantic_bytes = self._object_bytes(
            connection, manifest["semantic_state_ref"]
        )
        if (
            row["machine_state_sha256"] != manifest["machine_state_ref"]["sha256"]
            or row["semantic_state_sha256"]
            != manifest["semantic_state_ref"]["sha256"]
        ):
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        machine = parse_exact(MACHINE_SCHEMA, machine_bytes)
        semantic = parse_exact(SEMANTIC_SCHEMA, semantic_bytes)
        try:
            contract.verify_committed_checkpoint_context(
                manifest, machine, semantic, previous
            )
        except Exception as exc:
            self._fail("CHECKPOINT_STORAGE_CORRUPT", error_detail(exc))
        refs = [
            machine["workspace_state_ref"],
            machine["execution_state_ref"],
            semantic["task_request_ref"],
            semantic["requirement_ledger_ref"],
        ]
        if semantic["charter_ref"] is not None:
            refs.append(semantic["charter_ref"])
        refs.extend(semantic["evidence_refs"])
        for ref in refs:
            self._object_bytes(connection, ref)
        ack = ServerCheckpointAck.from_canonical_bytes(bytes(row["ack_bytes"]))
        item = commit_from_row(row)
        if (
            not self._ack_matches_local(ack, item, row)
            or not self._ack_matches_authority(ack, authority, item)
            or row["acknowledged_task_version"] != ack.committed_task_version
            or row["stored_ack_digest"] != ack.ack_digest
            or row["stored_request_digest"] != ack.request_digest
            or row["stored_accepted_at"] != ack.accepted_at
        ):
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        return machine_bytes, semantic_bytes

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
