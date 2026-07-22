"""Typed CAS publication and settlement documents for current R0 reads."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

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
from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class CurrentSettlementError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SettlementDocuments:
    receipt: dict[str, object]
    receipt_bytes: bytes
    outcome: dict[str, object]
    outcome_bytes: bytes
    outcome_schema_id: str
    observation: dict[str, object]
    observation_bytes: bytes
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
            "artifact_id": f"artifact_{published.sha256[:32]}",
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
        outcome_document, outcome_schema_id, violation_bytes = _source_error(
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
    observation = seal_current_document(
        "observation/v1",
        {
            "schema_id": "observation/v1",
            "observation_id": observation_id,
            "task_id": getattr(call, "task_id", None),
            "attempt_id": getattr(call, "attempt_id", None),
            "native_epoch": getattr(call, "native_epoch", None),
            "tool_key": getattr(call, "tool_key", None),
            "invocation_digest": getattr(call, "invocation_digest", None),
            "local_receipt_digest": receipt["receipt_digest"],
            "status": "policy_violation" if violation else receipt["status"],
            "source_state_before_id": before.source_state_id,
            "source_state_after_id": source_after.source_state_id,
            "result_ref": outcome_ref.content_ref,
        },
    )
    observation_bytes = canonical_validated_current_bytes(
        "observation/v1", observation
    )
    return SettlementDocuments(
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        outcome=outcome_document,
        outcome_bytes=outcome_bytes,
        outcome_schema_id=outcome_schema_id,
        observation=observation,
        observation_bytes=observation_bytes,
        payload=payload,
        outcome_object=outcome_ref,
        replay_bytes=outcome_bytes,
        violation_bytes=violation_bytes,
    )


def _verify_payload(
    object_store: CurrentObjectStore,
    payload: PublishedCurrentPayload,
    *,
    raw: object,
    invocation_digest: str,
    relative_path: str,
) -> None:
    source = _read_document(object_store, payload.source, "source-content/v1")
    document = _read_document(object_store, payload.payload, "r0-read-payload/v1")
    try:
        decoded = base64.b64decode(str(source["data_base64"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise CurrentSettlementError("CURRENT_SOURCE_CONTENT_INVALID") from exc
    if (
        source.get("encoding") != "base64"
        or source.get("media_type") != "application/octet-stream"
        or source.get("byte_sha256") != hashlib.sha256(decoded).hexdigest()
        or source.get("size_bytes") != len(decoded)
        or source.get("byte_sha256") != getattr(raw, "sha256", None)
        or source.get("size_bytes") != getattr(raw, "size_bytes", None)
        or decoded != getattr(raw, "payload", None)
        or document.get("invocation_digest") != invocation_digest
        or document.get("relative_path") != relative_path
        or document.get("content_ref") != payload.source.content_ref
    ):
        raise CurrentSettlementError("CURRENT_PAYLOAD_BINDING_INVALID")


def _read_document(
    object_store: CurrentObjectStore,
    reference: PublishedCurrentReference,
    schema_id: str,
) -> dict[str, object]:
    if (
        reference.content_ref.get("content_schema_id") != schema_id
        or reference.content_ref.get("sha256") != reference.object.sha256
        or reference.content_ref.get("size_bytes") != reference.object.size_bytes
        or reference.content_ref.get("media_type") != "application/json"
        or reference.content_ref.get("encoding") != "utf-8"
    ):
        raise CurrentSettlementError("CURRENT_CONTENT_REF_INVALID")
    if canonical_validated_current_bytes(
        "content-ref/v1", reference.content_ref
    ) != reference.content_ref_bytes:
        raise CurrentSettlementError("CURRENT_CONTENT_REF_INVALID")
    encoded = object_store.read_verified(reference.object)
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentSettlementError("CURRENT_OBJECT_DOCUMENT_INVALID") from exc
    document = verify_current_document_digest(schema_id, value)
    if canonical_validated_current_bytes(schema_id, document) != encoded:
        raise CurrentSettlementError("CURRENT_OBJECT_DOCUMENT_INVALID")
    return document


def _source_error(
    call: object,
    before: SourceTreeSnapshot,
    after: SourceTreeSnapshot,
) -> tuple[dict[str, object], str, bytes]:
    invocation_digest = getattr(call, "invocation_digest", "")
    stable_error = seal_current_document(
        "stable-error/v1",
        {
            "schema_id": "stable-error/v1",
            "code": "SOURCE_STATE_CHANGED",
            "message": "Source state changed during local tool dispatch.",
            "retryable": False,
            "retry_scope": "new_claim",
            "request_id": f"req_{invocation_digest[:32]}",
            "details": {
                "task_id": getattr(call, "task_id", None),
                "attempt_id": getattr(call, "attempt_id", None),
                "invocation_digest": invocation_digest,
                "expected_digest": before.source_state_id,
                "actual_digest": after.source_state_id,
                "stable_reason": "source_state_changed",
            },
        },
    )
    stable_error_bytes = canonical_validated_current_bytes(
        "stable-error/v1", stable_error
    )
    response = validate_current_document(
        "error-response/v1",
        {"schema_id": "error-response/v1", "error": stable_error},
    )
    return response, "error-response/v1", stable_error_bytes


def _validate_receipt(receipt: dict[str, object], call: object) -> None:
    if receipt.get("receipt_kind") != "local_tool":
        raise CurrentSettlementError("LOCAL_RECEIPT_TYPE_INVALID")
    if (
        receipt.get("tool_key") != getattr(call, "tool_key", None)
        or receipt.get("invocation_digest") != getattr(call, "invocation_digest", None)
        or receipt.get("status") != "succeeded"
    ):
        raise CurrentSettlementError("LOCAL_RECEIPT_BINDING_INVALID")
    try:
        started = _timestamp(str(receipt["started_at"]))
        completed = _timestamp(str(receipt["completed_at"]))
        elapsed = receipt["elapsed_ms"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CurrentSettlementError("LOCAL_RECEIPT_TIMING_INVALID") from exc
    if (
        completed < started
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, int)
        or elapsed < 0
    ):
        raise CurrentSettlementError("LOCAL_RECEIPT_TIMING_INVALID")


def _relative_path(call: object) -> str:
    value = getattr(call, "tool_input", None)
    if isinstance(value, dict):
        result = value.get("relative_path")
    else:
        result = getattr(value, "relative_path", None)
    if not isinstance(result, str) or not result:
        raise CurrentSettlementError("R0_READ_PATH_INVALID")
    return result


def _timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError(value)
    return datetime.fromisoformat(value[:-1] + "+00:00")


__all__ = [
    "CurrentSettlementError",
    "SettlementDocuments",
    "content_ref_for",
    "prepare_settlement",
    "publish_r0_payload",
]
