"""Fail-closed validation and verified reads for current settlement documents."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json

from .agent_kernel_current_objects import (
    CurrentObjectStore,
    PublishedCurrentPayload,
    PublishedCurrentReference,
)
from .agent_kernel_current_package import (
    canonical_validated_current_bytes,
    verify_current_document_digest,
)


class CurrentSettlementError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
