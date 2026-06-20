from __future__ import annotations

from pathlib import Path

from ..inventory.file_hashes import sha256_file


def validate_graph(graph: dict, checkout: Path) -> list[str]:
    node_ids = {str(node.get("id") or "") for node in graph.get("nodes", []) if isinstance(node, dict)}
    violations: list[str] = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source not in node_ids:
            violations.append(f"dangling edge source: {source}")
        if target not in node_ids:
            violations.append(f"dangling edge target: {target}")
        violations.extend(_validate_evidence(edge.get("evidence"), checkout))
    for node in graph.get("nodes", []):
        if isinstance(node, dict):
            violations.extend(_validate_evidence(node.get("evidence"), checkout))
    return violations


def _validate_evidence(value: object, checkout: Path) -> list[str]:
    rows = value if isinstance(value, list) else []
    violations: list[str] = []
    if not rows:
        return ["source evidence missing"]
    for evidence in rows:
        if not isinstance(evidence, dict):
            continue
        rel = str(evidence.get("file") or "")
        path = checkout / rel
        if not rel or not path.is_file():
            violations.append(f"evidence file missing: {rel}")
            continue
        start = int(evidence.get("start_line") or 0)
        end = int(evidence.get("end_line") or 0)
        if start <= 0 or end < start:
            violations.append(f"invalid evidence span: {rel}:{start}-{end}")
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                line_count = sum(1 for _ in handle)
        except OSError:
            line_count = 0
        if line_count and end > line_count:
            violations.append(f"evidence span exceeds file length: {rel}:{start}-{end}")
        expected_hash = str(evidence.get("content_hash") or "")
        if expected_hash and expected_hash != sha256_file(path):
            violations.append(f"evidence content hash is stale: {rel}")
    return violations
