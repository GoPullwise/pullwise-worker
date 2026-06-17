from __future__ import annotations

import hashlib
import re

from ..utils.paths import safe_path_component, safe_relative_path


REQUIRED_FIELDS = {
    "claim",
    "severity",
    "category",
    "graph_evidence",
    "evidence",
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
            clean = canonical_candidate(candidate, raw.get("task") if isinstance(raw.get("task"), dict) else {})
            clean["valid"] = candidate_has_required_evidence(clean)
            normalized.append(clean)
    return normalized


def canonical_candidate(candidate: dict, source_task: dict | None = None) -> dict:
    clean = dict(candidate)
    generated_id = _candidate_id(clean)
    raw_id = clean.get("candidate_id") or generated_id
    issue_id = safe_path_component(raw_id, default=generated_id)
    clean["candidate_id"] = issue_id
    clean["issue_id"] = issue_id
    clean["source_task"] = source_task or {}
    clean["severity"] = str(clean.get("severity") or "medium").lower()
    clean["category"] = normalize_category(clean.get("category"))
    clean["confidence"] = str(clean.get("confidence") or "medium").lower()
    clean["repro_likelihood"] = str(clean.get("repro_likelihood") or "medium").lower()

    claim = str(clean.get("claim") or "").strip()
    clean["claim"] = claim
    clean["title"] = claim[:96] or issue_id

    evidence = clean.get("evidence")
    clean["evidence"] = evidence
    clean["code_evidence"] = evidence_to_code_evidence(evidence)
    clean["dedupe_key"] = str(clean.get("dedupe_key") or _canonical_dedupe_key(clean))
    return clean


def normalize_category(value: object) -> str:
    text = str(value or "correctness").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "correctness": "correctness",
        "security": "security_auth_dataflow",
        "security_auth": "security_auth_dataflow",
        "security_auth_dataflow": "security_auth_dataflow",
        "api": "api_contract",
        "api_contract": "api_contract",
        "state": "state_concurrency_resource",
        "concurrency": "state_concurrency_resource",
        "resource": "state_concurrency_resource",
        "state_concurrency_resource": "state_concurrency_resource",
        "test": "test_repro",
        "test_repro": "test_repro",
    }
    return aliases.get(text, text or "correctness")


def candidate_has_required_evidence(candidate: dict) -> bool:
    if not all(candidate.get(field) for field in REQUIRED_FIELDS):
        return False
    return bool(candidate.get("graph_evidence")) and valid_code_evidence(candidate.get("evidence"))


def valid_code_evidence(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = safe_relative_path(item.get("file") or item.get("path"))
        if not file_path:
            continue
        start = _line_start(item)
        if start > 0:
            return True
    return False


def evidence_to_code_evidence(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    rendered = []
    for item in value:
        if isinstance(item, str):
            rendered.append(item)
            continue
        if not isinstance(item, dict):
            continue
        file_path = safe_relative_path(item.get("file") or item.get("path"))
        lines = str(item.get("lines") or "").strip()
        if file_path:
            rendered.append(f"{file_path}:{lines}" if lines else file_path)
    return rendered


def _line_start(item: dict) -> int:
    raw = item.get("startLine") or item.get("start_line") or item.get("line") or item.get("lines") or 0
    if isinstance(raw, str):
        match = re.search(r"\d+", raw)
        raw = match.group(0) if match else "0"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _candidate_id(candidate: dict) -> str:
    key = "|".join(str(candidate.get(field) or "") for field in ("dedupe_key", "claim", "severity", "category"))
    return "issue_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _canonical_dedupe_key(candidate: dict) -> str:
    source = candidate.get("source_task") if isinstance(candidate.get("source_task"), dict) else {}
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    first_file = ""
    if evidence and isinstance(evidence[0], dict):
        first_file = str(evidence[0].get("file") or "")
    return "|".join(
        str(part or "")
        for part in (
            candidate.get("category"),
            source.get("slice_id"),
            first_file,
            candidate.get("trigger_condition"),
            candidate.get("expected_behavior"),
        )
    )
