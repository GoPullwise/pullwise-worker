"""Repository, provenance, readiness, and history gates for decision records."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from scripts.agent_first_decision_catalog import (
    NORMATIVE_PATHS,
    NORMATIVE_UNIT_CATALOG,
    REPORT_SCHEMA_ID,
    SCHEMA_ID,
    SLICES,
)
from scripts.agent_first_decision_core import (
    decision_applicability,
    validate_register,
)
from scripts.agent_first_decision_render import render_document


REFERENCE_RE = re.compile(r"D[1-9][0-9]*@sha256:[0-9a-f]{64}")
MANIFEST_PATH = "contracts/agent-first/spec-decision-register.json"


class DecisionRegisterObservationError(RuntimeError):
    pass


def _repo_path(repo_root: Path, relative: str) -> Path:
    root = repo_root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise DecisionRegisterObservationError(f"outside_repo:{relative}") from exc
    return candidate


def _canonical_regular_text(repo_root: Path, relative: str) -> str:
    path = _repo_path(repo_root, relative)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise DecisionRegisterObservationError(f"not_regular:{relative}")
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        raise DecisionRegisterObservationError(f"unreadable:{relative}:{exc}") from exc


def _generated_document_matches(register: dict[str, Any], repo_root: Path) -> bool:
    document = register["document"]
    text = _canonical_regular_text(repo_root, document["path"])
    start, end = document["start_marker"], document["end_marker"]
    start_token, end_token = f"{start}\n", f"\n{end}"
    if text.count(start) != 1 or text.count(end) != 1:
        return False
    start_index = text.find(start_token)
    if start_index < 0:
        return False
    body_start = start_index + len(start_token)
    end_index = text.find(end_token, body_start)
    return end_index >= body_start and text[body_start:end_index] == render_document(register)


def _reference_token(decision: dict[str, Any]) -> str:
    return f"{decision['id']}@sha256:{decision['resolution']['resolution_sha256']}"


def _expected_unit_body(
    register: dict[str, Any], unit: dict[str, Any]
) -> str | None:
    by_id = {item["id"]: item for item in register["decisions"]}
    manifest_unit = next(item for item in register["normative_units"] if item["id"] == unit["id"])
    tokens: list[str] = []
    for decision_id in manifest_unit["decision_ids"]:
        applicability = decision_applicability(register, decision_id)
        decision = by_id[decision_id]
        if applicability == "inactive":
            continue
        if applicability != "active" or decision["status"] != "resolved":
            return None
        tokens.append(f"<!-- {_reference_token(decision)} -->")
    return "\n".join(tokens)


def _unit_span(text: str, unit: dict[str, str]) -> tuple[int, int] | None:
    start, end = unit["start_marker"], unit["end_marker"]
    if text.count(start) == 0 and text.count(end) == 0:
        return None
    if text.count(start) != 1 or text.count(end) != 1:
        return (-1, -1)
    start_token, end_token = f"{start}\n", f"\n{end}"
    start_index = text.find(start_token)
    if start_index < 0:
        return (-1, -1)
    body_start = start_index + len(start_token)
    end_index = text.find(end_token, body_start)
    return (body_start, end_index) if end_index >= body_start else (-1, -1)


def normative_reference_failures(
    register: dict[str, Any], repo_root: Path, *, require_slice: str | None
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    texts = {path: _canonical_regular_text(repo_root, path) for path in NORMATIVE_PATHS}
    decisions = {item["id"]: item for item in register["decisions"]}
    spans_by_path: dict[str, list[tuple[int, int, str]]] = {path: [] for path in texts}
    slice_index = SLICES.index(require_slice) if require_slice is not None else None

    for unit in NORMATIVE_UNIT_CATALOG:
        text = texts[unit["path"]]
        span = _unit_span(text, unit)
        expected = _expected_unit_body(register, unit)
        due = slice_index is not None and SLICES.index(unit["required_by_slice"]) <= slice_index
        if span == (-1, -1):
            failures.append({"code": "normative_unit_markers_invalid", "unit_id": unit["id"], "path": unit["path"]})
            continue
        if span is None:
            if due and expected is not None:
                failures.append({"code": "normative_unit_reference_missing", "unit_id": unit["id"], "path": unit["path"]})
            continue
        spans_by_path[unit["path"]].append((span[0], span[1], unit["id"]))
        actual = text[span[0]:span[1]]
        if expected is None or actual != expected:
            failures.append({"code": "normative_unit_reference_drift", "unit_id": unit["id"], "path": unit["path"]})

    for path, text in texts.items():
        for match in REFERENCE_RE.finditer(text):
            token = match.group(0)
            scoped = any(start <= match.start() < end for start, end, _unit in spans_by_path[path])
            if not scoped:
                failures.append({"code": "unscoped_decision_reference", "path": path, "reference": token})
                continue
            decision_id, digest = token.split("@sha256:", 1)
            decision = decisions.get(decision_id)
            if decision is None:
                failures.append({"code": "unknown_decision_reference", "path": path, "reference": token})
            elif decision["status"] != "resolved":
                failures.append({"code": "pending_decision_reference", "path": path, "reference": token})
            elif digest != decision["resolution"]["resolution_sha256"]:
                failures.append({"code": "stale_decision_reference", "path": path, "reference": token})
    return failures


def resolved_history_failures(
    register: dict[str, Any], historical_registers: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    current = {item["id"]: item for item in register["decisions"]}
    failures: list[dict[str, Any]] = []
    frozen: dict[str, bytes] = {}
    for snapshot in historical_registers:
        for decision in snapshot["decisions"]:
            if decision["status"] != "resolved":
                continue
            canonical = json.dumps(
                decision, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            prior = frozen.setdefault(decision["id"], canonical)
            if prior != canonical:
                failures.append({"code": "historical_resolution_rewritten", "decision_id": decision["id"]})
    for decision_id, canonical in frozen.items():
        decision = current.get(decision_id)
        current_bytes = None if decision is None else json.dumps(
            decision, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if current_bytes != canonical:
            failures.append({"code": "resolved_decision_not_immutable", "decision_id": decision_id})
    return failures


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DecisionRegisterObservationError(f"git_failed:{args[0]}:{exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise DecisionRegisterObservationError(f"git_failed:{args[0]}:{detail}")
    return result.stdout


def _history(repo_root: Path) -> list[dict[str, Any]]:
    if _git(repo_root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise DecisionRegisterObservationError("git_history_shallow")
    commits = _git(
        repo_root, "log", "--full-history", "--format=%H", "--", MANIFEST_PATH
    ).decode("ascii").split()
    snapshots: list[dict[str, Any]] = []
    for commit in reversed(commits):
        raw = _git(repo_root, "show", f"{commit}:{MANIFEST_PATH}")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DecisionRegisterObservationError(f"historical_manifest_invalid:{commit}:{exc}") from exc
        if value.get("schema_id") != SCHEMA_ID:
            continue
        try:
            snapshots.append(validate_register(value))
        except ValueError as exc:
            raise DecisionRegisterObservationError(f"historical_manifest_invalid:{commit}:{exc}") from exc
    return snapshots


def verify_register(
    register: dict[str, Any], repo_root: Path, *, require_slice: str | None = None,
    check_document: bool = True, check_history: bool = True,
) -> dict[str, Any]:
    validate_register(register)
    if require_slice is not None and require_slice not in SLICES:
        raise ValueError("require_slice:value")
    failures: list[dict[str, Any]] = []
    try:
        document_matches = not check_document or _generated_document_matches(register, repo_root)
        if check_document and not document_matches:
            failures.append({"code": "generated_document_drift", "path": register["document"]["path"]})
        failures.extend(normative_reference_failures(register, repo_root, require_slice=require_slice))
        if check_history:
            failures.extend(resolved_history_failures(register, _history(repo_root)))
    except DecisionRegisterObservationError as exc:
        return {
            "schema_id": REPORT_SCHEMA_ID, "status": "indeterminate", "valid": False,
            "ready": False, "failures": [], "indeterminate_reasons": [{"code": str(exc)}],
        }
    inactive = sorted(
        item["id"] for item in register["decisions"]
        if decision_applicability(register, item["id"]) == "inactive"
    )
    pending = [
        item for item in register["decisions"]
        if item["status"] == "pending" and item["id"] not in inactive
    ]
    slice_blockers: list[str] = []
    if require_slice is not None:
        limit = SLICES.index(require_slice)
        slice_blockers = sorted(
            item["id"] for item in pending
            if decision_applicability(register, item["id"]) == "active"
            and SLICES.index(item["required_by_slice"]) <= limit
        )
        if slice_blockers:
            failures.append({
                "code": "slice_blocked_by_pending_decisions", "slice": require_slice,
                "decision_ids": slice_blockers,
            })
    readiness_only = bool(failures) and all(
        item["code"] == "slice_blocked_by_pending_decisions" for item in failures
    )
    valid = not failures or readiness_only
    ready = not pending
    status = (
        "blocked" if readiness_only else "invalid" if failures
        else "valid_pending" if pending else "ready"
    )
    failures.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {
        "schema_id": REPORT_SCHEMA_ID, "register_id": register["register_id"],
        "status": status, "valid": valid, "ready": ready,
        "active_decision_id": register["active_decision_id"],
        "pending_decision_count": len(pending),
        "resolved_decision_count": sum(item["status"] == "resolved" for item in register["decisions"]),
        "inactive_decision_count": len(inactive), "inactive_decision_ids": inactive,
        "document_matches": document_matches, "required_slice": require_slice,
        "failures": failures, "indeterminate_reasons": [],
    }
