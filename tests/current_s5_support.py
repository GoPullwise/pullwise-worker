from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Mapping

from pullwise_worker import _generated_agent_task_contract as contract
from pullwise_worker.agent_kernel_current_package import (
    AgentClaimAbandonResponse,
    canonical_validated_current_bytes,
    seal_current_document,
)


def canonical_bytes(schema_id: str, document: dict[str, object]) -> bytes:
    return contract.canonical_validated_bytes(schema_id, document)


def successor_ledger(
    previous: dict[str, object],
    *,
    suffix: str = "5",
) -> dict[str, object]:
    candidate = deepcopy(previous)
    candidate.pop("ledger_digest")
    candidate["ledger_version"] = previous["ledger_version"] + 1
    parent_id = previous["active_requirement_ids"][0]
    requirement_id = "req_derived_" + suffix * 64
    entry = {
        "schema_id": "requirement-entry/v1",
        "requirement_id": requirement_id,
        "ledger_version": candidate["ledger_version"],
        "source_kind": "derived",
        "source_id": f"derived-{suffix}",
        "statement": f"Preserve derived invariant {suffix}.",
        "mandatory": True,
        "necessity": "safety_necessary",
        "rationale": "Required to preserve the accepted objective.",
        "parent_requirement_ids": [parent_id],
        "supersedes": [],
        "introduced_by": {
            "schema_id": "actor/v1",
            "kind": "task_owner",
            "id": "owner_" + "0" * 31 + suffix,
            "session_id": "sess_" + "0" * 31 + suffix,
        },
        "introduced_at": f"2026-07-22T00:00:0{suffix}.000Z",
    }
    candidate["entries"].append(entry)
    candidate["active_requirement_ids"] = sorted(
        [*candidate["active_requirement_ids"], requirement_id]
    )
    return contract.seal_document("requirement-ledger/v1", candidate)


def fenced_authority(authority: object) -> AgentClaimAbandonResponse:
    document = seal_current_document(
        "agent-claim-abandon-response/v1",
        {
            "schema_id": "agent-claim-abandon-response/v1",
            "package": authority.package.as_document(),
            "task_id": authority.task_id,
            "attempt_id": authority.attempt_id,
            "session_id": authority.session_id,
            "owner_id": authority.owner_id,
            "grant_id": authority.grant.grant_id,
            "lease_id": authority.lease_id,
            "previous_task_version": authority.task_version,
            "task_version": authority.task_version + 1,
            "deletion_version": authority.deletion_version,
            "owner_epoch": authority.owner_epoch,
            "native_epoch": authority.native_epoch,
            "transport_epoch": authority.transport_epoch,
            "state": "FENCED",
            "grant": authority.grant.as_document(),
            "superseded_authority_digest": authority.digest,
            "reason": "authority_revoked",
            "abandoned_at": "2026-07-22T00:00:09.000Z",
        },
    )
    raw = canonical_validated_current_bytes(
        "agent-claim-abandon-response/v1", document
    )
    return AgentClaimAbandonResponse.from_canonical_bytes(raw)


def object_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fixture_document(fixture_id: str) -> dict[str, object]:
    return deepcopy(contract.fixture(fixture_id)["document"])


def reseal(schema_id: str, document: dict[str, object]) -> dict[str, object]:
    candidate = deepcopy(document)
    digest = contract.schema(schema_id).get("x-pullwise-digest")
    if isinstance(digest, dict):
        candidate.pop(digest["field"], None)
    return contract.seal_document(schema_id, candidate)


def content_ref(
    artifact_suffix: str,
    schema_id: str,
    document: dict[str, object],
) -> dict[str, object]:
    raw = canonical_bytes(schema_id, document)
    return {
        "schema_id": "content-ref/v1",
        "artifact_id": "art_" + artifact_suffix * 32,
        "content_schema_id": schema_id,
        "sha256": object_sha256(raw),
        "size_bytes": len(raw),
        "media_type": "application/json",
        "encoding": "utf-8",
    }


def _pre_gate_manifest(root: dict[str, object]) -> dict[str, object]:
    root_ref = content_ref("a", "pre-gate-root-set/v1", root)
    entries: list[dict[str, object]] = []
    for field, value in root.items():
        if field in {"schema_id", "task_id", "root_set_digest"}:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if (
                isinstance(item, dict)
                and item.get("availability") == "available"
            ):
                entries.append(deepcopy(item["ref"]))
    entries.append(root_ref)
    unique = {contract.canonical_document_bytes(item): item for item in entries}
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item["content_schema_id"],
            item["artifact_id"],
            item["sha256"],
        ),
    )
    return reseal(
        "pre-gate-evidence-closure-manifest/v1",
        {
            "schema_id": "pre-gate-evidence-closure-manifest/v1",
            "task_id": root["task_id"],
            "pre_gate_root_set_ref": root_ref,
            "entries": ordered,
            "entry_count": len(ordered),
            "pre_gate_closure_digest": object_sha256(
                contract.canonical_document_bytes(ordered)
            ),
        },
    )


def terminalization_inputs(
    database: object,
    authority: object,
    *,
    source_available: bool = False,
) -> dict[str, object]:
    bootstrap = __import__(
        "tests.current_runtime_bootstrap_support",
        fromlist=["golden_runtime_bootstrap"],
    ).golden_runtime_bootstrap()
    accepted = bootstrap["accept_request"]
    request = accepted["task_request"]
    policy = accepted["effective_policy"]
    ledger = accepted["requirement_ledger"]
    task = bootstrap["construction_roots"]["task_record"]

    budget = fixture_document("publication_golden_budget_summary")
    budget.update(
        {
            "task_id": authority.task_id,
            "grant_digest": authority.grant_digest,
            "elapsed_limit_ms": authority.grant.elapsed_limit_ms,
            "consumed_ms": 0,
            "tool_call_limit": authority.grant.tool_call_limit,
            "calls_consumed": 0,
            "unsettled_reservations": 0,
        }
    )
    budget = reseal("budget-summary/v1", budget)
    effects = fixture_document("publication_golden_effect_ledger")
    effects.update(
        {
            "task_id": authority.task_id,
            "rows": [],
            "watermark": 0,
            "state_counts": {
                "prepared": 0,
                "dispatched": 0,
                "committed": 0,
                "not_applied": 0,
                "rejected": 0,
                "unknown": 0,
            },
        }
    )
    effects = reseal("effect-ledger-snapshot/v1", effects)
    publication = fixture_document(
        "gate_preparation_golden_publication_manifest"
    )
    publication["task_id"] = authority.task_id
    publication["entries"] = [deepcopy(publication["entries"][1])]
    publication["entry_count"] = 1
    publication = reseal("publication-content-manifest/v1", publication)
    debug_plan = fixture_document("gate_preparation_golden_debug_plan")
    debug_plan["task_id"] = authority.task_id
    debug_plan["debug_input_refs"] = []
    debug_plan = reseal("debug-redaction-plan/v1", debug_plan)

    refs = {
        "request": deepcopy(task["request_ref"]),
        "policy": deepcopy(task["policy_ref"]),
        "ledger": content_ref("3", "requirement-ledger/v1", ledger),
        "budget": content_ref("4", "budget-summary/v1", budget),
        "effects": content_ref("5", "effect-ledger-snapshot/v1", effects),
        "publication": content_ref(
            "6", "publication-content-manifest/v1", publication
        ),
        "debug": content_ref("7", "debug-redaction-plan/v1", debug_plan),
    }
    fact = fixture_document("gate_preparation_golden_terminalization_fact")
    fact.update(
        {
            "task_id": authority.task_id,
            "observed_task_version": authority.task_version,
            "reason_code": "CAPABILITY_UNAVAILABLE",
            "idempotency_key": (
                "terminalize:capability_unavailable:"
                + str(authority.task_version)
            ),
            "evidence_refs": [deepcopy(refs["budget"])],
            "observed_at": "2026-07-22T00:00:30.000Z",
        }
    )
    fact = reseal("terminalization-fact/v1", fact)
    refs["fact"] = content_ref("8", "terminalization-fact/v1", fact)

    root = fixture_document("pre_gate_golden_terminal_root_set")
    root["task_id"] = authority.task_id
    root["request"] = {"availability": "available", "ref": refs["request"]}
    root["policy"] = {"availability": "available", "ref": refs["policy"]}
    root["ledger"] = {"availability": "available", "ref": refs["ledger"]}
    root["budget_summary"] = {
        "availability": "available",
        "ref": refs["budget"],
    }
    root["effect_ledger"] = {
        "availability": "available",
        "ref": refs["effects"],
    }
    root["publication_content_manifest"] = {
        "availability": "available",
        "ref": refs["publication"],
    }
    root["debug_redaction_plan"] = {
        "availability": "available",
        "ref": refs["debug"],
    }
    root["termination_facts"] = [
        {"availability": "available", "ref": refs["fact"]}
    ]
    source = None
    if source_available:
        source = fixture_document("source_evidence_golden_source_tree")
        refs["source"] = content_ref("9", "source-tree-manifest/v1", source)
        root["original_source"] = {
            "availability": "available",
            "ref": refs["source"],
        }
        root["final_source"] = deepcopy(root["original_source"])
    root = reseal("pre-gate-root-set/v1", root)
    pre_gate = _pre_gate_manifest(root)

    snapshot = fixture_document(
        "gate_input_golden_terminalization_snapshot"
    )
    snapshot.update(
        {
            "task_id": authority.task_id,
            "attempt_id": authority.attempt_id,
            "native_epoch": authority.native_epoch,
            "owner_id": authority.owner_id,
            "owner_epoch": authority.owner_epoch,
            "task_version": authority.task_version + 1,
            "deletion_version": authority.deletion_version,
            "lifecycle": "FINALIZING",
            "desired_state": "RUN",
            "lease_id": authority.lease_id,
            "outer_lease_expires_at": "2026-07-22T00:00:50.000Z",
            "outer_lease_grace_expires_at": "2026-07-22T00:01:00.000Z",
            "absolute_deadline_at": authority.grant.absolute_deadline_at,
            "trusted_wall_time_at": "2026-07-22T00:00:40.000Z",
            "monotonic_deadline_remaining_ms": 20_000,
            "terminal_budget_reserved_ms": 1_000,
            "predicate_registry_digest": contract.gate_predicate_registry()[
                "registry_digest"
            ],
            "request_ref": refs["request"],
            "policy_ref": refs["policy"],
            "requirement_ledger_ref": refs["ledger"],
            "original_source": deepcopy(root["original_source"]),
            "final_source": deepcopy(root["final_source"]),
            "final_observation_manifest": deepcopy(
                root["final_observation_manifest"]
            ),
            "effect_ledger_ref": refs["effects"],
            "budget_summary_ref": refs["budget"],
            "publication_content_manifest_ref": refs["publication"],
            "debug_redaction_plan_ref": refs["debug"],
            "terminalization_fact_refs": [refs["fact"]],
            "pre_gate_root_set_ref": content_ref(
                "a", "pre-gate-root-set/v1", root
            ),
            "pre_gate_evidence_closure_ref": content_ref(
                "b", "pre-gate-evidence-closure-manifest/v1", pre_gate
            ),
            "pre_gate_closure_digest": pre_gate[
                "pre_gate_closure_digest"
            ],
        }
    )
    snapshot = reseal("terminalization-input-snapshot/v1", snapshot)
    objects = {}
    for schema_id, document in (
        ("budget-summary/v1", budget),
        ("effect-ledger-snapshot/v1", effects),
        ("publication-content-manifest/v1", publication),
        ("debug-redaction-plan/v1", debug_plan),
        *((("source-tree-manifest/v1", source),) if source is not None else ()),
    ):
        raw = canonical_bytes(schema_id, document)
        objects[object_sha256(raw)] = (schema_id, raw)
    return {
        "terminalization_input_bytes": canonical_bytes(
            "terminalization-input-snapshot/v1", snapshot
        ),
        "root_set_bytes": canonical_bytes("pre-gate-root-set/v1", root),
        "pre_gate_closure_bytes": canonical_bytes(
            "pre-gate-evidence-closure-manifest/v1", pre_gate
        ),
        "terminalization_fact_bytes": (
            canonical_bytes("terminalization-fact/v1", fact),
        ),
        "selector_axes": {
            "gate_mode": "none",
            "cancel_state": "none",
            "cause_family": "capability_unavailable",
            "delivery_state": "none",
        },
        "objects": objects,
        "documents": {
            "request": request,
            "policy": policy,
            "ledger": ledger,
            "budget": budget,
            "effects": effects,
            "publication": publication,
            "debug": debug_plan,
            "fact": fact,
            "root": root,
            "pre_gate": pre_gate,
            "snapshot": snapshot,
        },
    }


def blocked_task_result_bytes(
    authority: object,
    prepared: object,
    documents: Mapping[str, dict[str, object]],
) -> bytes:
    result = fixture_document("task_result_golden_completed")
    ledger = documents["ledger"]
    snapshot = documents["snapshot"]
    result.update(
        {
            "result_id": "result_" + "9" * 32,
            "task_id": authority.task_id,
            "task_type": documents["request"]["task_type"],
            "outcome": prepared.outcome,
            "reason_code": prepared.reason_code,
            "summary": "The task is blocked by an unavailable capability.",
            "outcome_details": {
                "kind": "blocked",
                "blockers": [
                    {
                        "code": "CAPABILITY_UNAVAILABLE",
                        "requirement_ids": list(
                            ledger["active_requirement_ids"]
                        ),
                        "unblock_condition": (
                            "Grant the required execution capability."
                        ),
                    }
                ],
            },
            "published_from_version": authority.task_version + 1,
            "terminal_task_version": authority.task_version + 2,
            "attempt_identity": {
                "kind": "started",
                "attempt_id": authority.attempt_id,
                "native_epoch": authority.native_epoch,
            },
            "owner_identity": {
                "kind": "started",
                "owner_id": authority.owner_id,
                "owner_epoch": authority.owner_epoch,
            },
            "request_ref": deepcopy(snapshot["request_ref"]),
            "policy_ref": deepcopy(snapshot["policy_ref"]),
            "requirement_ledger_ref": deepcopy(
                snapshot["requirement_ledger_ref"]
            ),
            "charter": {
                "availability": "unavailable",
                "reason_code": "CHARTER_NOT_CREATED",
            },
            "requirement_results": [
                {
                    "requirement_id": requirement_id,
                    "verdict": "UNVERIFIABLE",
                    "evidence_refs": [],
                    "attestation_refs": [],
                    "waiver_refs": [],
                }
                for requirement_id in ledger["active_requirement_ids"]
            ],
            "original_source_state": deepcopy(snapshot["original_source"]),
            "final_source_state": deepcopy(snapshot["final_source"]),
            "execution_states": [
                {
                    "availability": "unavailable",
                    "reason_code": "EXECUTION_STATE_UNAVAILABLE",
                }
            ],
            "change_set_ref": None,
            "completion_proposal": {
                "availability": "not_applicable",
                "reason_code": "PROPOSAL_NOT_CREATED",
            },
            "observation_manifest": deepcopy(
                snapshot["final_observation_manifest"]
            ),
            "attestations": {
                "availability": "not_applicable",
                "reason_code": "ATTESTATIONS_NOT_CREATED",
            },
            "gate_decision": {
                "availability": "available",
                "ref": deepcopy(prepared.gate_decision_ref),
            },
            "selector_input_digest": prepared.selector_input_digest,
            "evidence_closure_ref": deepcopy(
                prepared.evidence_closure_ref
            ),
            "evidence_closure_digest": prepared.evidence_closure_ref[
                "sha256"
            ],
            "effect_ledger_ref": deepcopy(snapshot["effect_ledger_ref"]),
            "effects": deepcopy(documents["effects"]["state_counts"]),
            "artifact_refs": [],
            "report": {
                "availability": "unavailable",
                "reason_code": "REPORT_NOT_CREATED",
            },
            "budget_summary_ref": deepcopy(snapshot["budget_summary_ref"]),
            "provenance": {
                "attempt_ids": [authority.attempt_id],
                "checkpoint_generation": 0,
                "effective_policy_digest": documents["policy"]["digest"],
                "control_plane_digest": "c" * 64,
                "evaluation_runtime_digest": "e" * 64,
            },
            "diagnostics": {
                "worker_debug_fragment": {
                    "availability": "not_applicable",
                    "reason_code": "CAPABILITY_NOT_IMPLEMENTED",
                }
            },
            "created_at": "2026-07-22T00:00:40.000Z",
            "terminal_at": "2026-07-22T00:00:45.000Z",
        }
    )
    checked = contract.validate_document("task-result/v1", result)
    return contract.canonical_validated_bytes("task-result/v1", checked)
