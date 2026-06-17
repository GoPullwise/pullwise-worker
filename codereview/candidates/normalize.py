from __future__ import annotations

import hashlib

from ..utils.paths import safe_relative_path


REQUIRED_FIELDS = {
    "title",
    "severity",
    "category",
    "graph_evidence",
    "code_evidence",
    "trigger_condition",
    "expected_behavior",
    "actual_behavior_hypothesis",
    "minimal_repro_idea",
}


def normalize_candidates(raw_candidates: list[dict]) -> list[dict]:
    normalized = []
    for raw in raw_candidates:
        result = raw.get("result") if isinstance(raw, dict) else {}
        candidates = result.get("candidates") if isinstance(result, dict) else []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            clean = dict(candidate)
            clean["issue_id"] = str(clean.get("issue_id") or _candidate_id(clean))
            clean["source_task"] = raw.get("task") if isinstance(raw.get("task"), dict) else {}
            clean["valid"] = candidate_has_required_evidence(clean)
            normalized.append(clean)
    return normalized


def candidate_has_required_evidence(candidate: dict) -> bool:
    if not all(candidate.get(field) for field in REQUIRED_FIELDS):
        return False
    return bool(candidate.get("graph_evidence")) and valid_code_evidence(candidate.get("code_evidence"))


def valid_code_evidence(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if isinstance(item, str):
            file_part = item.split(":", 1)[0]
            if safe_relative_path(file_part):
                return True
            continue
        if not isinstance(item, dict):
            continue
        file_path = safe_relative_path(item.get("file") or item.get("path"))
        if not file_path:
            continue
        try:
            start = int(item.get("startLine") or item.get("start_line") or item.get("line") or 0)
        except (TypeError, ValueError):
            start = 0
        if start > 0:
            return True
    return False


def _candidate_id(candidate: dict) -> str:
    key = "|".join(str(candidate.get(field) or "") for field in ("title", "severity", "category"))
    return "issue_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
