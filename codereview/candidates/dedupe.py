from __future__ import annotations


def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for candidate in candidates:
        if not candidate.get("valid"):
            deduped.append(candidate)
            continue
        key = str(candidate.get("dedupe_key") or "").strip()
        if not key:
            deduped.append(candidate)
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped
