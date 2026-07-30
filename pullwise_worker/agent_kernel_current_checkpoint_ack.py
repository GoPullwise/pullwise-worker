"""Exact verifier for Server-owned checkpoint watermark acknowledgements."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Mapping

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_checkpoint_contract import CurrentCheckpointError


REQUEST_SCHEMA_ID = "checkpoint-watermark-request/internal-v1"
ACK_SCHEMA_ID = "checkpoint-watermark-ack/internal-v1"
_REQUEST_DOMAIN = b"pullwise:checkpoint-watermark-request:internal-v1\0"
_ACK_DOMAIN = b"pullwise:checkpoint-watermark-ack:internal-v1\0"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_KEYS = frozenset(
    {
        "schema_id",
        "package",
        "task_id",
        "generation",
        "previous_manifest_hash",
        "manifest_hash",
        "committed_from_task_version",
        "committed_task_version",
        "attempt_id",
        "owner_id",
        "owner_epoch",
        "lease_id",
        "authority_digest",
        "grant_id",
        "grant_digest",
        "deletion_version",
        "native_epoch",
        "transport_epoch",
        "same_run_resume_selected",
        "request_digest",
        "accepted_at",
        "ack_digest",
    }
)
_ID_PREFIXES = {
    "task_id": "task_",
    "attempt_id": "attempt_",
    "owner_id": "owner_",
    "lease_id": "lease_",
    "grant_id": "grant_",
}


@dataclass(frozen=True)
class ServerCheckpointAck:
    task_id: str
    generation: int
    previous_manifest_hash: str | None
    manifest_hash: str
    committed_from_task_version: int
    committed_task_version: int
    attempt_id: str
    owner_id: str
    owner_epoch: int
    lease_id: str
    authority_digest: str
    grant_id: str
    grant_digest: str
    deletion_version: int
    native_epoch: int
    transport_epoch: int
    request_digest: str
    accepted_at: str
    ack_digest: str
    canonical_bytes: bytes

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> "ServerCheckpointAck":
        document = _verify(raw)
        return cls(
            **{
                field: document[field]
                for field in cls.__dataclass_fields__
                if field != "canonical_bytes"
            },
            canonical_bytes=raw,
        )


def _digest(domain: bytes, document: Mapping[str, object]) -> str:
    return hashlib.sha256(domain + contract.canonical_document_bytes(document)).hexdigest()


def _fail() -> None:
    raise CurrentCheckpointError("CHECKPOINT_ACK_INVALID")


def _is_prefixed_id(value: object, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) == len(prefix) + 32
        and all(character in "0123456789abcdef" for character in value[len(prefix):])
    )


def _verify(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes):
        _fail()
    try:
        document = json.loads(raw.decode("utf-8"))
        if contract.canonical_document_bytes(document) != raw:
            _fail()
    except CurrentCheckpointError:
        raise
    except Exception:
        _fail()
    if (
        type(document) is not dict
        or set(document) != _KEYS
        or document.get("schema_id") != ACK_SCHEMA_ID
        or document.get("package") != contract.package_tuple()
        or document.get("same_run_resume_selected") is not True
    ):
        _fail()
    for field, prefix in _ID_PREFIXES.items():
        if not _is_prefixed_id(document.get(field), prefix):
            _fail()
    for field in (
        "manifest_hash",
        "authority_digest",
        "grant_digest",
        "request_digest",
        "ack_digest",
    ):
        value = document.get(field)
        if not isinstance(value, str) or _HASH.fullmatch(value) is None:
            _fail()
    previous = document.get("previous_manifest_hash")
    if previous is not None and (
        not isinstance(previous, str) or _HASH.fullmatch(previous) is None
    ):
        _fail()
    for field, minimum in (
        ("generation", 1),
        ("committed_from_task_version", 1),
        ("committed_task_version", 2),
        ("owner_epoch", 1),
        ("deletion_version", 0),
        ("native_epoch", 1),
        ("transport_epoch", 1),
    ):
        value = document.get(field)
        if type(value) is not int or value < minimum:
            _fail()
    if (
        document["committed_task_version"]
        != document["committed_from_task_version"] + 1
        or not isinstance(document.get("accepted_at"), str)
        or _TIMESTAMP.fullmatch(document["accepted_at"]) is None
    ):
        _fail()
    ack_unsigned = {
        key: value for key, value in document.items() if key != "ack_digest"
    }
    if not hmac.compare_digest(
        document["ack_digest"], _digest(_ACK_DOMAIN, ack_unsigned)
    ):
        _fail()
    request_unsigned = {
        **{
            key: document[key]
            for key in _KEYS
            if key
            not in {"schema_id", "request_digest", "accepted_at", "ack_digest"}
        },
        "schema_id": REQUEST_SCHEMA_ID,
    }
    if not hmac.compare_digest(
        document["request_digest"],
        _digest(_REQUEST_DOMAIN, request_unsigned),
    ):
        _fail()
    return document


__all__ = ["ServerCheckpointAck"]
