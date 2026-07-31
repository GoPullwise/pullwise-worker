"""Mechanical terminal gate preparation over exact closed evidence."""

from __future__ import annotations

from copy import deepcopy
import hashlib
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
    content_ref,
    error_detail,
    load_ref,
    object_sha256,
    parse_exact,
    ref_matches,
)


_DOCUMENTS = (
    ("pre-gate-root-set/v1", "root_set_bytes"),
    (
        "pre-gate-evidence-closure-manifest/v1",
        "pre_gate_closure_bytes",
    ),
    ("terminalization-input-snapshot/v1", "terminalization_input_bytes"),
)
_AXES = {"gate_mode", "cancel_state", "cause_family", "delivery_state"}
_CAUSE_BY_REASON = {
    "BUDGET_EXHAUSTED": "budget_exhausted",
    "CAPABILITY_UNAVAILABLE": "capability_unavailable",
    "DEADLINE_REACHED": "deadline_reached",
    "INTERACTION_UNAVAILABLE": "interaction_unavailable",
    "POLICY_INVARIANT_BROKEN": "policy_invariant_broken",
    "PROTOCOL_FAILURE": "protocol_failure",
    "RUNTIME_FAILURE": "runtime_failure",
    "STORAGE_FAILURE": "storage_failure",
}


def _task_record(
    connection: sqlite3.Connection, task_id: str
) -> dict[str, object]:
    row = connection.execute(
        "SELECT r.record_bytes FROM runtime_task_heads h "
        "JOIN runtime_task_records r USING(task_id,task_version,record_sha256) "
        "WHERE h.task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise CurrentTerminalizationError("TERMINALIZATION_TASK_NOT_FOUND")
    return parse_exact(
        "task-record/v1",
        bytes(row["record_bytes"]),
        code="TERMINALIZATION_TASK_CORRUPT",
    )


def _assert_authority(
    connection: sqlite3.Connection,
    authority_store: CurrentAuthorityProjection,
    snapshot: dict[str, object],
) -> object:
    authority = authority_store.load_head(connection, snapshot["task_id"])
    authority_store.assert_runnable(authority)
    exact = (
        snapshot["task_id"] == authority.task_id
        and snapshot["attempt_id"] == authority.attempt_id
        and snapshot["native_epoch"] == authority.native_epoch
        and snapshot["owner_id"] == authority.owner_id
        and snapshot["owner_epoch"] == authority.owner_epoch
        and snapshot["task_version"] == authority.task_version + 1
        and snapshot["deletion_version"] == authority.deletion_version
        and snapshot["lease_id"] == authority.lease_id
        and snapshot["desired_state"] == authority.desired_state == "RUN"
        and snapshot["absolute_deadline_at"]
        == authority.grant.absolute_deadline_at
        and snapshot["terminal_budget_reserved_ms"]
        == authority.terminalization_reserve_ms
    )
    if not exact:
        raise CurrentTerminalizationError("AUTHORITY_FENCED")
    return authority


def _assert_control_roots(
    connection: sqlite3.Connection,
    snapshot: dict[str, object],
    root: dict[str, object],
) -> None:
    task = _task_record(connection, snapshot["task_id"])
    ledger = load_current_requirement_ledger(connection, snapshot["task_id"])
    exact = (
        task["task_version"] + 1 == snapshot["task_version"]
        and task["lifecycle"] == "ACTIVE"
        and snapshot["lifecycle"] == "FINALIZING"
        and task["desired_state"] == snapshot["desired_state"] == "RUN"
        and task["deletion_version"] == snapshot["deletion_version"]
        and task["current_attempt_id"] == snapshot["attempt_id"]
        and task["native_epoch"] == snapshot["native_epoch"]
        and task["owner_id"] == snapshot["owner_id"]
        and task["owner_epoch"] == snapshot["owner_epoch"]
        and task["lease_id"] == snapshot["lease_id"]
    )
    if not exact:
        raise CurrentTerminalizationError("GATE_INPUT_STALE")
    for field in ("request_ref", "policy_ref"):
        if snapshot[field] != task[field]:
            raise CurrentTerminalizationError("GATE_INPUT_STALE")
    if root["request"]["ref"] != task["request_ref"]:
        raise CurrentTerminalizationError("GATE_INPUT_STALE")
    if root["policy"]["ref"] != task["policy_ref"]:
        raise CurrentTerminalizationError("GATE_INPUT_STALE")
    ledger_ref = snapshot["requirement_ledger_ref"]
    if (
        root["ledger"]["ref"] != ledger_ref
        or not ref_matches(
            ledger_ref, "requirement-ledger/v1", ledger.canonical_bytes
        )
    ):
        raise CurrentTerminalizationError("GATE_INPUT_STALE")


def _assert_budget(
    connection: sqlite3.Connection,
    budget: dict[str, object],
) -> None:
    row = connection.execute(
        "SELECT * FROM dispatch_budgets WHERE task_id=? AND grant_digest=?",
        (budget["task_id"], budget["grant_digest"]),
    ).fetchone()
    expected = (
        budget["elapsed_limit_ms"],
        budget["consumed_ms"],
        budget["tool_call_limit"],
        budget["calls_consumed"],
        0,
        0,
        0,
    )
    actual = None if row is None else (
        row["elapsed_limit_ms"],
        row["consumed_ms"],
        row["tool_call_limit"],
        row["calls_consumed"],
        row["reserved_ms"],
        row["calls_reserved"],
        budget["unsettled_reservations"],
    )
    if actual != expected:
        raise CurrentTerminalizationError("BUDGET_CLOSURE_INVALID")


def _effect_state(
    snapshot: dict[str, object], ledger: dict[str, object]
) -> str:
    counts = ledger["state_counts"]
    if counts["prepared"] or counts["dispatched"]:
        raise CurrentTerminalizationError("EFFECT_CLOSURE_ACTIVE")
    if ledger["rows"]:
        raise CurrentTerminalizationError("EFFECT_LEDGER_UNBACKED")
    if counts["unknown"]:
        return (
            "unknown_pre_deadline"
            if snapshot["trusted_wall_time_at"] < snapshot["absolute_deadline_at"]
            else "unknown_post_deadline"
        )
    return "committed" if counts["committed"] else "none"


def _predicate_results(
    snapshot_ref: dict[str, object],
    fact_refs: list[dict[str, object]],
    effect_ref: dict[str, object],
    publication_ref: dict[str, object],
) -> list[dict[str, object]]:
    evidence = {
        "GATE_TERMINAL_AUTHORITY_FACT": fact_refs,
        "GATE_TERMINAL_AVAILABILITY": [snapshot_ref],
        "GATE_TERMINAL_NO_ACTIVE_EFFECTS": [effect_ref],
        "GATE_TERMINAL_OUTCOME_CLASSIFICATION": fact_refs,
        "GATE_TERMINAL_ARTIFACT_DELIVERY": [publication_ref],
    }
    return [
        {
            "predicate_id": item["predicate_id"],
            "passed": True,
            "failure_code": None,
            "repairable": False,
            "evidence_refs": deepcopy(evidence[item["predicate_id"]]),
        }
        for item in contract.gate_predicate_registry()["predicates"]
        if item["decision_kind"] == "terminalization"
    ]


def _make_evidence_closure(
    snapshot: dict[str, object],
    pre_gate: dict[str, object],
    decision: dict[str, object],
) -> dict[str, object]:
    input_ref = content_ref("terminalization-input-snapshot/v1", snapshot)
    decision_ref = content_ref("gate-decision/v1", decision)
    candidates = [
        *pre_gate["entries"],
        deepcopy(snapshot["pre_gate_evidence_closure_ref"]),
        input_ref,
        decision_ref,
    ]
    unique = {contract.canonical_document_bytes(item): item for item in candidates}
    entries = sorted(
        unique.values(),
        key=lambda item: (
            item["content_schema_id"],
            item["artifact_id"],
            item["sha256"],
        ),
    )
    return contract.seal_document(
        "evidence-closure-manifest/v1",
        {
            "schema_id": "evidence-closure-manifest/v1",
            "task_id": snapshot["task_id"],
            "pre_gate_evidence_closure_ref": deepcopy(
                snapshot["pre_gate_evidence_closure_ref"]
            ),
            "input_snapshot_ref": input_ref,
            "gate_decision_ref": decision_ref,
            "entries": entries,
            "entry_count": len(entries),
            "evidence_closure_digest": hashlib.sha256(
                contract.canonical_document_bytes(entries)
            ).hexdigest(),
        },
    )


def prepare_terminalization(
    connection: sqlite3.Connection,
    authority_store: CurrentAuthorityProjection,
    *,
    terminalization_input_bytes: bytes,
    root_set_bytes: bytes,
    pre_gate_closure_bytes: bytes,
    terminalization_fact_bytes: tuple[bytes, ...],
    selector_axes: Mapping[str, str],
    objects: Mapping[str, tuple[str, bytes]],
) -> PreparedTerminalization:
    if not isinstance(terminalization_fact_bytes, tuple) or not terminalization_fact_bytes:
        raise CurrentTerminalizationError("TERMINALIZATION_FACT_SET_INVALID")
    raw_documents = {
        "terminalization_input_bytes": terminalization_input_bytes,
        "root_set_bytes": root_set_bytes,
        "pre_gate_closure_bytes": pre_gate_closure_bytes,
    }
    documents = {
        field: parse_exact(schema_id, raw_documents[field])
        for schema_id, field in _DOCUMENTS
    }
    root = documents["root_set_bytes"]
    pre_gate = documents["pre_gate_closure_bytes"]
    snapshot = documents["terminalization_input_bytes"]
    facts = tuple(
        parse_exact("terminalization-fact/v1", raw)
        for raw in terminalization_fact_bytes
    )
    memory = dict(objects)
    for schema_id, field in _DOCUMENTS:
        raw = raw_documents[field]
        memory[object_sha256(raw)] = (schema_id, raw)
    for raw in terminalization_fact_bytes:
        memory[object_sha256(raw)] = ("terminalization-fact/v1", raw)
    try:
        contract.verify_pre_gate_root_set_context(root, snapshot["task_id"])
        contract.verify_pre_gate_evidence_closure_context(pre_gate, root)
        contract.verify_terminalization_input_snapshot_context(
            snapshot, root, pre_gate, list(facts)
        )
        for fact in facts:
            contract.verify_terminalization_fact_context(
                fact,
                snapshot["task_id"],
                snapshot["task_version"] - 1,
                "ACTIVE",
            )
    except Exception as exc:
        raise CurrentTerminalizationError(
            "TERMINALIZATION_CONTEXT_INVALID", error_detail(exc)
        ) from exc
    _assert_authority(connection, authority_store, snapshot)
    _assert_control_roots(connection, snapshot, root)
    for ref in pre_gate["entries"]:
        load_ref(connection, ref, memory)
    effect, _ = load_ref(connection, snapshot["effect_ledger_ref"], memory)
    budget, _ = load_ref(connection, snapshot["budget_summary_ref"], memory)
    publication, _ = load_ref(
        connection, snapshot["publication_content_manifest_ref"], memory
    )
    load_ref(connection, snapshot["debug_redaction_plan_ref"], memory)
    _assert_budget(connection, budget)
    state = _effect_state(snapshot, effect)
    if set(selector_axes) != _AXES or any(
        not isinstance(value, str) for value in selector_axes.values()
    ):
        raise CurrentTerminalizationError("TERMINAL_SELECTOR_INVALID")
    if selector_axes["gate_mode"] != "none":
        raise CurrentTerminalizationError("SUCCESS_GATE_REQUIRED")
    expected_cause = _CAUSE_BY_REASON[facts[-1]["reason_code"]]
    if (
        selector_axes["cancel_state"] == "none"
        and selector_axes["cause_family"] != expected_cause
    ):
        raise CurrentTerminalizationError("TERMINAL_SELECTOR_INVALID")
    snapshot_ref = content_ref(
        "terminalization-input-snapshot/v1", snapshot
    )
    context = {
        "input_snapshot_ref": snapshot_ref,
        "profile": "task_result",
        **dict(selector_axes),
        "effect_state": state,
        "source_availability": deepcopy(snapshot["final_source"]),
        "evidence_availability": {
            "availability": "unavailable",
            "reason_code": "VERIFICATION_NOT_RUN",
        },
        "effect_availability": {
            "availability": "available",
            "ref": deepcopy(snapshot["effect_ledger_ref"]),
        },
        "effect_ledger": effect,
        "predicate_results": _predicate_results(
            snapshot_ref,
            list(snapshot["terminalization_fact_refs"]),
            snapshot["effect_ledger_ref"],
            snapshot["publication_content_manifest_ref"],
        ),
    }
    try:
        decision = contract.evaluate_terminalization_gate(snapshot, context)
    except Exception as exc:
        raise CurrentTerminalizationError(
            "TERMINAL_GATE_REJECTED", error_detail(exc)
        ) from exc
    if not decision["passed"] or decision["selected_lifecycle"] != "TERMINAL":
        raise CurrentTerminalizationError("TERMINAL_GATE_REJECTED")
    decision_raw = canonical_bytes("gate-decision/v1", decision)
    memory[object_sha256(decision_raw)] = ("gate-decision/v1", decision_raw)
    closure = _make_evidence_closure(snapshot, pre_gate, decision)
    try:
        contract.verify_evidence_closure_context(closure, pre_gate)
    except Exception as exc:
        raise CurrentTerminalizationError(
            "EVIDENCE_CLOSURE_INVALID", error_detail(exc)
        ) from exc
    closure_raw = canonical_bytes("evidence-closure-manifest/v1", closure)
    memory[object_sha256(closure_raw)] = (
        "evidence-closure-manifest/v1",
        closure_raw,
    )
    for ref in closure["entries"]:
        load_ref(connection, ref, memory)
    return PreparedTerminalization(
        task_id=snapshot["task_id"],
        outcome=decision["selected_outcome"],
        reason_code=decision["selected_reason"],
        selector_input_digest=decision["selector_input_digest"],
        terminalization_input_bytes=terminalization_input_bytes,
        root_set_bytes=root_set_bytes,
        pre_gate_closure_bytes=pre_gate_closure_bytes,
        terminalization_fact_bytes=terminalization_fact_bytes,
        selector_axes=tuple(sorted(selector_axes.items())),
        gate_decision_bytes=decision_raw,
        evidence_closure_bytes=closure_raw,
        gate_decision_ref=content_ref("gate-decision/v1", decision),
        evidence_closure_ref=content_ref(
            "evidence-closure-manifest/v1", closure
        ),
    )


__all__ = ["prepare_terminalization"]
