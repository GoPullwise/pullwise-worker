"""Typed CAS publication and settlement documents for current R0 reads."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re

from .agent_kernel_current_error_documents import source_changed_error
from .agent_kernel_current_objects import (
    CurrentObjectStore,
    PublishedCurrentObject,
    PublishedCurrentPayload,
    PublishedCurrentReference,
)
from .agent_kernel_current_package import (
    canonical_validated_current_bytes,
    seal_current_document,
    validate_current_document,
    verify_current_document_digest,
)
from .agent_kernel_current_settlement_validation import (
    CurrentSettlementError as CurrentSettlementError,
    _read_document as _read_document,
    _relative_path as _relative_path,
    _timestamp as _timestamp,
    _validate_receipt as _validate_receipt,
    _verify_payload as _verify_payload,
)
from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SettlementDocuments:
    receipt: dict[str, object]
    receipt_bytes: bytes
    outcome: dict[str, object]
    outcome_bytes: bytes
    outcome_schema_id: str
    observation_seed: dict[str, object]
    payload: PublishedCurrentPayload
    outcome_object: PublishedCurrentReference
    replay_bytes: bytes
    violation_bytes: bytes | None


def content_ref_for(
    published: PublishedCurrentObject,
    *,
    content_schema_id: str,
    media_type: str,
    encoding: str,
) -> PublishedCurrentReference:
    document = validate_current_document(
        "content-ref/v1",
        {
            "schema_id": "content-ref/v1",
            "artifact_id": f"art_{published.sha256[:32]}",
            "content_schema_id": content_schema_id,
            "sha256": published.sha256,
            "size_bytes": published.size_bytes,
            "media_type": media_type,
            "encoding": encoding,
        },
    )
    encoded = canonical_validated_current_bytes("content-ref/v1", document)
    return PublishedCurrentReference(published, document, encoded)


def publish_r0_payload(
    object_store: CurrentObjectStore,
    *,
    invocation_digest: str,
    relative_path: str,
    raw: object,
) -> PublishedCurrentPayload:
    payload = getattr(raw, "payload", None)
    digest = getattr(raw, "sha256", None)
    size = getattr(raw, "size_bytes", None)
    if (
        not isinstance(payload, bytes)
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size != len(payload)
        or digest != hashlib.sha256(payload).hexdigest()
    ):
        raise CurrentSettlementError("R0_READ_RECEIPT_INVALID")

    source_document = seal_current_document(
        "source-content/v1",
        {
            "schema_id": "source-content/v1",
            "media_type": "application/octet-stream",
            "encoding": "base64",
            "data_base64": base64.b64encode(payload).decode("ascii"),
            "byte_sha256": digest,
            "size_bytes": size,
        },
    )
    source_bytes = canonical_validated_current_bytes(
        "source-content/v1", source_document
    )
    source_object = object_store.publish(source_bytes)
    source_ref = content_ref_for(
        source_object,
        content_schema_id="source-content/v1",
        media_type="application/json",
        encoding="utf-8",
    )

    payload_document = seal_current_document(
        "r0-read-payload/v1",
        {
            "schema_id": "r0-read-payload/v1",
            "invocation_digest": invocation_digest,
            "relative_path": relative_path,
            "content_ref": source_ref.content_ref,
        },
    )
    payload_bytes = canonical_validated_current_bytes(
        "r0-read-payload/v1", payload_document
    )
    payload_object = object_store.publish(payload_bytes)
    payload_ref = content_ref_for(
        payload_object,
        content_schema_id="r0-read-payload/v1",
        media_type="application/json",
        encoding="utf-8",
    )
    published = PublishedCurrentPayload(payload=payload_ref, source=source_ref)
    _verify_payload(
        object_store,
        published,
        raw=raw,
        invocation_digest=invocation_digest,
        relative_path=relative_path,
    )
    return published


def prepare_settlement(
    *,
    object_store: CurrentObjectStore,
    call: object,
    prepared: object,
    outcome: object,
    source_after: SourceTreeSnapshot,
    observation_id: str,
    tool_invocation_id: str,
    changes: SourceDiff | None = None,
) -> SettlementDocuments:
    if not isinstance(source_after, SourceTreeSnapshot):
        raise CurrentSettlementError("SOURCE_STATE_INVALID")
    receipt_input = getattr(outcome, "receipt", None)
    if not isinstance(receipt_input, dict):
        raise CurrentSettlementError("LOCAL_RECEIPT_INVALID")
    if (
        receipt_input.get("receipt_kind") == "server_transport"
        or receipt_input.get("schema_id") == "server-transport-receipt/v1"
    ):
        raise CurrentSettlementError("SERVER_TRANSPORT_RECEIPT_FORBIDDEN")
    receipt = verify_current_document_digest("local-tool-receipt/v1", receipt_input)
    _validate_receipt(receipt, call)
    receipt_bytes = canonical_validated_current_bytes("local-tool-receipt/v1", receipt)

    payload = getattr(outcome, "payload", None)
    if not isinstance(payload, PublishedCurrentPayload):
        raise CurrentSettlementError("CURRENT_PAYLOAD_INVALID")
    relative_path = _relative_path(call)
    _verify_payload(
        object_store,
        payload,
        raw=getattr(outcome, "raw", None),
        invocation_digest=getattr(call, "invocation_digest", ""),
        relative_path=relative_path,
    )
    if receipt.get("payload_ref") != payload.content_ref:
        raise CurrentSettlementError("CURRENT_PAYLOAD_BINDING_INVALID")

    before = getattr(prepared, "source_before", None)
    if not isinstance(before, SourceTreeSnapshot):
        raise CurrentSettlementError("SOURCE_STATE_INVALID")
    violation = changes is not None
    if violation:
        if (
            changes.is_empty
            or changes.original_source_state_id != before.source_state_id
            or changes.final_source_state_id != source_after.source_state_id
        ):
            raise CurrentSettlementError("SOURCE_DIFF_INVALID")
        outcome_document, outcome_schema_id, violation_bytes = source_changed_error(
            call, before, source_after
        )
    else:
        if before.source_state_id != source_after.source_state_id:
            raise CurrentSettlementError("SOURCE_STATE_CHANGED")
        outcome_document = seal_current_document(
            "r0-read-result/v1",
            {
                "schema_id": "r0-read-result/v1",
                "invocation_digest": getattr(call, "invocation_digest", None),
                "local_receipt_digest": receipt["receipt_digest"],
                "source_state_before_id": before.source_state_id,
                "source_state_after_id": source_after.source_state_id,
                "payload_ref": payload.content_ref,
            },
        )
        outcome_schema_id = "r0-read-result/v1"
        violation_bytes = None

    outcome_bytes = canonical_validated_current_bytes(
        outcome_schema_id, outcome_document
    )
    published_outcome = object_store.publish(outcome_bytes)
    outcome_ref = content_ref_for(
        published_outcome,
        content_schema_id=outcome_schema_id,
        media_type="application/json",
        encoding="utf-8",
    )
    observation_seed = {
        "schema_id": "observation/v1",
        "observation_id": observation_id,
        "task_id": getattr(call, "task_id", None),
        "attempt_id": getattr(call, "attempt_id", None),
        "native_epoch": getattr(call, "native_epoch", None),
        "actor": {
            "schema_id": "actor/v1",
            "kind": "task_owner",
            "id": getattr(call, "owner_id", None),
            "session_id": getattr(call, "session_id", None),
        },
        "tool_id": getattr(call, "tool_key", None),
        "tool_version": getattr(prepared, "tool_version", None),
        "tool_invocation_id": tool_invocation_id,
        "idempotency_key": getattr(call, "idempotency_key", None),
        "input_digest": getattr(call, "invocation_digest", None),
        "status": "policy_violation" if violation else receipt["status"],
        "started_at": receipt["started_at"],
        "completed_at": receipt["completed_at"],
        "duration_ms": receipt["elapsed_ms"],
        "exit_code": None,
        "source_state_before_id": before.source_state_id,
        "source_state_after_id": source_after.source_state_id,
        "execution_state_id": None,
        "stdout_ref": {
            "availability": "not_applicable",
            "reason_code": "IN_PROCESS_TOOL",
        },
        "stderr_ref": {
            "availability": "not_applicable",
            "reason_code": "IN_PROCESS_TOOL",
        },
        "result_ref": {
            "availability": "available",
            "ref": outcome_ref.content_ref,
        },
        "redaction_report_ref": {
            "availability": "not_applicable",
            "reason_code": "REDACTION_NOT_REQUIRED",
        },
        "partial_side_effect": False,
    }
    return SettlementDocuments(
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        outcome=outcome_document,
        outcome_bytes=outcome_bytes,
        outcome_schema_id=outcome_schema_id,
        observation_seed=observation_seed,
        payload=payload,
        outcome_object=outcome_ref,
        replay_bytes=outcome_bytes,
        violation_bytes=violation_bytes,
    )


def finalize_observation(
    documents: SettlementDocuments, observation_seq: int
) -> tuple[dict[str, object], bytes]:
    if (
        isinstance(observation_seq, bool)
        or not isinstance(observation_seq, int)
        or observation_seq < 1
    ):
        raise CurrentSettlementError("OBSERVATION_SEQUENCE_INVALID")
    observation = seal_current_document(
        "observation/v1",
        {**documents.observation_seed, "observation_seq": observation_seq},
    )
    encoded = canonical_validated_current_bytes("observation/v1", observation)
    return observation, encoded


__all__ = [
    "CurrentSettlementError",
    "SettlementDocuments",
    "content_ref_for",
    "finalize_observation",
    "prepare_settlement",
    "publish_r0_payload",
]
