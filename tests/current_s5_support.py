from __future__ import annotations

from copy import deepcopy
import hashlib

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
