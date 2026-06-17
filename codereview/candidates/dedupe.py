from __future__ import annotations

import hashlib


def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for candidate in candidates:
        key = _dedupe_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _dedupe_key(candidate: dict) -> str:
    explicit = str(candidate.get("dedupe_key") or "").strip()
    if explicit:
        return explicit
    location = candidate.get("evidence") or candidate.get("code_evidence")
    if isinstance(location, list) and location:
        location = location[0]
    return hashlib.sha1(
        f"{candidate.get('claim') or candidate.get('title')}|{candidate.get('category')}|{location}".encode("utf-8")
    ).hexdigest()
