from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .repo_profile import RepoProfile

_BASE = {"critical": 100, "high": 80, "medium": 55, "low": 35, "info": 15}


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: dict
    score: int
    reasons: tuple[str, ...]


def rank_candidates(candidates: Iterable[dict], profile: RepoProfile) -> list[ScoredCandidate]:
    scored = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            scored.append(score_candidate(candidate, profile))
    return sorted(scored, key=lambda item: (-item.score, str(item.candidate.get("candidate_id") or "")))


def score_candidate(candidate: dict, profile: RepoProfile) -> ScoredCandidate:
    score = _BASE.get(str(candidate.get("severity") or "info").lower(), _BASE["info"])
    reasons = ["severity"]
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    files = []
    for item in evidence:
        if isinstance(item, dict) and item.get("file"):
            files.append(str(item.get("file")))
    if files:
        score += min(18, len(set(files)) * 4)
        reasons.append("files")
    for path in files[:8]:
        risk = profile.risk_for_path(path)
        if risk:
            score += min(28, risk // 3)
            reasons.append("risk")
    if str(candidate.get("expected_behavior_source") or "").strip():
        score += 10
        reasons.append("source")
    else:
        score -= 18
    if len(str(candidate.get("trigger_condition") or "").strip()) >= 20:
        score += 8
    if str(candidate.get("reproduction_idea") or candidate.get("minimal_repro_idea") or "").strip():
        score += 8
        reasons.append("repro")
    return ScoredCandidate(candidate, max(0, min(200, score)), tuple(dict.fromkeys(reasons)))


def select_candidates_for_verification(scored: list[ScoredCandidate], *, limit: int, min_score: int, always_repro_severities: set[str] | frozenset[str]) -> tuple[list[dict], list[dict], list[dict]]:
    selected = []
    rejected = []
    summaries = []
    always = {str(value).lower() for value in always_repro_severities or set()}
    for item in scored:
        severity = str(item.candidate.get("severity") or "info").lower()
        summaries.append({"candidate_id": str(item.candidate.get("candidate_id") or ""), "score": item.score, "reasons": list(item.reasons)})
        if len(selected) < max(0, int(limit or 0)) and (item.score >= max(0, int(min_score or 0)) or severity in always):
            selected.append(item.candidate)
        else:
            rejected.append({"stage": "score-filter", "candidate_id": str(item.candidate.get("candidate_id") or ""), "reason": "not selected by adaptive scoring", "score": item.score})
    if not selected and scored and max(0, int(limit or 0)) > 0:
        selected.append(scored[0].candidate)
    return selected, rejected, summaries
