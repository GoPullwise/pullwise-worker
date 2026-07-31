"""TaskResult/Core validation against one prepared terminal gate."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Mapping

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_authority import CurrentAuthorityProjection
from .agent_kernel_current_requirements import (
    load_current_requirement_ledger,
)
from .agent_kernel_current_terminalization_contract import (
    CurrentTerminalizationError,
    PreparedTerminalization,
    canonical_bytes,
    collect_content_refs,
    error_detail,
    load_ref,
    object_sha256,
    parse_exact,
    ref_matches,
)


@dataclass(frozen=True)
class ValidatedTerminalResult:
    document: dict[str, object]
    canonical_bytes: bytes
    result_digest: str
    core_document: dict[str, object]
    core_bytes: bytes
    core_sha256: str
    effect_ledger_sha256: str
    budget_summary_sha256: str


def _memory_with_prepared(
    prepared: PreparedTerminalization,
    supplied: Mapping[str, tuple[str, bytes]],
) -> dict[str, tuple[str, bytes]]:
    memory = dict(supplied)
    values = (
        ("terminalization-input-snapshot/v1", prepared.terminalization_input_bytes),
        ("pre-gate-root-set/v1", prepared.root_set_bytes),
        (
            "pre-gate-evidence-closure-manifest/v1",
            prepared.pre_gate_closure_bytes,
        ),
        ("gate-decision/v1", prepared.gate_decision_bytes),
        ("evidence-closure-manifest/v1", prepared.evidence_closure_bytes),
    )
    for schema_id, raw in values:
        memory[object_sha256(raw)] = (schema_id, raw)
    for raw in prepared.terminalization_fact_bytes:
        memory[object_sha256(raw)] = ("terminalization-fact/v1", raw)
    return memory


def _assert_identity(
    connection: sqlite3.Connection,
    authority_store: CurrentAuthorityProjection,
    result: dict[str, object],
) -> None:
    authority = authority_store.load_head(connection, result["task_id"])
    authority_store.assert_runnable(authority)
    attempt = result["attempt_identity"]
    owner = result["owner_identity"]
    exact = (
        result["published_from_version"] == authority.task_version + 1
        and result["terminal_task_version"] == authority.task_version + 2
        and attempt["kind"] == owner["kind"] == "started"
        and attempt["attempt_id"] == authority.attempt_id
        and attempt["native_epoch"] == authority.native_epoch
        and owner["owner_id"] == authority.owner_id
        and owner["owner_epoch"] == authority.owner_epoch
    )
    if not exact:
        raise CurrentTerminalizationError("AUTHORITY_FENCED")


def _assert_requirement_coverage(
    connection: sqlite3.Connection,
    result: dict[str, object],
) -> None:
    current = load_current_requirement_ledger(connection, result["task_id"])
    ledger = parse_exact(
        "requirement-ledger/v1",
        current.canonical_bytes,
        code="REQUIREMENT_LEDGER_STORAGE_CORRUPT",
    )
    if not ref_matches(
        result["requirement_ledger_ref"],
        "requirement-ledger/v1",
        current.canonical_bytes,
    ):
        raise CurrentTerminalizationError("REQUIREMENT_LEDGER_STALE")
    results = {item["requirement_id"]: item for item in result["requirement_results"]}
    active = set(ledger["active_requirement_ids"])
    if set(results) != active:
        raise CurrentTerminalizationError("REQUIREMENT_COVERAGE_INVALID")
    outcome = result["outcome"]
    if outcome in {"COMPLETED", "NO_CHANGE_NEEDED"} and any(
        item["verdict"] != "PASS" or item["waiver_refs"]
        for item in results.values()
    ):
        raise CurrentTerminalizationError("MANDATORY_REQUIREMENT_NOT_PASS")
    if outcome == "COMPLETED_WITH_WAIVERS" and any(
        item["verdict"] != "PASS" and not item["waiver_refs"]
        for item in results.values()
    ):
        raise CurrentTerminalizationError("WAIVER_CLOSURE_INVALID")


def _assert_result_refs(
    connection: sqlite3.Connection,
    result: dict[str, object],
    closure: dict[str, object],
    memory: Mapping[str, tuple[str, bytes]],
) -> None:
    entries = {
        contract.canonical_document_bytes(item) for item in closure["entries"]
    }
    for ref in collect_content_refs(result):
        load_ref(connection, ref, memory)
        if ref["content_schema_id"] in {
            "evidence-closure-manifest/v1",
            "worker-debug-fragment-descriptor/v1",
        }:
            continue
        if contract.canonical_document_bytes(ref) not in entries:
            raise CurrentTerminalizationError("EVIDENCE_CLOSURE_INVALID")


def validate_terminal_result(
    connection: sqlite3.Connection,
    authority_store: CurrentAuthorityProjection,
    prepared: PreparedTerminalization,
    task_result_bytes: bytes,
    objects: Mapping[str, tuple[str, bytes]],
) -> ValidatedTerminalResult:
    result = parse_exact(
        "task-result/v1", task_result_bytes, code="TASK_RESULT_INVALID"
    )
    decision = parse_exact(
        "gate-decision/v1", prepared.gate_decision_bytes
    )
    closure = parse_exact(
        "evidence-closure-manifest/v1", prepared.evidence_closure_bytes
    )
    snapshot = parse_exact(
        "terminalization-input-snapshot/v1",
        prepared.terminalization_input_bytes,
    )
    memory = _memory_with_prepared(prepared, objects)
    effect, effect_raw = load_ref(
        connection, snapshot["effect_ledger_ref"], memory
    )
    _budget, budget_raw = load_ref(
        connection, snapshot["budget_summary_ref"], memory
    )
    expected = (
        prepared.task_id,
        prepared.outcome,
        prepared.reason_code,
        prepared.selector_input_digest,
        prepared.gate_decision_ref,
        prepared.evidence_closure_ref,
        snapshot["request_ref"],
        snapshot["policy_ref"],
        snapshot["requirement_ledger_ref"],
        snapshot["effect_ledger_ref"],
        snapshot["budget_summary_ref"],
    )
    actual = (
        result["task_id"],
        result["outcome"],
        result["reason_code"],
        result["selector_input_digest"],
        result["gate_decision"]["ref"],
        result["evidence_closure_ref"],
        result["request_ref"],
        result["policy_ref"],
        result["requirement_ledger_ref"],
        result["effect_ledger_ref"],
        result["budget_summary_ref"],
    )
    if actual != expected:
        raise CurrentTerminalizationError("TASK_RESULT_CONTEXT_INVALID")
    try:
        contract.verify_task_result_context(
            result,
            terminal_gate_decision=decision,
            effect_ledger_snapshot=effect,
            worker_debug_descriptor=None,
        )
        core = contract.derive_task_result_core(result)
        contract.verify_task_result_core(result, core)
    except Exception as exc:
        raise CurrentTerminalizationError(
            "TASK_RESULT_CONTEXT_INVALID", error_detail(exc)
        ) from exc
    _assert_identity(connection, authority_store, result)
    _assert_requirement_coverage(connection, result)
    _assert_result_refs(connection, result, closure, memory)
    core_raw = canonical_bytes("task-result-core/v1", core)
    return ValidatedTerminalResult(
        document=result,
        canonical_bytes=task_result_bytes,
        result_digest=object_sha256(task_result_bytes),
        core_document=core,
        core_bytes=core_raw,
        core_sha256=object_sha256(core_raw),
        effect_ledger_sha256=object_sha256(effect_raw),
        budget_summary_sha256=object_sha256(budget_raw),
    )


__all__ = ["ValidatedTerminalResult", "validate_terminal_result"]
