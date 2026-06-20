from __future__ import annotations

from pathlib import Path

from ..utils.jsonl import write_json, write_jsonl
from .ids import stable_edge_id


def merge_graph_results(shard_results: list[dict]) -> dict:
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    unresolved: list[dict] = []
    coverage = {"assigned_files": set(), "mapped_files": set()}
    warnings: list[str] = []
    for result in shard_results:
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
        result_coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
        coverage["assigned_files"].update(str(path) for path in result_coverage.get("assigned_files", []) if str(path))
        coverage["mapped_files"].update(str(path) for path in result_coverage.get("mapped_files", []) if str(path))
        warnings.extend(str(item) for item in result.get("warnings", []) if str(item))
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
