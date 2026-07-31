"""Value objects and exact-CAS helpers for terminalization."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Mapping

from . import _generated_agent_task_contract as contract


class CurrentTerminalizationError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PreparedTerminalization:
    task_id: str
    outcome: str
    reason_code: str
    selector_input_digest: str
    terminalization_input_bytes: bytes
    root_set_bytes: bytes
    pre_gate_closure_bytes: bytes
    terminalization_fact_bytes: tuple[bytes, ...]
    selector_axes: tuple[tuple[str, str], ...]
    gate_decision_bytes: bytes
    evidence_closure_bytes: bytes
    gate_decision_ref: dict[str, object]
    evidence_closure_ref: dict[str, object]


@dataclass(frozen=True)
class FrozenTerminalization:
    result_id: str
    task_id: str
    outcome: str
    reason_code: str
    published_from_version: int
    terminal_task_version: int
    selector_input_digest: str
    result_digest: str
    task_result_core_sha256: str
    task_version_authority_sha256: str
    frozen_at: str


def error_detail(error: BaseException) -> str:
    return str(getattr(error, "code", type(error).__name__))


def parse_exact(
    schema_id: str,
    raw: bytes,
    *,
    code: str = "TERMINALIZATION_DOCUMENT_INVALID",
) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise CurrentTerminalizationError(code)
    try:
        detached = json.loads(raw.decode("utf-8"))
        if contract.canonical_document_bytes(detached) != raw:
            raise CurrentTerminalizationError(code, "NONCANONICAL")
        schema = contract.schema(schema_id)
        checked = (
            contract.verify_document_digest(schema_id, detached)
            if isinstance(schema.get("x-pullwise-digest"), dict)
            else contract.validate_document(schema_id, detached)
        )
        if contract.canonical_validated_bytes(schema_id, checked) != raw:
            raise CurrentTerminalizationError(code, "NONCANONICAL")
        return checked
    except CurrentTerminalizationError:
        raise
    except Exception as exc:
        raise CurrentTerminalizationError(code, error_detail(exc)) from exc


def canonical_bytes(schema_id: str, document: dict[str, object]) -> bytes:
    return contract.canonical_validated_bytes(schema_id, document)


def object_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def content_ref(
    schema_id: str,
    document: dict[str, object],
    *,
    artifact_id: str | None = None,
) -> dict[str, object]:
    raw = canonical_bytes(schema_id, document)
    digest = object_sha256(raw)
    return {
        "schema_id": "content-ref/v1",
        "artifact_id": artifact_id or "art_" + digest[:32],
        "content_schema_id": schema_id,
        "sha256": digest,
        "size_bytes": len(raw),
        "media_type": "application/json",
        "encoding": "utf-8",
    }


def ref_matches(
    ref: dict[str, object], schema_id: str, raw: bytes
) -> bool:
    return bool(
        ref.get("schema_id") == "content-ref/v1"
        and ref.get("content_schema_id") == schema_id
        and ref.get("sha256") == object_sha256(raw)
        and ref.get("size_bytes") == len(raw)
        and ref.get("media_type") == "application/json"
        and ref.get("encoding") == "utf-8"
    )


def normalize_objects(
    objects: Mapping[str, tuple[str, bytes]],
) -> dict[str, tuple[str, bytes]]:
    if not isinstance(objects, Mapping):
        raise CurrentTerminalizationError("EVIDENCE_OBJECT_SET_INVALID")
    normalized: dict[str, tuple[str, bytes]] = {}
    for presented, item in objects.items():
        if not isinstance(item, tuple) or len(item) != 2:
            raise CurrentTerminalizationError("EVIDENCE_OBJECT_SET_INVALID")
        schema_id, raw = item
        document = parse_exact(schema_id, raw, code="EVIDENCE_OBJECT_INVALID")
        canonical = canonical_bytes(schema_id, document)
        digest = object_sha256(canonical)
        if presented != digest or canonical != raw:
            raise CurrentTerminalizationError("EVIDENCE_OBJECT_INVALID")
        normalized[digest] = (schema_id, raw)
    return normalized


def put_object(
    connection: sqlite3.Connection,
    schema_id: str,
    raw: bytes,
) -> str:
    digest = object_sha256(raw)
    connection.execute(
        "INSERT OR IGNORE INTO checkpoint_objects "
        "(sha256,content_schema_id,size_bytes,object_bytes) VALUES (?,?,?,?)",
        (digest, schema_id, len(raw), raw),
    )
    row = connection.execute(
        "SELECT content_schema_id,size_bytes,object_bytes "
        "FROM checkpoint_objects WHERE sha256=?",
        (digest,),
    ).fetchone()
    if row is None or (
        row["content_schema_id"],
        row["size_bytes"],
        bytes(row["object_bytes"]),
    ) != (schema_id, len(raw), raw):
        raise CurrentTerminalizationError("SEMANTIC_OBJECT_COLLISION")
    return digest


def load_ref(
    connection: sqlite3.Connection,
    ref: dict[str, object],
    memory: Mapping[str, tuple[str, bytes]],
) -> tuple[dict[str, object], bytes]:
    digest = ref.get("sha256")
    item = memory.get(digest) if isinstance(digest, str) else None
    if item is None and isinstance(digest, str):
        row = connection.execute(
            "SELECT content_schema_id,object_bytes FROM checkpoint_objects "
            "WHERE sha256=?",
            (digest,),
        ).fetchone()
        if row is not None:
            item = (row["content_schema_id"], bytes(row["object_bytes"]))
    if item is None:
        raise CurrentTerminalizationError("EVIDENCE_OBJECT_MISSING")
    schema_id, raw = item
    if not ref_matches(ref, schema_id, raw):
        raise CurrentTerminalizationError("EVIDENCE_OBJECT_MISMATCH")
    return parse_exact(schema_id, raw, code="EVIDENCE_OBJECT_INVALID"), raw


def collect_content_refs(value: object) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("schema_id") == "content-ref/v1":
            refs.append(value)
        else:
            for child in value.values():
                refs.extend(collect_content_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(collect_content_refs(child))
    return refs


def frozen_from_row(row: sqlite3.Row) -> FrozenTerminalization:
    return FrozenTerminalization(
        result_id=row["result_id"],
        task_id=row["task_id"],
        outcome=row["outcome"],
        reason_code=row["reason_code"],
        published_from_version=row["published_from_version"],
        terminal_task_version=row["terminal_task_version"],
        selector_input_digest=row["selector_input_digest"],
        result_digest=row["result_digest"],
        task_result_core_sha256=row["task_result_core_sha256"],
        task_version_authority_sha256=row[
            "task_version_authority_sha256"
        ],
        frozen_at=row["frozen_at"],
    )


__all__ = [
    "CurrentTerminalizationError",
    "FrozenTerminalization",
    "PreparedTerminalization",
    "canonical_bytes",
    "collect_content_refs",
    "content_ref",
    "error_detail",
    "frozen_from_row",
    "load_ref",
    "normalize_objects",
    "object_sha256",
    "parse_exact",
    "put_object",
    "ref_matches",
]
