"""Atomic dual-layer checkpoint commits and ACK-gated recovery."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Callable, Mapping

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import (
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
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


class CurrentCheckpointStore:
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
                if row is None or _commit_from_row(row) != item:
                    self._fail("CHECKPOINT_ACK_INVALID")
                existing = connection.execute(
                    "SELECT * FROM checkpoint_server_acks "
                    "WHERE task_id=? AND generation=?",
                    (item.task_id, item.generation),
                ).fetchone()
                if existing is not None:
                    if _ack_matches(existing, item):
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
                if not _authority_matches_commit(authority, item):
                    self._fail("AUTHORITY_FENCED")
                connection.execute(
                    "INSERT INTO checkpoint_server_acks VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.task_id, item.generation, item.manifest_hash,
                        item.previous_manifest_hash, item.authority_digest,
                        item.committed_task_version, item.deletion_version,
                        item.transport_epoch, item.native_epoch, self.clock(),
                    ),
                )
                return item
        except CurrentCheckpointError:
            raise
        except (CurrentAuthorityProjectionError, sqlite3.Error) as exc:
            self._fail("CHECKPOINT_ACK_WRITE_FAILED", _detail(exc))

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

    def _insert_objects(
        self, connection: sqlite3.Connection,
        supplied: Mapping[str, tuple[str, bytes]],
    ) -> None:
        for digest, (schema_id, raw) in supplied.items():
            row = connection.execute(
                "SELECT content_schema_id,size_bytes,object_bytes "
                "FROM checkpoint_objects WHERE sha256=?", (digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO checkpoint_objects VALUES (?,?,?,?)",
                    (digest, schema_id, len(raw), raw),
                )
            elif tuple(row[:2]) != (schema_id, len(raw)) or bytes(row[2]) != raw:
                self._fail("CHECKPOINT_OBJECT_STORAGE_CORRUPT")

    def _insert_manifest(
        self, connection: sqlite3.Connection, manifest: dict[str, object],
        manifest_bytes: bytes, machine_bytes: bytes, semantic_bytes: bytes,
    ) -> None:
        for schema_id, raw in ((MACHINE_SCHEMA, machine_bytes),
                               (SEMANTIC_SCHEMA, semantic_bytes)):
            digest = hashlib.sha256(raw).hexdigest()
            row = connection.execute(
                "SELECT content_schema_id,size_bytes,object_bytes "
                "FROM checkpoint_objects WHERE sha256=?", (digest,),
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
            tuple(manifest[field] for field in (
                "manifest_hash", "task_id", "generation", "previous_generation",
                "previous_manifest_hash", "committed_from_task_version",
                "committed_task_version", "native_epoch", "attempt_id",
                "owner_epoch",
            )) + (
                manifest["machine_state_ref"]["sha256"],
                manifest["semantic_state_ref"]["sha256"],
                manifest_bytes, manifest["created_at"],
            ),
        )

    @staticmethod
    def _insert_index(
        connection: sqlite3.Connection, manifest: dict[str, object]
    ) -> None:
        connection.execute(
            "INSERT INTO checkpoint_index VALUES (?,?,?,?,?)",
            tuple(manifest[field] for field in (
                "task_id", "generation", "manifest_hash",
                "previous_manifest_hash", "created_at",
            )),
        )

    @staticmethod
    def _insert_task_record(
        connection: sqlite3.Connection, task: dict[str, object], raw: bytes,
        digest: str, source_digest: str,
    ) -> None:
        connection.execute(
            "INSERT INTO runtime_task_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task["task_id"], task["task_version"], digest, "CHECKPOINT",
                source_digest, task["lifecycle"], task["desired_state"],
                task["current_attempt_id"], task["native_epoch"],
                task["owner_epoch"], task["current_checkpoint_generation"],
                task["current_checkpoint_hash"], raw,
            ),
        )

    def _advance_heads(
        self, connection: sqlite3.Connection, previous: dict[str, object],
        task: dict[str, object], task_sha256: str,
        manifest: dict[str, object],
    ) -> None:
        old_sha = hashlib.sha256(
            contract.canonical_validated_bytes("task-record/v1", previous)
        ).hexdigest()
        updated = connection.execute(
            "UPDATE runtime_task_heads SET task_version=?, record_sha256=? "
            "WHERE task_id=? AND task_version=? AND record_sha256=?",
            (task["task_version"], task_sha256, task["task_id"],
             previous["task_version"], old_sha),
        ).rowcount
        if updated != 1:
            self._fail("CHECKPOINT_CAS_CONFLICT")
        head = connection.execute(
            "SELECT generation FROM checkpoint_heads WHERE task_id=?",
            (task["task_id"],),
        ).fetchone()
        values = tuple(manifest[field] for field in (
            "task_id", "generation", "manifest_hash", "previous_manifest_hash",
            "committed_task_version", "native_epoch", "attempt_id", "owner_epoch",
        ))
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

    def _recover_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> RecoveredCheckpoint:
        manifest_bytes = bytes(row["manifest_bytes"])
        manifest = _parse_exact(MANIFEST_SCHEMA, manifest_bytes)
        machine_bytes = self._object_bytes(
            connection, manifest["machine_state_ref"]
        )
        semantic_bytes = self._object_bytes(
            connection, manifest["semantic_state_ref"]
        )
        machine = _parse_exact(MACHINE_SCHEMA, machine_bytes)
        semantic = _parse_exact(SEMANTIC_SCHEMA, semantic_bytes)
        previous = self._previous_manifest(connection, manifest)
        try:
            contract.verify_committed_checkpoint_context(
                manifest, machine, semantic, previous
            )
        except Exception as exc:
            self._fail("CHECKPOINT_STORAGE_CORRUPT", _detail(exc))
        for ref in (machine["workspace_state_ref"], machine["execution_state_ref"],
                    semantic["task_request_ref"], semantic["requirement_ledger_ref"]):
            self._object_bytes(connection, ref)
        commit = _commit_from_row(row)
        return RecoveredCheckpoint(
            commit, commit.generation, manifest_bytes,
            machine_bytes, semantic_bytes,
        )

    def _object_bytes(
        self, connection: sqlite3.Connection, ref: dict[str, object]
    ) -> bytes:
        row = connection.execute(
            "SELECT content_schema_id,size_bytes,object_bytes "
            "FROM checkpoint_objects WHERE sha256=?", (ref["sha256"],),
        ).fetchone()
        if row is None:
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        raw = bytes(row["object_bytes"])
        if tuple(row[:2]) != (ref["content_schema_id"], ref["size_bytes"]) \
                or not _ref_matches(ref, row["content_schema_id"], raw):
            self._fail("CHECKPOINT_STORAGE_CORRUPT")
        _parse_validated(row["content_schema_id"], raw)
        return raw

    @staticmethod
    def _fail(code: str, detail: str = "") -> None:
        raise CurrentCheckpointError(code, detail)


def _parse_exact(schema_id: str, raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
        if contract.canonical_document_bytes(value) != raw:
            raise CurrentCheckpointError("CHECKPOINT_NONCANONICAL")
        return contract.verify_document_digest(schema_id, value)
    except CurrentCheckpointError:
        raise
    except Exception as exc:
        raise CurrentCheckpointError("CHECKPOINT_DOCUMENT_INVALID", _detail(exc)) from exc


def _parse_validated(schema_id: str, raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
        checked = contract.validate_document(schema_id, value)
        if contract.canonical_validated_bytes(schema_id, checked) != raw:
            raise CurrentCheckpointError("CHECKPOINT_NONCANONICAL")
        return checked
    except CurrentCheckpointError:
        raise
    except Exception as exc:
        raise CurrentCheckpointError("CHECKPOINT_OBJECT_INVALID", _detail(exc)) from exc


def _ref_matches(ref: dict[str, object], schema_id: str, raw: bytes) -> bool:
    return bool(
        ref["content_schema_id"] == schema_id
        and ref["sha256"] == hashlib.sha256(raw).hexdigest()
        and ref["size_bytes"] == len(raw)
        and ref["media_type"] == "application/json"
        and ref["encoding"] == "utf-8"
    )


def _commit_identity(
    manifest: dict[str, object], authority: ServerAuthorityEnvelope
) -> LocalCommittedCheckpoint:
    return LocalCommittedCheckpoint(
        task_id=manifest["task_id"], generation=manifest["generation"],
        manifest_hash=manifest["manifest_hash"],
        previous_manifest_hash=manifest["previous_manifest_hash"],
        committed_task_version=manifest["committed_task_version"],
        native_epoch=manifest["native_epoch"], attempt_id=manifest["attempt_id"],
        owner_epoch=manifest["owner_epoch"], authority_digest=authority.digest,
        deletion_version=authority.deletion_version,
        transport_epoch=authority.transport_epoch,
        committed_at=manifest["created_at"],
    )


def _commit_from_row(row: sqlite3.Row) -> LocalCommittedCheckpoint:
    return LocalCommittedCheckpoint(
        task_id=row["task_id"], generation=row["generation"],
        manifest_hash=row["manifest_hash"],
        previous_manifest_hash=row["previous_manifest_hash"],
        committed_task_version=row["committed_task_version"],
        native_epoch=row["native_epoch"], attempt_id=row["attempt_id"],
        owner_epoch=row["owner_epoch"], authority_digest=row["authority_digest"],
        deletion_version=row["deletion_version"],
        transport_epoch=row["transport_epoch"], committed_at=row["committed_at"],
    )


def _authority_matches_commit(
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


def _ack_matches(row: sqlite3.Row, item: LocalCommittedCheckpoint) -> bool:
    return tuple(row[field] for field in (
        "task_id", "generation", "manifest_hash", "previous_manifest_hash",
        "authority_digest", "task_version", "deletion_version",
        "transport_epoch", "native_epoch",
    )) == (
        item.task_id, item.generation, item.manifest_hash,
        item.previous_manifest_hash, item.authority_digest,
        item.committed_task_version, item.deletion_version,
        item.transport_epoch, item.native_epoch,
    )


def _detail(error: BaseException) -> str:
    return str(getattr(error, "code", type(error).__name__))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "CurrentCheckpointError",
    "CurrentCheckpointStore",
    "LocalCommittedCheckpoint",
    "RecoveredCheckpoint",
]
