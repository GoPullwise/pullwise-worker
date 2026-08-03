"""Value objects and exact-document helpers for current WorkerDebugFragment."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_objects import PublishedCurrentObject


class CurrentWorkerDebugError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class DebugCaptureLimits:
    max_file_bytes: int = 256 * 1024
    max_total_bytes: int = 2 * 1024 * 1024
    max_archive_bytes: int = 2 * 1024 * 1024
    max_entries: int = 15

    def __post_init__(self) -> None:
        values = (
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_archive_bytes,
            self.max_entries,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in values
        ) or self.max_entries > 15:
            raise CurrentWorkerDebugError("DEBUG_LIMIT_EXCEEDED")


@dataclass(frozen=True)
class StagedTaskResultCore:
    document: dict[str, object]
    canonical_bytes: bytes
    sha256: str
    content_ref: dict[str, object]


@dataclass(frozen=True)
class CapturedWorkerDebugFragment:
    document: dict[str, object]
    canonical_bytes: bytes
    sha256: str
    content_ref: dict[str, object]
    file_manifest: dict[str, object]
    file_manifest_bytes: bytes
    redaction_report: dict[str, object]
    redaction_report_bytes: bytes
    archive_object: PublishedCurrentObject


@dataclass(frozen=True)
class SealedWorkerDebugDescriptor:
    document: dict[str, object]
    canonical_bytes: bytes
    sha256: str
    content_ref: dict[str, object]


def parse_exact(schema_id: str, raw: bytes, code: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise CurrentWorkerDebugError(code)
    try:
        value = json.loads(raw.decode("utf-8"))
        if contract.canonical_document_bytes(value) != raw:
            raise CurrentWorkerDebugError(code, "NONCANONICAL")
        schema = contract.schema(schema_id)
        checked = (
            contract.verify_document_digest(schema_id, value)
            if isinstance(schema.get("x-pullwise-digest"), dict)
            else contract.validate_document(schema_id, value)
        )
        if contract.canonical_validated_bytes(schema_id, checked) != raw:
            raise CurrentWorkerDebugError(code, "NONCANONICAL")
        return checked
    except CurrentWorkerDebugError:
        raise
    except Exception as exc:
        detail = str(getattr(exc, "code", type(exc).__name__))
        raise CurrentWorkerDebugError(code, detail) from exc


def canonical_bytes(schema_id: str, document: dict[str, object]) -> bytes:
    return contract.canonical_validated_bytes(schema_id, document)


def object_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def content_ref(
    schema_id: str,
    raw: bytes,
    *,
    media_type: str = "application/json",
    encoding: str = "utf-8",
) -> dict[str, object]:
    digest = object_sha256(raw)
    return contract.validate_document(
        "content-ref/v1",
        {
            "schema_id": "content-ref/v1",
            "artifact_id": "art_" + digest[:32],
            "content_schema_id": schema_id,
            "sha256": digest,
            "size_bytes": len(raw),
            "media_type": media_type,
            "encoding": encoding,
        },
    )


def require_input_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
    return root


__all__ = [
    "CapturedWorkerDebugFragment",
    "CurrentWorkerDebugError",
    "DebugCaptureLimits",
    "SealedWorkerDebugDescriptor",
    "StagedTaskResultCore",
    "canonical_bytes",
    "content_ref",
    "object_sha256",
    "parse_exact",
    "require_input_root",
]
