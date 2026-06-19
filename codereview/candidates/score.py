from __future__ import annotations


SEVERITY_SCORE = {"critical": 5, "high": 4, "medium": 2, "low": 1, "info": 0}
CONFIDENCE_SCORE = {"high": 3, "medium": 2, "low": 0}
REPRO_SCORE = {"high": 3, "medium": 1, "low": -2}
ENTRYPOINT_TAGS = {"public-entrypoint", "route", "cli", "job"}
SINK_TAGS = {"auth", "authorization", "db-write", "filesystem", "network", "resource"}


def score_candidates(candidates: list[dict], _scoring: object = None) -> list[dict]:
    scored = []
    for candidate in candidates:
        item = dict(candidate)
        score = SEVERITY_SCORE.get(str(item.get("severity") or "").lower(), 1)
        score += CONFIDENCE_SCORE.get(str(item.get("confidence") or "medium").lower(), 0)
        score += REPRO_SCORE.get(str(item.get("repro_likelihood") or "medium").lower(), 0)
        if item.get("valid"):
            score += 1
        if item.get("repository_tests"):
            score += 2
        tags = _risk_tags(item)
        if tags & ENTRYPOINT_TAGS:
            score += 2
        if tags & SINK_TAGS:
            score += 2
        if not item.get("needs_network"):
            score += 1
        else:
            score -= 3
        if item.get("minimal_repro_idea"):
            score += 1
        item["score"] = score
        scored.append(item)
    return sorted(scored, key=lambda row: (-int(row.get("score") or 0), str(row.get("issue_id") or "")))


def _risk_tags(candidate: dict) -> set[str]:
    tags = candidate.get("risk_tags")
    if isinstance(tags, list):
        return {str(tag) for tag in tags}
    source = candidate.get("source_task") if isinstance(candidate.get("source_task"), dict) else {}
    tags = source.get("risk_tags")
    return {str(tag) for tag in tags} if isinstance(tags, list) else set()
