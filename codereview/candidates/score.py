from __future__ import annotations


SEVERITY_SCORE = {"critical": 100, "high": 80, "medium": 50, "low": 20, "info": 5}


def score_candidates(candidates: list[dict], _scoring: object = None) -> list[dict]:
    scored = []
    for candidate in candidates:
        item = dict(candidate)
        score = SEVERITY_SCORE.get(str(item.get("severity") or "").lower(), 30)
        if item.get("valid"):
            score += 30
        if item.get("affected_tests"):
            score += 10
        if not item.get("needs_network"):
            score += 8
        if item.get("minimal_repro_idea"):
            score += 10
        item["score"] = score
        scored.append(item)
    return sorted(scored, key=lambda row: (-int(row.get("score") or 0), str(row.get("issue_id") or "")))
