"""Safe allowlist capture, structured redaction, and deterministic ZIP."""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from . import _generated_agent_task_contract as contract
from .agent_kernel_current_debug_contract import (
    CurrentWorkerDebugError,
    DebugCaptureLimits,
    canonical_bytes,
    object_sha256,
    require_input_root,
)


_MEDIA_TYPES = {
    "agent-events.jsonl": "application/x-ndjson",
    "agent-task-summary.json": "application/json",
    "artifact-manifest.json": "application/json",
    "checkpoint-index.json": "application/json",
    "codex-events.jsonl": "application/x-ndjson",
    "codex-runtime.json": "application/json",
    "debug-summary.json": "application/json",
    "error-report.json": "application/json",
    "evidence-index.json": "application/json",
    "gateway-events.jsonl": "application/x-ndjson",
    "progress.log.jsonl": "application/x-ndjson",
    "qa.json": "application/json",
    "redaction-report.json": "application/json",
    "task-events.jsonl": "application/x-ndjson",
    "worker.log.jsonl": "application/x-ndjson",
}
_GENERATED = {"redaction-report.json", "fragment-files.json"}
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|credential|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_TOKEN = re.compile(
    r"(?i)(?:bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
)


@dataclass(frozen=True)
class PreparedDebugContent:
    payloads: dict[str, bytes]
    file_manifest: dict[str, object]
    file_manifest_bytes: bytes
    redaction_report: dict[str, object]
    redaction_report_bytes: bytes
    archive_bytes: bytes
    status: str
    reason_code: str | None


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_regular(path: Path, limit: int) -> bytes | None:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE", type(exc).__name__) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or _is_reparse(before)
    ):
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE", path.name)
    if before.st_size > limit:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE", path.name) from exc
    try:
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE", path.name)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if _identity(before) != _identity(after) or _identity(before) != _identity(
            path_after
        ):
            raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE", path.name)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _redact(value: object) -> tuple[object, int]:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        count = 0
        for key, child in value.items():
            if not isinstance(key, str):
                raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED")
            if _SECRET_KEY.search(key) and child != "[REDACTED]":
                result[key] = "[REDACTED]"
                count += 1
            else:
                result[key], detected = _redact(child)
                count += detected
        return result, count
    if isinstance(value, list):
        result = []
        count = 0
        for child in value:
            cleaned, detected = _redact(child)
            result.append(cleaned)
            count += detected
        return result, count
    if isinstance(value, str):
        cleaned, count = _TOKEN.subn("[REDACTED]", value)
        return cleaned, count
    if value is None or isinstance(value, (bool, int)):
        return value, 0
    raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED")


def _decode_json(path: str, raw: bytes) -> tuple[object, int]:
    try:
        text = raw.decode("utf-8")
        if path.endswith(".jsonl"):
            values = []
            detected = 0
            for line in text.splitlines():
                if not line:
                    raise ValueError("empty NDJSON line")
                cleaned, count = _redact(json.loads(line))
                values.append(cleaned)
                detected += count
            encoded = b"".join(
                contract.canonical_document_bytes(value) + b"\n" for value in values
            )
            return encoded, detected
        cleaned, detected = _redact(json.loads(text))
        return contract.canonical_document_bytes(cleaned), detected
    except CurrentWorkerDebugError:
        raise
    except Exception as exc:
        raise CurrentWorkerDebugError(
            "DEBUG_REDACTION_FAILED", path
        ) from exc


def _entry(path: str, raw: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": object_sha256(raw),
        "size_bytes": len(raw),
        "media_type": _MEDIA_TYPES[path],
        "encoding": "utf-8",
    }


def _report(policy_digest: str, detected: int) -> tuple[dict[str, object], bytes]:
    value = contract.seal_document(
        "worker-debug-redaction-report/v1",
        {
            "schema_id": "worker-debug-redaction-report/v1",
            "policy_digest": policy_digest,
            "structured_pass_detection_count": detected,
            "archive_rescan_detection_count": 0,
            "redacted_value_count": detected,
            "status": "redacted" if detected else "clean",
        },
    )
    return value, canonical_bytes("worker-debug-redaction-report/v1", value)


def _manifest(payloads: dict[str, bytes]) -> tuple[dict[str, object], bytes]:
    entries = [_entry(path, payloads[path]) for path in sorted(payloads)]
    value = contract.seal_document(
        "worker-debug-file-manifest/v1",
        {
            "schema_id": "worker-debug-file-manifest/v1",
            "entries": entries,
            "entry_count": len(entries),
            "total_size_bytes": sum(item["size_bytes"] for item in entries),
        },
    )
    return value, canonical_bytes("worker-debug-file-manifest/v1", value)


def _summary(
    base: object, ignored: list[str], omitted: list[str]
) -> bytes:
    if not isinstance(base, dict):
        raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED", "debug-summary.json")
    value = dict(base)
    value["ignored_paths"] = sorted(ignored)
    value["omitted_paths"] = sorted(omitted)
    return contract.canonical_document_bytes(value)


def _archive(payloads: dict[str, bytes], manifest: dict[str, object]) -> bytes:
    files = contract.canonical_document_bytes(
        {
            "entries": manifest["entries"],
            "entry_count": manifest["entry_count"],
            "total_size_bytes": manifest["total_size_bytes"],
        }
    )
    entries = {**payloads, "fragment-files.json": files}
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as bundle:
        for path in sorted(entries):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.flag_bits |= 0x800
            bundle.writestr(info, entries[path], compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def _secret_count(path: str, raw: bytes) -> int:
    value, _ = _decode_json(path, raw)
    return 0 if value == raw else 1


def _rescan(archive: bytes, staging_root: Path) -> None:
    try:
        with tempfile.TemporaryDirectory(
            prefix="debug-rescan-", dir=staging_root
        ) as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = bundle.namelist()
                if names != sorted(names) or len(names) != len(set(names)):
                    raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED")
                for name in names:
                    if name not in {*_MEDIA_TYPES, "fragment-files.json"}:
                        raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED")
                    raw = bundle.read(name)
                    target = root / name
                    descriptor = os.open(
                        target,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                    try:
                        view = memoryview(raw)
                        while view:
                            written = os.write(descriptor, view)
                            if written < 1:
                                raise CurrentWorkerDebugError(
                                    "DEBUG_REDACTION_FAILED"
                                )
                            view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    if _secret_count(name, raw):
                        raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED")
    except CurrentWorkerDebugError:
        raise
    except Exception as exc:
        raise CurrentWorkerDebugError("DEBUG_REDACTION_FAILED") from exc


def prepare_debug_content(
    *,
    input_root: Path,
    redaction_plan: dict[str, object],
    limits: DebugCaptureLimits,
    staging_root: Path,
) -> PreparedDebugContent:
    root = require_input_root(input_root)
    try:
        root_info = root.lstat()
        children = list(root.iterdir())
    except OSError as exc:
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE") from exc
    if not stat.S_ISDIR(root_info.st_mode) or _is_reparse(root_info):
        raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE")
    by_fold: dict[str, list[Path]] = {}
    for child in children:
        by_fold.setdefault(child.name.casefold(), []).append(child)
    allowed_fold = {name.casefold(): name for name in _MEDIA_TYPES}
    for folded, paths in by_fold.items():
        if folded in allowed_fold and (
            len(paths) != 1 or paths[0].name != allowed_fold[folded]
        ):
            raise CurrentWorkerDebugError("DEBUG_UNAVAILABLE", "case collision")

    ignored = sorted(
        child.name for child in children if child.name not in _MEDIA_TYPES
    )
    omitted: list[str] = []
    payloads: dict[str, bytes] = {}
    detected = 0
    summary_base: object = {}
    for path in sorted(set(_MEDIA_TYPES) - _GENERATED):
        candidate = root / path
        if not os.path.lexists(candidate):
            continue
        raw = _read_regular(candidate, limits.max_file_bytes)
        if raw is None:
            omitted.append(path)
            continue
        cleaned, count = _decode_json(path, raw)
        detected += count
        if path == "debug-summary.json":
            summary_base = json.loads(cleaned)
        else:
            payloads[path] = cleaned

    def assemble() -> tuple[dict[str, bytes], dict[str, object], bytes, bytes]:
        current = dict(payloads)
        current["debug-summary.json"] = _summary(
            summary_base, ignored, omitted
        )
        report, report_raw = _report(redaction_plan["policy_digest"], detected)
        current["redaction-report.json"] = report_raw
        manifest, manifest_raw = _manifest(current)
        return current, report, manifest_raw, _archive(current, manifest)

    while True:
        current, report, manifest_raw, archive = assemble()
        total = sum(len(raw) for raw in current.values())
        if (
            len(current) <= limits.max_entries
            and total <= limits.max_total_bytes
            and len(archive) <= limits.max_archive_bytes
        ):
            break
        removable = sorted(
            set(payloads) - {"debug-summary.json"}, reverse=True
        )
        if not removable:
            raise CurrentWorkerDebugError("DEBUG_LIMIT_EXCEEDED")
        removed = removable[0]
        payloads.pop(removed)
        omitted.append(removed)

    manifest, manifest_raw = _manifest(current)
    report_raw = current["redaction-report.json"]
    _rescan(archive, staging_root)
    return PreparedDebugContent(
        payloads=current,
        file_manifest=manifest,
        file_manifest_bytes=manifest_raw,
        redaction_report=report,
        redaction_report_bytes=report_raw,
        archive_bytes=archive,
        status="partial" if omitted else "complete",
        reason_code="DEBUG_LIMIT_EXCEEDED" if omitted else None,
    )


__all__ = ["PreparedDebugContent", "prepare_debug_content"]
