from __future__ import annotations

import re
from pathlib import Path

from ..units.context import unit_file_stem
from ..utils.paths import safe_path_component, safe_relative_path


REQUIRED_FIELDS = {
    "candidate_id",
    "dedupe_key",
    "category",
    "severity",
    "confidence",
    "claim",
    "graph_evidence",
    "evidence",
    "trigger_condition",
    "expected_behavior",
    "expected_behavior_source",
    "actual_behavior_hypothesis",
    "minimal_repro_idea",
    "repro_likelihood",
}
OPTIONAL_FIELDS = {"repository_tests", "needs_network", "notes"}
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
DERIVED_FIELDS = {"issue_id", "source_task", "title", "code_evidence", "valid", "invalid_reasons", "score"}
CATEGORIES = {"correctness", "security_auth_dataflow", "api_contract", "state_concurrency_resource", "test_repro"}
SEVERITIES = {"critical", "high", "medium", "low"}
CONFIDENCES = {"high", "medium", "low"}
REPRO_LIKELIHOODS = {"high", "medium", "low"}


def normalize_candidates(raw_candidates: list[dict], checkout: Path | None = None, run: Path | None = None) -> list[dict]:
    normalized = []
    for raw in raw_candidates:
        result = raw.get("result") if isinstance(raw, dict) else {}
        candidates = result.get("candidates") if isinstance(result, dict) else []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            clean = canonical_candidate(
                candidate,
                raw.get("task") if isinstance(raw.get("task"), dict) else {},
                checkout=checkout,
                run=run,
            )
            normalized.append(clean)
    return normalized


def canonical_candidate(
    candidate: dict,
    source_task: dict | None = None,
    *,
    checkout: Path | None = None,
    run: Path | None = None,
) -> dict:
    clean = dict(candidate)
    candidate_id = str(clean.get("candidate_id") or "").strip()
    clean["issue_id"] = candidate_id if _is_safe_issue_id(candidate_id) else ""
    clean["source_task"] = source_task or {}
    claim = str(clean.get("claim") or "").strip()
    clean["claim"] = claim
    clean["title"] = claim[:96] or candidate_id

    evidence = clean.get("evidence")
    clean["code_evidence"] = evidence_to_code_evidence(evidence)
    invalid_reasons = validate_candidate(clean, checkout=checkout, run=run)
    clean["valid"] = not invalid_reasons
    clean["invalid_reasons"] = invalid_reasons
    return clean


def validate_candidate(candidate: dict, *, checkout: Path | None = None, run: Path | None = None) -> list[str]:
    reasons: list[str] = []
    missing = [field for field in sorted(REQUIRED_FIELDS) if not _present(candidate.get(field))]
    if missing:
        reasons.append(f"missing required fields: {', '.join(missing)}")

    unexpected = sorted(set(candidate) - ALLOWED_FIELDS - DERIVED_FIELDS)
    if unexpected:
        reasons.append(f"unexpected fields: {', '.join(unexpected)}")

    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if candidate_id and not _is_safe_issue_id(candidate_id):
        reasons.append("candidate_id must be a safe path component")

    _validate_enum(candidate, "category", CATEGORIES, reasons)
    _validate_enum(candidate, "severity", SEVERITIES, reasons)
    _validate_enum(candidate, "confidence", CONFIDENCES, reasons)
    _validate_enum(candidate, "repro_likelihood", REPRO_LIKELIHOODS, reasons)

    graph_reasons = validate_graph_evidence(candidate.get("graph_evidence"), checkout=checkout, run=run)
    if graph_reasons:
        reasons.extend(graph_reasons)

    evidence_reasons = validate_code_evidence(candidate.get("evidence"), checkout=checkout)
    if evidence_reasons:
        reasons.extend(evidence_reasons)

    for field in ("claim", "dedupe_key", "trigger_condition", "expected_behavior", "actual_behavior_hypothesis", "minimal_repro_idea"):
        if field in candidate and not str(candidate.get(field) or "").strip():
            reasons.append(f"{field} must be non-empty")
    expected_source = candidate.get("expected_behavior_source")
    if "expected_behavior_source" in candidate and (
        not isinstance(expected_source, list) or not any(str(item or "").strip() for item in expected_source)
    ):
        reasons.append("expected_behavior_source must be a non-empty list")
    return reasons


def candidate_has_required_evidence(candidate: dict) -> bool:
    return not validate_candidate(candidate)


def valid_graph_evidence(value: object) -> bool:
    return not validate_graph_evidence(value)


def validate_graph_evidence(value: object, *, checkout: Path | None = None, run: Path | None = None) -> list[str]:
    reasons: list[str] = []
    if not isinstance(value, dict):
        return ["graph_evidence must be an object"]
    unexpected = sorted(set(value) - {"unit_id", "context_files", "path_summary"})
    if unexpected:
        reasons.append(f"graph_evidence has unexpected fields: {', '.join(unexpected)}")
    unit_id = str(value.get("unit_id") or "").strip()
    context_files = value.get("context_files")
    path_summary = value.get("path_summary")
    if not unit_id or not isinstance(context_files, list) or not isinstance(path_summary, list):
        reasons.append("graph_evidence requires unit_id, context_files, and path_summary")
    if run is not None and unit_id and not _unit_exists(run, unit_id):
        reasons.append(f"graph_evidence.unit_id does not exist under run/artifacts/review-units: {unit_id}")
    if not isinstance(context_files, list) or not context_files:
        reasons.append("graph_evidence.context_files must be a non-empty list")
    elif not any(str(item or "").strip() for item in context_files):
        reasons.append("graph_evidence.context_files must contain at least one file")
    else:
        for item in context_files:
            rel = _safe_source_path(item)
            if not rel:
                reasons.append(f"graph_evidence.context_files contains an unsafe path: {item}")
                continue
            if checkout is not None and not (checkout / rel).is_file():
                reasons.append(f"graph_evidence.context_files file does not exist: {rel}")
    if not isinstance(path_summary, list) or not path_summary or not any(str(item or "").strip() for item in path_summary):
        reasons.append("graph_evidence.path_summary must be a non-empty list")
    return reasons


def valid_code_evidence(value: object) -> bool:
    return not validate_code_evidence(value)


def validate_code_evidence(value: object, *, checkout: Path | None = None) -> list[str]:
    reasons: list[str] = []
    if not isinstance(value, list) or not value:
        return ["evidence must be a non-empty list"]
    has_valid_location = False
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            reasons.append(f"evidence[{index}] must be an object")
            continue
        unexpected = sorted(set(item) - {"file", "lines", "why_it_matters"})
        if unexpected:
            reasons.append(f"evidence[{index}] has unexpected fields: {', '.join(unexpected)}")
        file_path = safe_relative_path(item.get("file"))
        if not file_path:
            reasons.append(f"evidence[{index}].file must be a safe checkout-relative path")
            continue
        if checkout is not None and not (checkout / file_path).is_file():
            reasons.append(f"evidence[{index}].file does not exist: {file_path}")
            continue
        line_range = _line_range(item.get("lines"))
        if line_range is None:
            reasons.append(f"evidence[{index}].lines must be a positive line or line range")
            continue
        start, end = line_range
        if checkout is not None and (checkout / file_path).is_file():
            line_count = _line_count(checkout / file_path)
            if end > line_count:
                reasons.append(f"evidence[{index}].lines exceed file length: {file_path}")
                continue
        if not str(item.get("why_it_matters") or "").strip():
            reasons.append(f"evidence[{index}].why_it_matters must be non-empty")
            continue
        if start > 0:
            has_valid_location = True
    if not has_valid_location:
        reasons.append("evidence must contain at least one valid file/line location")
    return reasons


def evidence_to_code_evidence(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    rendered = []
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = safe_relative_path(item.get("file"))
        lines = str(item.get("lines") or "").strip()
        if file_path:
            rendered.append(f"{file_path}:{lines}" if lines else file_path)
    return rendered


def _present(value: object) -> bool:
    if isinstance(value, (list, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def _validate_enum(candidate: dict, field: str, allowed: set[str], reasons: list[str]) -> None:
    value = str(candidate.get(field) or "").strip()
    if value and value not in allowed:
        reasons.append(f"{field} must be one of: {', '.join(sorted(allowed))}")


def _is_safe_issue_id(value: str) -> bool:
    return bool(value) and safe_path_component(value, default="") == value


def _safe_source_path(value: object) -> str:
    rel = safe_relative_path(value)
    if not rel:
        return ""
    blocked_prefixes = (".codereview/", ".codegraph/", "node_modules/", ".venv/", "venv/")
    if rel in {".codereview", ".codegraph", "node_modules", ".venv", "venv"} or rel.startswith(blocked_prefixes):
        return ""
    return rel


def _line_range(value: object) -> tuple[int, int] | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", text)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if start <= 0 or end < start:
        return None
    return start, end


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _unit_exists(run: Path, unit_id: str) -> bool:
    stem = unit_file_stem(unit_id)
    units = run / "artifacts" / "review-units"
    return (units / f"{stem}.json").is_file() or (units / f"{stem}.context.md").is_file()
