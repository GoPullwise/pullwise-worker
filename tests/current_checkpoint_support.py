from __future__ import annotations

from copy import deepcopy
import hashlib

from pullwise_worker import _generated_agent_task_contract as contract
from tests.current_runtime_bootstrap_support import golden_runtime_bootstrap


def _content_ref(
    schema_id: str,
    raw: bytes,
    artifact_number: int,
) -> dict[str, object]:
    return {
        "schema_id": "content-ref/v1",
        "artifact_id": f"art_{artifact_number:032x}",
        "content_schema_id": schema_id,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "media_type": "application/json",
        "encoding": "utf-8",
    }


def _fixture_bytes(fixture_id: str, schema_id: str) -> bytes:
    return contract.canonical_validated_bytes(
        schema_id,
        deepcopy(contract.fixture(fixture_id)["document"]),
    )


def checkpoint_documents(
    generation: int,
    *,
    previous_manifest: dict[str, object] | None = None,
    summary_suffix: str = "",
) -> tuple[bytes, bytes, bytes, dict[str, tuple[str, bytes]]]:
    bootstrap = golden_runtime_bootstrap()
    authority = bootstrap["authority"]
    accepted = bootstrap["accept_request"]
    from_version = authority["task_version"] + generation - 1
    created_at = f"2026-07-22T00:00:{generation + 1:02d}.000Z"

    workspace_bytes = _fixture_bytes(
        "source_evidence_golden_source_tree", "source-tree-manifest/v1"
    )
    execution_bytes = _fixture_bytes(
        "source_evidence_golden_execution_state", "execution-state-manifest/v1"
    )
    request_bytes = contract.canonical_validated_bytes(
        "task-request/v1", accepted["task_request"]
    )
    ledger_bytes = contract.canonical_validated_bytes(
        "requirement-ledger/v1", accepted["requirement_ledger"]
    )
    attachments = {
        hashlib.sha256(raw).hexdigest(): (schema_id, raw)
        for schema_id, raw in (
            ("source-tree-manifest/v1", workspace_bytes),
            ("execution-state-manifest/v1", execution_bytes),
            ("task-request/v1", request_bytes),
            ("requirement-ledger/v1", ledger_bytes),
        )
    }

    machine = deepcopy(contract.fixture("checkpoint_state_golden_machine")["document"])
    machine.update(
        {
            "package": contract.package_tuple(),
            "task_id": authority["task_id"],
            "generation": generation,
            "task_version": from_version,
            "attempt_id": authority["attempt_id"],
            "native_epoch": authority["native_epoch"],
            "owner_id": authority["owner_id"],
            "owner_epoch": authority["owner_epoch"],
            "session_id": authority["session_id"],
            "transport_binding": bootstrap["transport_binding"],
            "workspace_state_ref": _content_ref(
                "source-tree-manifest/v1", workspace_bytes, 1
            ),
            "execution_state_ref": _content_ref(
                "execution-state-manifest/v1", execution_bytes, 2
            ),
            "event_seq": generation - 1,
            "created_at": created_at,
        }
    )
    machine.pop("machine_checkpoint_digest")
    machine = contract.seal_document("machine-checkpoint/v1", machine)
    machine_bytes = contract.canonical_validated_bytes(
        "machine-checkpoint/v1", machine
    )

    semantic = deepcopy(
        contract.fixture("checkpoint_state_golden_semantic")["document"]
    )
    semantic.update(
        {
            "package": contract.package_tuple(),
            "task_id": authority["task_id"],
            "generation": generation,
            "task_version": from_version,
            "owner_id": authority["owner_id"],
            "owner_epoch": authority["owner_epoch"],
            "task_request_ref": _content_ref("task-request/v1", request_bytes, 3),
            "requirement_ledger_ref": _content_ref(
                "requirement-ledger/v1", ledger_bytes, 4
            ),
            "created_at": created_at,
        }
    )
    semantic["owner_summary"]["objective_restated"] += summary_suffix
    semantic.pop("semantic_checkpoint_digest")
    semantic = contract.seal_document("semantic-checkpoint/v1", semantic)
    semantic_bytes = contract.canonical_validated_bytes(
        "semantic-checkpoint/v1", semantic
    )

    manifest = deepcopy(
        contract.fixture("checkpoint_manifest_golden_genesis_commit")["document"]
    )
    manifest.update(
        {
            "package": contract.package_tuple(),
            "task_id": authority["task_id"],
            "generation": generation,
            "previous_generation": generation - 1,
            "previous_manifest_hash": (
                None if previous_manifest is None
                else previous_manifest["manifest_hash"]
            ),
            "committed_from_task_version": from_version,
            "committed_task_version": from_version + 1,
            "native_epoch": authority["native_epoch"],
            "attempt_id": authority["attempt_id"],
            "owner_epoch": authority["owner_epoch"],
            "machine_state_ref": _content_ref(
                "machine-checkpoint/v1", machine_bytes, 5 + generation * 2
            ),
            "semantic_state_ref": _content_ref(
                "semantic-checkpoint/v1", semantic_bytes, 6 + generation * 2
            ),
            "event_seq": generation - 1,
            "created_at": created_at,
        }
    )
    manifest.pop("manifest_hash")
    manifest = contract.seal_document("committed-checkpoint-manifest/v1", manifest)
    manifest_bytes = contract.canonical_validated_bytes(
        "committed-checkpoint-manifest/v1", manifest
    )
    return manifest_bytes, machine_bytes, semantic_bytes, attachments


def manifest_document(raw: bytes) -> dict[str, object]:
    import json

    return contract.verify_document_digest(
        "committed-checkpoint-manifest/v1", json.loads(raw)
    )


def checkpoint_ack_bytes(
    item: object,
    authority: object,
    *,
    accepted_at: str | None = None,
) -> bytes:
    request_unsigned = {
        "schema_id": "checkpoint-watermark-request/internal-v1",
        "package": contract.package_tuple(),
        "task_id": item.task_id,
        "generation": item.generation,
        "previous_manifest_hash": item.previous_manifest_hash,
        "manifest_hash": item.manifest_hash,
        "committed_from_task_version": item.committed_task_version - 1,
        "committed_task_version": item.committed_task_version,
        "attempt_id": item.attempt_id,
        "owner_id": authority.owner_id,
        "owner_epoch": item.owner_epoch,
        "lease_id": authority.lease_id,
        "authority_digest": item.authority_digest,
        "grant_id": authority.grant.grant_id,
        "grant_digest": authority.grant_digest,
        "deletion_version": item.deletion_version,
        "native_epoch": item.native_epoch,
        "transport_epoch": item.transport_epoch,
        "same_run_resume_selected": True,
    }
    request_digest = hashlib.sha256(
        b"pullwise:checkpoint-watermark-request:internal-v1\0"
        + contract.canonical_document_bytes(request_unsigned)
    ).hexdigest()
    ack_unsigned = {
        **request_unsigned,
        "schema_id": "checkpoint-watermark-ack/internal-v1",
        "request_digest": request_digest,
        "accepted_at": accepted_at
        or f"2026-07-22T00:01:{item.generation:02d}.000Z",
    }
    ack_digest = hashlib.sha256(
        b"pullwise:checkpoint-watermark-ack:internal-v1\0"
        + contract.canonical_document_bytes(ack_unsigned)
    ).hexdigest()
    return contract.canonical_document_bytes(
        {**ack_unsigned, "ack_digest": ack_digest}
    )
