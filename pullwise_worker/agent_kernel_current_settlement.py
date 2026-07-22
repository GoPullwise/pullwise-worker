"""Package-owned document preparation for current R0 journal settlement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .agent_kernel_current_objects import (
    CurrentObjectStore,
    PublishedCurrentObject,
    PublishedCurrentPayload,
)
from .agent_kernel_current_package import (
    canonical_current_document_bytes,
    canonical_validated_current_bytes,
    seal_current_document,
    validate_current_document,
    verify_current_document_digest,
)
from .agent_kernel_source_state import SourceDiff, SourceTreeSnapshot


class CurrentSettlementError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SettlementDocuments:
    receipt: dict[str, object]
    receipt_bytes: bytes
    result: dict[str, object]
    result_bytes: bytes
    observation: dict[str, object]
    observation_bytes: bytes
    payload: PublishedCurrentPayload
    result_object: PublishedCurrentPayload
    replay_bytes: bytes
    violation_bytes: bytes | None


def content_ref_for(
    published: PublishedCurrentObject,
    *,
    content_schema_id: str,
    media_type: str,
    encoding: str,
) -> PublishedCurrentPayload:
    document = validate_current_document("content-ref/v1", {
        "schema_id": "content-ref/v1",
        "artifact_id": f"artifact_{published.sha256[:32]}",
        "content_schema_id": content_schema_id,
        "sha256": published.sha256,
        "size_bytes": published.size_bytes,
        "media_type": media_type,
        "encoding": encoding,
    })
    encoded = canonical_validated_current_bytes("content-ref/v1", document)
    return PublishedCurrentPayload(published, document, encoded)


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
        or receipt_input.get("schema_id") in {
            "transport-receipt/v1", "server-transport-receipt/v1"
        }
    ):
        raise CurrentSettlementError("SERVER_TRANSPORT_RECEIPT_FORBIDDEN")
    receipt = verify_current_document_digest("local-tool-receipt/v1", receipt_input)
    _validate_receipt(receipt, call)
    receipt_bytes = canonical_validated_current_bytes("local-tool-receipt/v1", receipt)

    raw = getattr(outcome, "raw", None)
    payload = getattr(outcome, "payload", None)
    if not isinstance(payload, PublishedCurrentPayload):
        raise CurrentSettlementError("CURRENT_PAYLOAD_INVALID")
    stored = object_store.read_verified(payload.object)
    if (
        getattr(raw, "payload", None) != stored
        or getattr(raw, "sha256", None) != payload.sha256
        or getattr(raw, "size_bytes", None) != payload.size_bytes
        or receipt.get("payload_ref") != payload.content_ref
        or canonical_validated_current_bytes(
            "content-ref/v1", payload.content_ref
        ) != payload.content_ref_bytes
    ):
        raise CurrentSettlementError("CURRENT_PAYLOAD_BINDING_INVALID")

    before = getattr(prepared, "source_before", None)
    if not isinstance(before, SourceTreeSnapshot):
        raise CurrentSettlementError("SOURCE_STATE_INVALID")
    result = seal_current_document("r0-read-result/v1", {
        "schema_id": "r0-read-result/v1",
        "invocation_digest": getattr(call, "invocation_digest", None),
        "local_receipt_digest": receipt["receipt_digest"],
        "source_state_before_id": before.source_state_id,
        "source_state_after_id": source_after.source_state_id,
        "payload_ref": payload.content_ref,
    })
    result_bytes = canonical_validated_current_bytes("r0-read-result/v1", result)
    result_published = object_store.publish(result_bytes)
    result_object = content_ref_for(
        result_published,
        content_schema_id="r0-read-result/v1",
        media_type="application/json",
        encoding="utf-8",
    )
    is_violation = changes is not None and not changes.is_empty
    observation = seal_current_document("observation/v1", {
        "schema_id": "observation/v1",
        "observation_id": observation_id,
        "task_id": getattr(call, "task_id", None),
        "attempt_id": getattr(call, "attempt_id", None),
        "native_epoch": getattr(call, "native_epoch", None),
        "tool_key": getattr(call, "tool_key", None),
        "invocation_digest": getattr(call, "invocation_digest", None),
        "local_receipt_digest": receipt["receipt_digest"],
        "status": "policy_violation" if is_violation else receipt["status"],
        "source_state_before_id": before.source_state_id,
        "source_state_after_id": source_after.source_state_id,
        "result_ref": result_object.content_ref,
    })
    observation_bytes = canonical_validated_current_bytes("observation/v1", observation)
    violation = None
    replay = result_bytes
    if is_violation:
        assert changes is not None
        violation = canonical_current_document_bytes({
            "record_kind": "worker_current_source_violation",
            "code": "SOURCE_MUTATION_FORBIDDEN",
            "invocation_digest": getattr(call, "invocation_digest", None),
            "source_state_before_id": before.source_state_id,
            "source_state_after_id": source_after.source_state_id,
            "paths": _changed_paths(changes),
        })
        replay = violation
    return SettlementDocuments(
        receipt, receipt_bytes, result, result_bytes, observation,
        observation_bytes, payload, result_object, replay, violation,
    )


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


def _timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError(value)
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _changed_paths(changes: SourceDiff) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for kind in ("added", "modified", "deleted", "type_changed"):
        values.extend({"kind": kind, "path": item.path} for item in getattr(changes, kind))
    return values


__all__ = [
    "CurrentSettlementError",
    "SettlementDocuments",
    "content_ref_for",
    "prepare_settlement",
]
