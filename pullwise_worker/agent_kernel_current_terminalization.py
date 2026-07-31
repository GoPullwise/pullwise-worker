"""Atomic preparation and freezing of the one current TaskResult candidate."""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping

from .agent_kernel_current_authority import (
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
from .agent_kernel_current_terminal_commit import (
    advance_runtime_head,
    build_terminal_commit,
    insert_runtime_records,
)
from .agent_kernel_current_terminal_replay import assert_terminal_commit
from .agent_kernel_current_terminal_gate import prepare_terminalization
from .agent_kernel_current_terminal_result import validate_terminal_result
from .agent_kernel_current_terminalization_contract import (
    CurrentTerminalizationError,
    FrozenTerminalization,
    PreparedTerminalization,
    frozen_from_row,
    normalize_objects,
    object_sha256,
    put_object,
)


class CurrentTerminalizationStore:
    def __init__(
        self,
        database: CurrentAgentKernelDatabase,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(database, CurrentAgentKernelDatabase):
            raise CurrentTerminalizationError("CURRENT_DATABASE_INVALID")
        if fault_hook is not None and not callable(fault_hook):
            raise CurrentTerminalizationError("TERMINALIZATION_HOOK_INVALID")
        self.database = database
        self.fault_hook = fault_hook or (lambda _stage: None)
        self.authority = CurrentAuthorityProjection(database)

    def prepare(
        self,
        *,
        terminalization_input_bytes: bytes,
        root_set_bytes: bytes,
        pre_gate_closure_bytes: bytes,
        terminalization_fact_bytes: tuple[bytes, ...],
        selector_axes: Mapping[str, str],
        objects: Mapping[str, tuple[str, bytes]],
    ) -> PreparedTerminalization:
        normalized = normalize_objects(objects)
        connection = self.database.connect()
        try:
            return prepare_terminalization(
                connection,
                self.authority,
                terminalization_input_bytes=terminalization_input_bytes,
                root_set_bytes=root_set_bytes,
                pre_gate_closure_bytes=pre_gate_closure_bytes,
                terminalization_fact_bytes=terminalization_fact_bytes,
                selector_axes=selector_axes,
                objects=normalized,
            )
        except CurrentTerminalizationError:
            raise
        except CurrentAuthorityProjectionError as exc:
            self._fail("AUTHORITY_FENCED", exc.code)
        finally:
            connection.close()

    def freeze(
        self,
        prepared: PreparedTerminalization,
        task_result_bytes: bytes,
        objects: Mapping[str, tuple[str, bytes]],
    ) -> FrozenTerminalization:
        if not isinstance(prepared, PreparedTerminalization):
            self._fail("TERMINALIZATION_PREPARED_INVALID")
        normalized = normalize_objects(objects)
        try:
            with self.database.transaction() as connection:
                existing = self._stored_row(connection, prepared.task_id)
                result = validate_terminal_result(
                    connection,
                    self.authority,
                    prepared,
                    task_result_bytes,
                    normalized,
                )
                if existing is not None:
                    expected = self._candidate_values(prepared, result)
                    if tuple(existing[: len(expected)]) != expected:
                        self._fail("TERMINALIZATION_REPLAY_CONFLICT")
                    documents = build_terminal_commit(
                        connection,
                        self.authority,
                        prepared,
                        result,
                        stored=existing,
                    )
                    self._assert_objects(
                        connection,
                        self._all_objects(
                            prepared, result, normalized, documents
                        ),
                    )
                    assert_terminal_commit(
                        connection, existing, documents
                    )
                    return frozen_from_row(existing)
                fresh = prepare_terminalization(
                    connection,
                    self.authority,
                    terminalization_input_bytes=(
                        prepared.terminalization_input_bytes
                    ),
                    root_set_bytes=prepared.root_set_bytes,
                    pre_gate_closure_bytes=prepared.pre_gate_closure_bytes,
                    terminalization_fact_bytes=(
                        prepared.terminalization_fact_bytes
                    ),
                    selector_axes=dict(prepared.selector_axes),
                    objects=normalized,
                )
                if fresh != prepared:
                    self._fail("TERMINALIZATION_PREPARED_STALE")
                documents = build_terminal_commit(
                    connection, self.authority, prepared, result
                )
                all_objects = self._all_objects(
                    prepared, result, normalized, documents
                )
                for schema_id, raw in all_objects.values():
                    put_object(connection, schema_id, raw)
                self.fault_hook("after_objects")
                expected = self._candidate_values(prepared, result)
                connection.execute(
                    "INSERT INTO terminalization_candidates VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    expected,
                )
                self.fault_hook("after_candidate")
                insert_runtime_records(connection, documents)
                self.fault_hook("after_runtime_records")
                connection.execute(
                    "INSERT INTO terminalization_commits VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._commit_values(result, documents),
                )
                self.fault_hook("after_commit")
                self.fault_hook("before_head")
                advance_runtime_head(connection, documents)
                self.fault_hook("before_commit")
                row = self._stored_row(connection, prepared.task_id)
                return frozen_from_row(row)
        except CurrentTerminalizationError:
            raise
        except CurrentAuthorityProjectionError as exc:
            self._fail("AUTHORITY_FENCED", exc.code)
        except sqlite3.IntegrityError as exc:
            self._fail("TERMINALIZATION_CONFLICT", type(exc).__name__)
        except sqlite3.Error as exc:
            self._fail("TERMINALIZATION_WRITE_FAILED", type(exc).__name__)

    @staticmethod
    def _prepared_objects(
        prepared: PreparedTerminalization,
    ) -> tuple[tuple[str, bytes], ...]:
        fixed = (
            (
                "terminalization-input-snapshot/v1",
                prepared.terminalization_input_bytes,
            ),
            ("pre-gate-root-set/v1", prepared.root_set_bytes),
            (
                "pre-gate-evidence-closure-manifest/v1",
                prepared.pre_gate_closure_bytes,
            ),
            ("gate-decision/v1", prepared.gate_decision_bytes),
            (
                "evidence-closure-manifest/v1",
                prepared.evidence_closure_bytes,
            ),
        )
        facts = tuple(
            ("terminalization-fact/v1", raw)
            for raw in prepared.terminalization_fact_bytes
        )
        return fixed + facts

    @staticmethod
    def _all_objects(prepared, result, normalized, documents):
        values = dict(normalized)
        for schema_id, raw in CurrentTerminalizationStore._prepared_objects(
            prepared
        ):
            values[object_sha256(raw)] = (schema_id, raw)
        values[result.result_digest] = (
            "task-result/v1",
            result.canonical_bytes,
        )
        values[result.core_sha256] = (
            "task-result-core/v1",
            result.core_bytes,
        )
        for schema_id, raw in documents.object_values():
            values[object_sha256(raw)] = (schema_id, raw)
        return values

    @staticmethod
    def _assert_objects(connection, objects) -> None:
        for digest, (schema_id, raw) in objects.items():
            row = connection.execute(
                "SELECT content_schema_id,size_bytes,object_bytes "
                "FROM checkpoint_objects WHERE sha256=?",
                (digest,),
            ).fetchone()
            if row is None or (
                row["content_schema_id"],
                row["size_bytes"],
                bytes(row["object_bytes"]),
            ) != (schema_id, len(raw), raw):
                raise CurrentTerminalizationError(
                    "TERMINALIZATION_STORAGE_CORRUPT"
                )

    @staticmethod
    def _stored_row(connection, task_id):
        row = connection.execute(
            "SELECT c.*,m.base_task_version,"
            "m.base_task_record_sha256,m.finalizing_task_record_sha256,"
            "m.terminal_task_record_sha256,"
            "m.terminalization_event_sha256,"
            "m.publication_event_sha256,"
            "m.task_version_authority_sha256,m.committed_at "
            "FROM terminalization_candidates c "
            "JOIN terminalization_commits m "
            "ON m.result_digest=c.result_digest WHERE c.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            candidate = connection.execute(
                "SELECT 1 FROM terminalization_candidates WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if candidate is not None:
                raise CurrentTerminalizationError(
                    "TERMINALIZATION_STORAGE_CORRUPT"
                )
        return row

    @staticmethod
    def _commit_values(result, documents) -> tuple[object, ...]:
        return (
            result.result_digest,
            result.document["task_id"],
            documents.base["task_version"],
            result.document["published_from_version"],
            result.document["terminal_task_version"],
            documents.base_sha256,
            documents.finalizing_sha256,
            documents.terminal_sha256,
            documents.requested_event_sha256,
            documents.published_event_sha256,
            documents.proof_sha256,
            result.document["terminal_at"],
        )

    @staticmethod
    def _candidate_values(
        prepared: PreparedTerminalization,
        result: object,
    ) -> tuple[object, ...]:
        document = result.document
        return (
            result.result_digest,
            document["result_id"],
            document["task_id"],
            document["outcome"],
            document["reason_code"],
            document["published_from_version"],
            document["terminal_task_version"],
            document["selector_input_digest"],
            object_sha256(prepared.terminalization_input_bytes),
            object_sha256(prepared.root_set_bytes),
            object_sha256(prepared.pre_gate_closure_bytes),
            object_sha256(prepared.gate_decision_bytes),
            object_sha256(prepared.evidence_closure_bytes),
            result.effect_ledger_sha256,
            result.budget_summary_sha256,
            result.core_sha256,
            document["terminal_at"],
        )

    @staticmethod
    def _fail(code: str, detail: str = "") -> None:
        raise CurrentTerminalizationError(code, detail)


__all__ = [
    "CurrentTerminalizationError",
    "CurrentTerminalizationStore",
    "FrozenTerminalization",
    "PreparedTerminalization",
]
