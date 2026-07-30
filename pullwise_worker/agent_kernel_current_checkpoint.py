"""Atomic dual-layer checkpoint commits and ACK-gated recovery."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import sqlite3
from typing import Callable, Mapping

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import (
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_checkpoint_contract import (
    MACHINE_SCHEMA,
    MANIFEST_SCHEMA,
    SEMANTIC_SCHEMA,
    CurrentCheckpointError,
    LocalCommittedCheckpoint,
    RecoveredCheckpoint,
    commit_from_row as _commit_from_row,
    commit_identity as _commit_identity,
    error_detail as _detail,
    parse_exact as _parse_exact,
    parse_validated as _parse_validated,
    ref_matches as _ref_matches,
    utc_now as _utc_now,
)
from .agent_kernel_current_checkpoint_recovery import CheckpointRecoveryMixin
from .agent_kernel_current_checkpoint_writes import CheckpointWriteMixin
from .agent_kernel_current_package import ServerAuthorityEnvelope


class CurrentCheckpointStore(CheckpointRecoveryMixin, CheckpointWriteMixin):
    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        clock: Callable[[], str] | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentCheckpointError("CURRENT_DATABASE_INVALID")
        if clock is not None and not callable(clock):
            raise CurrentCheckpointError("CHECKPOINT_CLOCK_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise CurrentCheckpointError("CHECKPOINT_HOOK_INVALID")
        self.database = database
        self.clock = clock or _utc_now
        self.fault_hook = fault_hook or (lambda _stage: None)
        self.authority = CurrentAuthorityProjection(database)

    def commit(
        self,
        manifest_bytes: bytes,
        machine_state_bytes: bytes,
        semantic_state_bytes: bytes,
        objects: Mapping[str, tuple[str, bytes]],
    ) -> LocalCommittedCheckpoint:
        manifest = _parse_exact(MANIFEST_SCHEMA, manifest_bytes)
        machine = _parse_exact(MACHINE_SCHEMA, machine_state_bytes)
        semantic = _parse_exact(SEMANTIC_SCHEMA, semantic_state_bytes)
        supplied = self._validated_objects(objects, machine, semantic)
        snapshot = self._commit_snapshot(manifest)
        if snapshot["replay"] is not None:
            return self._assert_replay(
                snapshot["replay"], manifest_bytes, machine_state_bytes,
                semantic_state_bytes, supplied,
            )
        previous = snapshot["previous"]
        try:
            contract.verify_committed_checkpoint_context(
                manifest, machine, semantic, previous
            )
        except Exception as exc:
            self._fail("CHECKPOINT_CONTEXT_INVALID", _detail(exc))
        previous_task = snapshot["task"]
        authority = snapshot["authority"]
        candidate = self._candidate_task(previous_task, manifest, authority)
        candidate_bytes = contract.canonical_validated_bytes(
            "task-record/v1", candidate
        )
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        try:
            with self.database.transaction() as connection:
                replay = self._generation_row(
                    connection, manifest["task_id"], manifest["generation"]
                )
                if replay is not None:
                    return self._assert_replay(
                        replay, manifest_bytes, machine_state_bytes,
                        semantic_state_bytes, supplied, connection=connection,
                    )
                self._assert_predecessor(connection, manifest)
                current_task = self._load_task(connection, manifest["task_id"])
                current_authority = self._load_authority(
                    connection, manifest["task_id"]
                )
                if (
                    contract.canonical_document_bytes(current_task)
                    != contract.canonical_document_bytes(previous_task)
                    or current_authority != authority
                ):
                    self._fail("CHECKPOINT_CAS_CONFLICT")
                self._insert_objects(connection, supplied)
                self.fault_hook("after_objects")
                self._insert_manifest(
                    connection, manifest, manifest_bytes,
                    machine_state_bytes, semantic_state_bytes,
                )
                self.fault_hook("after_manifest")
                self._insert_index(connection, manifest)
                self.fault_hook("after_index")
                self._insert_task_record(
                    connection, candidate, candidate_bytes, candidate_sha256,
                    manifest["manifest_hash"],
                )
                self.fault_hook("after_task_record")
                self.fault_hook("before_head_cas")
                self._advance_heads(
                    connection, previous_task, candidate, candidate_sha256,
                    manifest,
                )
                self.fault_hook("after_head_cas")
                self.fault_hook("before_commit")
                return _commit_identity(manifest, authority)
        except CurrentCheckpointError:
            raise
        except CurrentAuthorityProjectionError as exc:
            self._fail("AUTHORITY_FENCED", exc.code)
        except sqlite3.Error as exc:
            self._fail("CHECKPOINT_WRITE_FAILED", type(exc).__name__)

    def _commit_snapshot(self, manifest: dict[str, object]) -> dict[str, object]:
        connection = self.database.connect()
        try:
            replay = self._generation_row(
                connection, manifest["task_id"], manifest["generation"]
            )
            if replay is not None:
                return {"replay": replay, "previous": None, "task": None,
                        "authority": None}
            self._assert_predecessor(connection, manifest)
            previous = self._previous_manifest(connection, manifest)
            task = self._load_task(connection, manifest["task_id"])
            authority = self._load_authority(connection, manifest["task_id"])
            return {"replay": None, "previous": previous, "task": task,
                    "authority": authority}
        finally:
            connection.close()

    def _validated_objects(
        self,
        objects: Mapping[str, tuple[str, bytes]],
        machine: dict[str, object],
        semantic: dict[str, object],
    ) -> dict[str, tuple[str, bytes]]:
        if not isinstance(objects, Mapping):
            self._fail("CHECKPOINT_OBJECT_SET_INVALID")
        supplied: dict[str, tuple[str, bytes]] = {}
        for presented, item in objects.items():
            if not isinstance(item, tuple) or len(item) != 2:
                self._fail("CHECKPOINT_OBJECT_SET_INVALID")
            schema_id, raw = item
            document = _parse_validated(schema_id, raw)
            canonical = contract.canonical_validated_bytes(schema_id, document)
            digest = hashlib.sha256(canonical).hexdigest()
            if presented != digest or canonical != raw:
                self._fail("CHECKPOINT_OBJECT_INVALID")
            supplied[digest] = (schema_id, raw)
        refs = [machine["workspace_state_ref"], machine["execution_state_ref"],
                semantic["task_request_ref"], semantic["requirement_ledger_ref"]]
        if semantic["charter_ref"] is not None:
            refs.append(semantic["charter_ref"])
        refs.extend(semantic["evidence_refs"])
        for ref in refs:
            item = supplied.get(ref["sha256"])
            if item is None or not _ref_matches(ref, *item):
                self._fail("CHECKPOINT_OBJECT_MISSING")
        return supplied

    def _candidate_task(
        self,
        previous: dict[str, object],
        manifest: dict[str, object],
        authority: ServerAuthorityEnvelope,
    ) -> dict[str, object]:
        exact = (
            previous["task_version"] == manifest["committed_from_task_version"]
            and previous["task_id"] == manifest["task_id"] == authority.task_id
            and previous["current_attempt_id"] == manifest["attempt_id"]
            == authority.attempt_id
            and previous["native_epoch"] == manifest["native_epoch"]
            == authority.native_epoch
            and previous["owner_epoch"] == manifest["owner_epoch"]
            == authority.owner_epoch
            and previous["deletion_version"] == authority.deletion_version
            and previous["lease_id"] == authority.lease_id
            and previous["transport_epoch"] == authority.transport_epoch
            and previous["lifecycle"] == "ACTIVE"
            and previous["desired_state"] == "RUN"
        )
        if not exact:
            self._fail("AUTHORITY_FENCED")
        candidate = deepcopy(previous)
        candidate.update(
            {
                "task_version": manifest["committed_task_version"],
                "current_checkpoint_generation": manifest["generation"],
                "current_checkpoint_hash": manifest["manifest_hash"],
                "updated_at": manifest["created_at"],
            }
        )
        try:
            return contract.validate_task_record_transition(previous, candidate)
        except Exception as exc:
            self._fail("CHECKPOINT_TASK_TRANSITION_INVALID", _detail(exc))

    def _load_task(
        self, connection: sqlite3.Connection, task_id: str
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT r.record_bytes FROM runtime_task_heads h "
            "JOIN runtime_task_records r USING(task_id,task_version,record_sha256) "
            "WHERE h.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            self._fail("CHECKPOINT_TASK_NOT_FOUND")
        return _parse_validated("task-record/v1", bytes(row["record_bytes"]))

    def _load_authority(
        self, connection: sqlite3.Connection, task_id: str
    ) -> ServerAuthorityEnvelope:
        projection = self.authority.load_head(connection, task_id)
        if not isinstance(projection, ServerAuthorityEnvelope):
            self._fail("AUTHORITY_FENCED")
        return projection

    @staticmethod
    def _generation_row(
        connection: sqlite3.Connection, task_id: str, generation: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT m.*, h.projection_digest AS authority_digest, "
            "h.deletion_version, h.transport_epoch "
            "FROM checkpoint_manifests m JOIN authority_history h "
            "ON h.task_id=m.task_id AND h.projection_kind='ACTIVE' "
            "WHERE m.task_id=? AND m.generation=? ORDER BY h.rowid LIMIT 1",
            (task_id, generation),
        ).fetchone()

    def _assert_predecessor(
        self, connection: sqlite3.Connection, manifest: dict[str, object]
    ) -> None:
        head = connection.execute(
            "SELECT generation, manifest_hash FROM checkpoint_heads WHERE task_id=?",
            (manifest["task_id"],),
        ).fetchone()
        expected = (0, None) if head is None else (head["generation"], head["manifest_hash"])
        if (
            manifest["previous_generation"],
            manifest["previous_manifest_hash"],
        ) != expected or manifest["generation"] != expected[0] + 1:
            self._fail("CHECKPOINT_CAS_CONFLICT")

    @staticmethod
    def _previous_manifest(
        connection: sqlite3.Connection, manifest: dict[str, object]
    ) -> dict[str, object] | None:
        if manifest["generation"] == 1:
            return None
        row = connection.execute(
            "SELECT manifest_bytes FROM checkpoint_manifests WHERE manifest_hash=?",
            (manifest["previous_manifest_hash"],),
        ).fetchone()
        if row is None:
            raise CurrentCheckpointError("CHECKPOINT_CAS_CONFLICT")
        return _parse_exact(MANIFEST_SCHEMA, bytes(row["manifest_bytes"]))

    def _assert_replay(
        self, row: sqlite3.Row, manifest_bytes: bytes, machine_bytes: bytes,
        semantic_bytes: bytes, supplied: Mapping[str, tuple[str, bytes]],
        *, connection: sqlite3.Connection | None = None,
    ) -> LocalCommittedCheckpoint:
        owned = connection is None
        selected = connection or self.database.connect()
        try:
            if bytes(row["manifest_bytes"]) != manifest_bytes:
                self._fail("CHECKPOINT_REPLAY_CONFLICT")
            stored = selected.execute(
                "SELECT machine_state_sha256,semantic_state_sha256 "
                "FROM checkpoint_manifests WHERE manifest_hash=?",
                (row["manifest_hash"],),
            ).fetchone()
            expected = (hashlib.sha256(machine_bytes).hexdigest(),
                        hashlib.sha256(semantic_bytes).hexdigest())
            if stored is None or tuple(stored) != expected:
                self._fail("CHECKPOINT_STORAGE_CORRUPT")
            for digest, (schema_id, raw) in supplied.items():
                found = selected.execute(
                    "SELECT content_schema_id,size_bytes,object_bytes "
                    "FROM checkpoint_objects WHERE sha256=?", (digest,),
                ).fetchone()
                if found is None or tuple(found[:2]) != (schema_id, len(raw)) \
                        or bytes(found[2]) != raw:
                    self._fail("CHECKPOINT_STORAGE_CORRUPT")
            return _commit_from_row(row)
        finally:
            if owned:
                selected.close()

    @staticmethod
    def _fail(code: str, detail: str = "") -> None:
        raise CurrentCheckpointError(code, detail)


__all__ = [
    "CurrentCheckpointError",
    "CurrentCheckpointStore",
    "LocalCommittedCheckpoint",
    "RecoveredCheckpoint",
]
