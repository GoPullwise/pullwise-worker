"""Public value objects for current TaskResult transport storage."""

from __future__ import annotations

from dataclasses import dataclass


class CurrentTaskResultTransportError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PreparedTaskResultTransport:
    task_id: str
    result_digest: str
    task_result_core_sha256: str
    task_version_authority_sha256: str
    worker_debug_descriptor_sha256: str | None
    transport_receipt_sha256: str | None
    transport_envelope_digest: str
    canonical_bytes: bytes
    document: dict[str, object]
    task_result_core: dict[str, object]


@dataclass(frozen=True)
class AcceptedTaskResultTransport:
    task_id: str
    result_digest: str
    transport_envelope_digest: str
    ack_sha256: str
    ack_digest: str
    accepted_at: str
    canonical_bytes: bytes
    document: dict[str, object]


__all__ = [
    "AcceptedTaskResultTransport",
    "CurrentTaskResultTransportError",
    "PreparedTaskResultTransport",
]
