from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from pullwise_worker import _generated_agent_task_contract as contract
from tests.current_s5_support import fixture_document

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
