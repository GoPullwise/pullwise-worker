from __future__ import annotations


def plan_repairs(audit: dict) -> list[dict]:
    repairs = audit.get("repairs")
    return list(repairs) if isinstance(repairs, list) else []


def merge_repairs(graph: dict, repair_results: list[dict]) -> dict:
    if not repair_results:
        return graph
    nodes = [*graph.get("nodes", [])]
    edges = [*graph.get("edges", [])]
    unresolved = [*graph.get("unresolved_refs", [])]
    for result in repair_results:
        if not isinstance(result, dict):
            continue
        nodes.extend(result.get("nodes") or [])
        edges.extend(result.get("edges") or [])
        unresolved.extend(result.get("unresolved_refs") or [])
    from .merge import merge_graph_results

    return merge_graph_results([{"nodes": nodes, "edges": edges, "unresolved_refs": unresolved, "coverage": graph.get("coverage") or {}}])
