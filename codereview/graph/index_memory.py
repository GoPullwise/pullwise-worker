from __future__ import annotations


class MemoryGraphIndex:
    def __init__(self, graph: dict) -> None:
        self.graph = graph
        self.nodes_by_id = {str(node.get("id") or ""): node for node in graph.get("nodes", []) if isinstance(node, dict)}
        self.edges_by_id = {str(edge.get("id") or ""): edge for edge in graph.get("edges", []) if isinstance(edge, dict)}
        self.nodes_by_file: dict[str, list[dict]] = {}
        self.nodes_by_name: dict[str, list[dict]] = {}
        self.outgoing_edges: dict[str, list[dict]] = {}
        self.incoming_edges: dict[str, list[dict]] = {}
        for node in self.nodes_by_id.values():
            self.nodes_by_file.setdefault(str(node.get("file") or ""), []).append(node)
            self.nodes_by_name.setdefault(str(node.get("name") or ""), []).append(node)
        for edge in self.edges_by_id.values():
            self.outgoing_edges.setdefault(str(edge.get("from") or ""), []).append(edge)
            self.incoming_edges.setdefault(str(edge.get("to") or ""), []).append(edge)

    def get_node(self, node_id: str) -> dict | None:
        return self.nodes_by_id.get(node_id)

    def get_nodes_by_file(self, path: str) -> list[dict]:
        return list(self.nodes_by_file.get(path, []))

    def get_nodes_by_name(self, name: str) -> list[dict]:
        return list(self.nodes_by_name.get(name, []))

    def get_outgoing(self, node_id: str, edge_types: set[str] | None = None) -> list[dict]:
        edges = list(self.outgoing_edges.get(node_id, []))
        return [edge for edge in edges if not edge_types or edge.get("type") in edge_types]

    def get_incoming(self, node_id: str, edge_types: set[str] | None = None) -> list[dict]:
        edges = list(self.incoming_edges.get(node_id, []))
        return [edge for edge in edges if not edge_types or edge.get("type") in edge_types]

    def walk(self, node_id: str, *, direction: str, max_depth: int, edge_types: set[str] | None = None, max_nodes: int = 100) -> list[str]:
        seen = {node_id}
        frontier = [(node_id, 0)]
        while frontier and len(seen) < max_nodes:
            current, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            edges = self.get_incoming(current, edge_types) if direction == "upstream" else self.get_outgoing(current, edge_types)
            for edge in edges:
                next_id = str(edge.get("from") if direction == "upstream" else edge.get("to") or "")
                if next_id and next_id not in seen:
                    seen.add(next_id)
                    frontier.append((next_id, depth + 1))
        return [item for item in seen if item != node_id]
