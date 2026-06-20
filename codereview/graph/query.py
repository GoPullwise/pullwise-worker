from __future__ import annotations

from .index_memory import MemoryGraphIndex


def get_nodes_by_file(index: MemoryGraphIndex, path: str) -> list[dict]:
    return index.get_nodes_by_file(path)


def get_node(index: MemoryGraphIndex, node_id: str) -> dict | None:
    return index.get_node(node_id)


def get_incoming(index: MemoryGraphIndex, node_id: str, edge_types: set[str] | None = None) -> list[dict]:
    return index.get_incoming(node_id, edge_types)


def get_outgoing(index: MemoryGraphIndex, node_id: str, edge_types: set[str] | None = None) -> list[dict]:
    return index.get_outgoing(node_id, edge_types)


def walk_upstream(index: MemoryGraphIndex, node_id: str, max_depth: int, edge_types: set[str] | None = None) -> list[str]:
    return index.walk(node_id, direction="upstream", max_depth=max_depth, edge_types=edge_types)


def walk_downstream(index: MemoryGraphIndex, node_id: str, max_depth: int, edge_types: set[str] | None = None) -> list[str]:
    return index.walk(node_id, direction="downstream", max_depth=max_depth, edge_types=edge_types)


def find_paths(index: MemoryGraphIndex, source_id: str, target_id: str, max_depth: int = 4, max_paths: int = 20) -> list[list[str]]:
    paths: list[list[str]] = []
    queue: list[list[str]] = [[source_id]]
    while queue and len(paths) < max_paths:
        path = queue.pop(0)
        current = path[-1]
        if len(path) - 1 >= max_depth:
            continue
        for edge in index.get_outgoing(current):
            next_id = str(edge.get("to") or "")
            if not next_id or next_id in path:
                continue
            candidate = [*path, next_id]
            if next_id == target_id:
                paths.append(candidate)
            else:
                queue.append(candidate)
    return paths
