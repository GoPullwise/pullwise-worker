from __future__ import annotations

from pathlib import Path

from ..inventory.file_hashes import sha256_file
from ..inventory.git_inventory import analyzable_files
from ..utils.jsonl import write_json, write_jsonl
from .contracts import language_for_path, risk_tags_for_path
from .ids import file_node_id, stable_edge_id


def merge_graph_results(shard_results: list[dict]) -> dict:
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    unresolved: list[dict] = []
    coverage = {"assigned_files": set(), "mapped_files": set()}
    warnings: list[str] = []
    for result in shard_results:
        status = str(result.get("status") or "ok").lower()
        result_coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
        coverage["assigned_files"].update(str(path) for path in result_coverage.get("assigned_files", []) if str(path))
        warnings.extend(str(item) for item in result.get("warnings", []) if str(item))
        if status != "ok":
            reason = str(result.get("blocked_reason") or result.get("error") or "non-ok graph shard result")
            task_id = str(result.get("task_id") or result.get("shard_id") or "unknown")
            warnings.append(f"ignored non-ok graph shard {task_id}: {reason}")
            continue
        for node in result.get("nodes", []):
            if not isinstance(node, dict) or not node.get("id"):
                continue
            nodes.setdefault(str(node["id"]), node)
        for edge in result.get("edges", []):
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("from") or "")
            target = str(edge.get("to") or "")
            edge_type = str(edge.get("type") or "")
            if not source or not target or not edge_type:
                continue
            if not edge.get("id"):
                edge["id"] = stable_edge_id(source, target, edge_type, edge.get("evidence"))
            edges.setdefault(str(edge["id"]), edge)
        unresolved.extend(item for item in result.get("unresolved_refs", []) if isinstance(item, dict))
        coverage["mapped_files"].update(str(path) for path in result_coverage.get("mapped_files", []) if str(path))
    conflicts = detect_dual_map_conflicts(shard_results)
    graph = {
        "manifest": {
            "schema_version": "3",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "unresolved_count": len(unresolved),
            "dual_map_conflict_count": len(conflicts),
            "semantic_source": "codex-agent-or-local-conservative",
        },
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "unresolved_refs": unresolved,
        "coverage": {
            "assigned_files": sorted(coverage["assigned_files"]),
            "mapped_files": sorted(coverage["mapped_files"]),
        },
        "conflicts": conflicts,
        "warnings": warnings,
    }
    graph["indexes"] = build_inline_indexes(graph)
    return graph


def normalize_graph_for_inventory(graph: dict, inventory: dict, checkout: Path) -> dict:
    source_nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict) and node.get("id")]
    source_edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]
    nodes: dict[str, dict] = {}
    line_counts: dict[str, int] = {}
    content_hashes: dict[str, str] = {}
    dropped_nodes = 0
    dropped_edges = 0

    for node in source_nodes:
        item = dict(node)
        evidence = _valid_evidence_rows(item.get("evidence"), checkout, line_counts, content_hashes)
        if not evidence:
            evidence = _node_fallback_evidence(item, checkout, line_counts, content_hashes)
        if not evidence:
            dropped_nodes += 1
            continue
        item["evidence"] = evidence
        nodes.setdefault(str(item["id"]), item)

    for file_info in analyzable_files(inventory):
        rel = str(file_info.get("path") or "")
        if not rel or not (checkout / rel).is_file():
            continue
        node_id = file_node_id(rel)
        if node_id not in nodes:
            nodes[node_id] = _inventory_file_node(rel, file_info)

    edges: dict[str, dict] = {}
    for edge in source_edges:
        item = dict(edge)
        source = str(item.get("from") or "")
        target = str(item.get("to") or "")
        edge_type = str(item.get("type") or "")
        if source not in nodes or target not in nodes or not edge_type:
            dropped_edges += 1
            continue
        evidence = _valid_evidence_rows(item.get("evidence"), checkout, line_counts, content_hashes)
        if not evidence:
            dropped_edges += 1
            continue
        item["evidence"] = evidence
        item["id"] = str(item.get("id") or stable_edge_id(source, target, edge_type, evidence))
        edges.setdefault(str(item["id"]), item)

    unresolved = []
    for ref in graph.get("unresolved_refs", []) or []:
        if not isinstance(ref, dict):
            continue
        source = str(ref.get("source_node") or "")
        if source and source not in nodes:
            continue
        unresolved.append(ref)

    normalized = {
        **graph,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "unresolved_refs": unresolved,
        "warnings": [
            *[str(item) for item in graph.get("warnings", []) if str(item)],
            *([f"graph normalizer dropped {dropped_nodes} node(s) with invalid source evidence"] if dropped_nodes else []),
            *([f"graph normalizer dropped {dropped_edges} edge(s) with dangling endpoints or invalid source evidence"] if dropped_edges else []),
        ],
    }
    normalized["indexes"] = build_inline_indexes(normalized)
    normalized["manifest"] = {
        **(graph.get("manifest") or {}),
        "node_count": len(normalized["nodes"]),
        "edge_count": len(normalized["edges"]),
        "unresolved_count": len(unresolved),
        "normalization": {
            "dropped_nodes": dropped_nodes,
            "dropped_edges": dropped_edges,
            "inventory_file_nodes_ensured": len(analyzable_files(inventory)),
        },
    }
    return normalized


def build_inline_indexes(graph: dict) -> dict:
    nodes_by_file: dict[str, list[str]] = {}
    nodes_by_name: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        file_path = str(node.get("file") or "")
        name = str(node.get("name") or "")
        if node_id and file_path:
            nodes_by_file.setdefault(file_path, []).append(node_id)
        if node_id and name:
            nodes_by_name.setdefault(name, []).append(node_id)
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        edge_id = str(edge.get("id") or "")
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if edge_id and source:
            outgoing.setdefault(source, []).append(edge_id)
        if edge_id and target:
            incoming.setdefault(target, []).append(edge_id)
    return {
        "nodes_by_file": {key: sorted(value) for key, value in nodes_by_file.items()},
        "nodes_by_name": {key: sorted(value) for key, value in nodes_by_name.items()},
        "outgoing_edges": {key: sorted(value) for key, value in outgoing.items()},
        "incoming_edges": {key: sorted(value) for key, value in incoming.items()},
    }


def write_graph_artifacts(graph_dir: Path, graph: dict) -> None:
    graph_dir.mkdir(parents=True, exist_ok=True)
    write_json(graph_dir / "manifest.json", graph.get("manifest") or {})
    write_jsonl(graph_dir / "nodes.jsonl", graph.get("nodes") or [])
    write_jsonl(graph_dir / "edges.jsonl", graph.get("edges") or [])
    write_jsonl(graph_dir / "unresolved.jsonl", graph.get("unresolved_refs") or [])
    write_json(graph_dir / "coverage.json", graph.get("coverage") or {})
    write_json(graph_dir / "conflicts.json", graph.get("conflicts") or [])
    write_json(graph_dir / "indexes.json", graph.get("indexes") or {})


def _inventory_file_node(rel: str, file_info: dict) -> dict:
    line_count = max(1, int(file_info.get("line_count") or 1))
    language = language_for_path(rel)
    return {
        "id": file_node_id(rel),
        "kind": "test_file" if _is_test_file(rel) else "file",
        "name": Path(rel).name,
        "qualified_name": rel,
        "language": language,
        "file": rel,
        "span": {"start_line": 1, "end_line": line_count},
        "signature": rel,
        "visibility": "repository",
        "content_hash": file_info.get("content_hash") or "",
        "attributes": risk_tags_for_path(rel),
        "evidence": [
            {
                "file": rel,
                "start_line": 1,
                "end_line": line_count,
                "evidence_kind": "inventory_file",
                "content_hash": file_info.get("content_hash") or "",
            }
        ],
        "generated_by": {"worker_id": "graph-normalizer", "prompt_version": "deterministic-file-node-v3", "schema_version": "3"},
    }


def _node_fallback_evidence(
    node: dict,
    checkout: Path,
    line_counts: dict[str, int],
    content_hashes: dict[str, str],
) -> list[dict]:
    rel = str(node.get("file") or "")
    span = node.get("span") if isinstance(node.get("span"), dict) else {}
    start = int(span.get("start_line") or 1)
    end = int(span.get("end_line") or start)
    row = {
        "file": rel,
        "start_line": start,
        "end_line": end,
        "evidence_kind": "node_span",
        "content_hash": str(node.get("content_hash") or ""),
    }
    return _valid_evidence_rows([row], checkout, line_counts, content_hashes)


def _valid_evidence_rows(
    value: object,
    checkout: Path,
    line_counts: dict[str, int],
    content_hashes: dict[str, str],
) -> list[dict]:
    rows = value if isinstance(value, list) else []
    valid: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = str(row.get("file") or "")
        path = checkout / rel
        if not rel or not path.is_file():
            continue
        start = int(row.get("start_line") or 0)
        end = int(row.get("end_line") or 0)
        if start <= 0 or end < start:
            continue
        line_count = _line_count(path, rel, line_counts)
        if line_count and end > line_count:
            continue
        expected_hash = str(row.get("content_hash") or "")
        if expected_hash and expected_hash != _content_hash(path, rel, content_hashes):
            continue
        valid.append(dict(row))
    return valid


def _line_count(path: Path, rel: str, cache: dict[str, int]) -> int:
    if rel in cache:
        return cache[rel]
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            cache[rel] = sum(1 for _ in handle)
    except OSError:
        cache[rel] = 0
    return cache[rel]


def _content_hash(path: Path, rel: str, cache: dict[str, str]) -> str:
    if rel not in cache:
        cache[rel] = sha256_file(path)
    return cache[rel]


def _is_test_file(rel: str) -> bool:
    lower = rel.lower()
    return "/test" in lower or "/tests" in lower or lower.endswith((".test.py", ".spec.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))


def detect_dual_map_conflicts(shard_results: list[dict]) -> list[dict]:
    by_shard: dict[str, list[dict]] = {}
    for result in shard_results:
        shard_id = str(result.get("shard_id") or "")
        if shard_id:
            by_shard.setdefault(shard_id, []).append(result)
    conflicts: list[dict] = []
    for shard_id, results in sorted(by_shard.items()):
        if len(results) < 2:
            continue
        baseline = _result_signature(results[0])
        for other in results[1:]:
            signature = _result_signature(other)
            missing_nodes = sorted(baseline["nodes"] - signature["nodes"])
            extra_nodes = sorted(signature["nodes"] - baseline["nodes"])
            missing_edges = sorted(baseline["edges"] - signature["edges"])
            extra_edges = sorted(signature["edges"] - baseline["edges"])
            if missing_nodes or extra_nodes or missing_edges or extra_edges:
                conflicts.append(
                    {
                        "shard_id": shard_id,
                        "baseline_task_id": results[0].get("task_id"),
                        "other_task_id": other.get("task_id"),
                        "missing_nodes": missing_nodes[:50],
                        "extra_nodes": extra_nodes[:50],
                        "missing_edges": missing_edges[:50],
                        "extra_edges": extra_edges[:50],
                    }
                )
    return conflicts


def _result_signature(result: dict) -> dict[str, set[str]]:
    nodes = {str(node.get("id") or "") for node in result.get("nodes", []) if isinstance(node, dict) and node.get("id")}
    edges = {
        f"{edge.get('from')}->{edge.get('type')}->{edge.get('to')}"
        for edge in result.get("edges", [])
        if isinstance(edge, dict) and edge.get("from") and edge.get("to") and edge.get("type")
    }
    return {"nodes": nodes, "edges": edges}
