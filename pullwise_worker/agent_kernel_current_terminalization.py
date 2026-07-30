"""Atomic preparation and freezing of the one current TaskResult candidate."""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping

from .agent_kernel_current_authority import (
    CurrentAuthorityProjection,
    CurrentAuthorityProjectionError,
)
from .agent_kernel_current_database import CurrentAgentKernelDatabase
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
                result = validate_terminal_result(
                    connection,
                    self.authority,
                    prepared,
                    task_result_bytes,
                    normalized,
                )
                all_objects = dict(normalized)
                for schema_id, raw in self._prepared_objects(prepared):
                    all_objects[object_sha256(raw)] = (schema_id, raw)
                all_objects[result.result_digest] = (
                    "task-result/v1",
                    result.canonical_bytes,
                )
                all_objects[result.core_sha256] = (
                    "task-result-core/v1",
                    result.core_bytes,
                )
                for schema_id, raw in all_objects.values():
                    put_object(connection, schema_id, raw)
                self.fault_hook("after_objects")
                existing = connection.execute(
                    "SELECT * FROM terminalization_candidates WHERE task_id=?",
                    (prepared.task_id,),
                ).fetchone()
                expected = self._candidate_values(prepared, result)
                if existing is not None:
                    if tuple(existing) != expected:
                        self._fail("TERMINALIZATION_REPLAY_CONFLICT")
                    return frozen_from_row(existing)
                connection.execute(
                    "INSERT INTO terminalization_candidates VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    expected,
                )
                self.fault_hook("after_candidate")
                self.fault_hook("before_commit")
                row = connection.execute(
                    "SELECT * FROM terminalization_candidates WHERE task_id=?",
                    (prepared.task_id,),
                ).fetchone()
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
