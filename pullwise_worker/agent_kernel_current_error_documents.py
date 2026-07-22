"""Package-owned stable error documents emitted by the current journal."""

from __future__ import annotations

from .agent_kernel_current_package import (
    canonical_validated_current_bytes,
    seal_current_document,
    validate_current_document,
)
from .agent_kernel_source_state import SourceTreeSnapshot


def source_changed_error(
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


__all__ = ["source_changed_error"]
