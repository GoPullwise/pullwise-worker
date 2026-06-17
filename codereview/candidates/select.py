from __future__ import annotations

from ..config import ReviewConfig


def select_for_repro(candidates: list[dict], config: ReviewConfig) -> list[dict]:
    selected = []
    min_score = getattr(getattr(config, "scoring", None), "min_score_for_repro", 8)
    always = set(getattr(getattr(config, "scoring", None), "always_repro_severities", {"critical", "high"}))
    for candidate in candidates:
        if not candidate.get("valid"):
            continue
        if candidate.get("needs_network") is True:
            continue
        severity = str(candidate.get("severity") or "").lower()
        repro_likelihood = str(candidate.get("repro_likelihood") or "medium").lower()
        score = int(candidate.get("score") or 0)
        if score < min_score and not (severity in always and repro_likelihood != "low"):
            continue
        selected.append(candidate)
        if len(selected) >= config.max_repro:
            break
    return selected
