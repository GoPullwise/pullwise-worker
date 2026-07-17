"""Deterministic legacy claim identities for the Agent Kernel shadow path."""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Callable


LEGACY_TASK_DOMAIN = b"pullwise-scan-id/v1\0"
MAX_SAFE_INTEGER = 2**53 - 1


class AgentKernelIdentityError(ValueError):
    pass


def _sha256(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def legacy_v1_task_mapping(
    scan_id: str,
    transport_epoch: int,
    *,
    digest: Callable[[bytes], bytes] = _sha256,
) -> dict[str, object]:
    if not isinstance(scan_id, str) or not scan_id:
        raise AgentKernelIdentityError("scan_id_invalid")
    try:
        scan_bytes = scan_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AgentKernelIdentityError("scan_id_invalid") from exc
    if unicodedata.normalize("NFC", scan_id) != scan_id:
        raise AgentKernelIdentityError("scan_id_not_nfc")
    if isinstance(transport_epoch, bool) or not isinstance(transport_epoch, int):
        raise AgentKernelIdentityError("transport_epoch_invalid")
    if not 1 <= transport_epoch <= MAX_SAFE_INTEGER:
        raise AgentKernelIdentityError("transport_epoch_invalid")
    task_hash = digest(LEGACY_TASK_DOMAIN + scan_bytes)
    if not isinstance(task_hash, bytes) or len(task_hash) != 32:
        raise AgentKernelIdentityError("task_digest_invalid")
    return {
        "schema_id": "legacy-v1-task-mapping/v1",
        "scan_id": scan_id,
        "task_id": "task_" + task_hash.hex()[:32],
        "transport_epoch": transport_epoch,
    }


def assert_same_legacy_identity(
    *,
    existing_task_id: str,
    existing_scan_id: str,
    incoming: dict[str, object],
) -> None:
    incoming_task_id = incoming.get("task_id")
    incoming_scan_id = incoming.get("scan_id")
    if incoming_task_id != existing_task_id:
        raise AgentKernelIdentityError("TASK_IDENTITY_MISMATCH")
    if not isinstance(incoming_scan_id, str) or (
        existing_scan_id.encode("utf-8") != incoming_scan_id.encode("utf-8")
    ):
        raise AgentKernelIdentityError("TASK_ID_COLLISION")
