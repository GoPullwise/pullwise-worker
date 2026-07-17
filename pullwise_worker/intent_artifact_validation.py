from __future__ import annotations

import shlex
from typing import Any

from .agentic_execution import CANDIDATE_KEYS, COMMAND_KEYS


SKIP_REASON_KEYS = (
    "skip_reason",
    "skipped_reason",
    "skipReason",
    "skippedReason",
)

SOURCE_COMMAND_KEYS = (
    *COMMAND_KEYS,
    "intended_command",
    "intendedCommand",
)

SOURCE_TARGET_ID_KEYS = (
    "test_ids",
    "testIds",
    "target_ids",
    "targetIds",
    "target_test_ids",
    "targetTestIds",
    "related_test_ids",
    "relatedTestIds",
    "targets",
)

CANDIDATE_CWD_KEYS = (
    "cwd",
    "working_directory",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _skip_reason(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in SKIP_REASON_KEYS:
        reason = _text(value.get(key))
        if reason:
            return reason
    return ""


def _command(value: object) -> list[str]:
    if isinstance(value, list):
        if not value or any(not isinstance(part, str) or not part.strip() for part in value):
            return []
        return [part.strip() for part in value]
    if isinstance(value, str) and value.strip():
        try:
            return shlex.split(value)
        except ValueError:
            return []
    return []


def _record_command(value: object, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, dict):
        return _command(value)
    for key in keys:
        command = _command(value.get(key))
        if command:
            return command
    return []


def _candidate_values(target: dict[str, Any]) -> list[object]:
    candidates: list[object] = []
    for key in CANDIDATE_KEYS:
        raw = target.get(key)
        if isinstance(raw, list):
            candidates.extend(raw)
    return candidates


def _candidate_cwd(candidate: object) -> str:
    if not isinstance(candidate, dict):
        return ""
    for key in CANDIDATE_CWD_KEYS:
        cwd = candidate.get(key)
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    return ""


def _target_ids(record: dict[str, Any]) -> list[str]:
    target_ids: list[str] = []
    for key in SOURCE_TARGET_ID_KEYS:
        raw = record.get(key)
        values = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
        for value in values:
            target_id = value.strip() if isinstance(value, str) else ""
            if target_id and target_id not in target_ids:
                target_ids.append(target_id)
    return target_ids


def intent_plan_target_contract_errors(payload: dict[str, Any]) -> list[str]:
    targets = payload.get("test_targets")
    if not isinstance(targets, list):
        return []
    errors: list[str] = []
    for index, target in enumerate(targets):
        if not isinstance(target, dict) or _skip_reason(target):
            continue
        candidates = _candidate_values(target)
        field = f"intent-test-plan.json test_targets[{index}].execution_candidates"
        if not candidates:
            errors.append(f"{field} is missing or empty")
            continue
        for candidate_index, candidate in enumerate(candidates):
            if not _record_command(candidate, COMMAND_KEYS):
                errors.append(f"{field}[{candidate_index}].command is missing or empty")
            if not _candidate_cwd(candidate):
                errors.append(f"{field}[{candidate_index}].cwd is missing or empty")
    return errors


def intent_source_record_contract_errors(payload: dict[str, Any]) -> list[str]:
    generated = payload.get("generated_tests")
    if not isinstance(generated, list):
        return []
    errors: list[str] = []
    top_level_skip = _skip_reason(payload)
    for index, record in enumerate(generated):
        if not isinstance(record, dict):
            continue
        field = f"intent-test-source.json generated_tests[{index}]"
        if not _target_ids(record):
            errors.append(f"{field}.target_test_ids is missing or empty")
        if not (top_level_skip or _skip_reason(record)) and not _record_command(record, SOURCE_COMMAND_KEYS):
            errors.append(f"{field}.command is missing or empty")
    return errors
