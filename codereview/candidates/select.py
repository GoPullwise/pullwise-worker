from __future__ import annotations

from ..config import ReviewConfig


def select_for_repro(candidates: list[dict], config: ReviewConfig) -> list[dict]:
    selected = []
    for candidate in candidates:
        if not candidate.get("valid"):
            continue
        if candidate.get("needs_network") is True:
            continue
        selected.append(candidate)
        if len(selected) >= config.max_repro:
            break
    return selected
