from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import ReviewConfig
from ..graph.contracts import CALL_EDGE_TYPES, ENTRYPOINT_KINDS, STATE_EDGE_TYPES
from ..graph.index_memory import MemoryGraphIndex


PRODUCTION_SYMBOL_KINDS = {"function", "method", "class", "interface", "type", "enum", "constant", "public_api"}
STATE_KINDS = {"database_model", "database_table", "cache_key", "filesystem_location", "message_topic", "external_service"}
CONFIG_KINDS = {"config_file", "config_key", "env_var", "dependency", "build_script", "ci_job"}
TRUST_TAGS = {"authorization", "authentication", "secret-handling", "validation", "trust-boundary", "public-entrypoint"}


def build_all_review_units(graph: dict, inventory: dict, census: dict, config: ReviewConfig) -> list[dict]:
    index = MemoryGraphIndex(graph)
    units: list[dict] = []
    covered_symbols: set[str] = set()
    units.extend(_entrypoint_flow_units(index, graph, config, covered_symbols))
    units.extend(_component_units(index, graph, config, covered_symbols))
    units.extend(_state_units(index, graph, config))
    units.extend(_trust_units(index, graph, config))
    units.extend(_config_build_units(index, graph, inventory, config))
    units.extend(_test_integrity_units(index, graph, config))
    units.extend(_orphan_units(index, graph, config, covered_symbols))
    units.extend(_cross_boundary_units(index, graph, config))
    units.extend(_global_invariant_units(graph, inventory, census, config))
    return _dedupe_units(units)


def _entrypoint_flow_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig, covered: set[str]) -> list[dict]:
    units = []
    for node in _nodes(graph, kinds=ENTRYPOINT_KINDS):
        node_id = str(node.get("id") or "")
        downstream = index.walk(node_id, direction="downstream", max_depth=config.units.high_risk_downstream_depth, edge_types=CALL_EDGE_TYPES | STATE_EDGE_TYPES | {"reads_env", "reads_config"}, max_nodes=config.units.max_unit_nodes)
        node_ids = _bounded_unique([node_id, *downstream], config.units.max_unit_nodes)
        covered.update(_production_nodes(index, node_ids))
        units.append(_unit(index, graph, "entrypoint_flow", f"flow:{node_id}", node_ids, ["public-entrypoint", *_risk_tags(index, node_ids)]))
    return units


def _component_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig, covered: set[str]) -> list[dict]:
    units = []
    for node in _nodes(graph, kinds=PRODUCTION_SYMBOL_KINDS):
        node_id = str(node.get("id") or "")
        if node_id in covered:
            continue
        downstream = index.walk(node_id, direction="downstream", max_depth=config.units.default_downstream_depth, edge_types=CALL_EDGE_TYPES | STATE_EDGE_TYPES | {"reads_env", "reads_config"}, max_nodes=config.units.max_unit_nodes)
        upstream = index.walk(node_id, direction="upstream", max_depth=config.units.default_upstream_depth, edge_types=CALL_EDGE_TYPES | {"defines"}, max_nodes=config.units.max_unit_nodes)
        node_ids = _bounded_unique([*upstream[:10], node_id, *downstream[:50]], config.units.max_unit_nodes)
        covered.add(node_id)
        units.append(_unit(index, graph, "component", f"component:{node_id}", node_ids, _risk_tags(index, node_ids)))
    return units


def _state_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig) -> list[dict]:
    units = []
    for node in _nodes(graph, kinds=STATE_KINDS):
        node_id = str(node.get("id") or "")
        upstream = index.walk(node_id, direction="upstream", max_depth=config.units.high_risk_upstream_depth, edge_types=STATE_EDGE_TYPES | CALL_EDGE_TYPES | {"references"}, max_nodes=config.units.max_unit_nodes)
        units.append(_unit(index, graph, "state", f"state:{node_id}", _bounded_unique([*upstream, node_id], config.units.max_unit_nodes), ["state", *_risk_tags(index, [node_id])]))
    return units


def _trust_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig) -> list[dict]:
    units = []
    for node in graph.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        attrs = {str(tag) for tag in node.get("attributes", []) if str(tag)}
        if not attrs & TRUST_TAGS:
            continue
        downstream = index.walk(node_id, direction="downstream", max_depth=config.units.high_risk_downstream_depth, edge_types=CALL_EDGE_TYPES | STATE_EDGE_TYPES | {"reads_env", "reads_config"}, max_nodes=config.units.max_unit_nodes)
        units.append(_unit(index, graph, "trust_boundary", f"trust:{node_id}", _bounded_unique([node_id, *downstream], config.units.max_unit_nodes), ["trust-boundary", *sorted(attrs)]))
    return units


def _config_build_units(index: MemoryGraphIndex, graph: dict, inventory: dict, config: ReviewConfig) -> list[dict]:
    del inventory, config
    units = []
    for node in _nodes(graph, kinds=CONFIG_KINDS):
        node_id = str(node.get("id") or "")
        units.append(_unit(index, graph, "config_build", f"config:{node_id}", [node_id], ["configuration"]))
    return units


def _test_integrity_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig) -> list[dict]:
    del config
    units = []
    for node in _nodes(graph, kinds={"test_file", "test_case"}):
        node_id = str(node.get("id") or "")
        units.append(_unit(index, graph, "test_integrity", f"test:{node_id}", [node_id], ["test-only", "test-integrity"]))
    return units


def _orphan_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig, covered: set[str]) -> list[dict]:
    del config
    units = []
    for node in _nodes(graph, kinds=PRODUCTION_SYMBOL_KINDS):
        node_id = str(node.get("id") or "")
        if node_id not in covered:
            units.append(_unit(index, graph, "orphan_component", f"orphan:{node_id}", [node_id], ["orphan", *_risk_tags(index, [node_id])]))
    return units


def _cross_boundary_units(index: MemoryGraphIndex, graph: dict, config: ReviewConfig) -> list[dict]:
    if not config.units.require_boundary_review:
        return []
    units = []
    for edge in graph.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if not source or not target:
            continue
        source_node = index.get_node(source) or {}
        target_node = index.get_node(target) or {}
        if _package(source_node.get("file")) == _package(target_node.get("file")):
            continue
        units.append(_unit(index, graph, "cross_boundary", f"boundary:{source}:{target}", [source, target], ["cross-boundary"]))
    return units


def _global_invariant_units(graph: dict, inventory: dict, census: dict, config: ReviewConfig) -> list[dict]:
    if not config.units.require_global_review:
        return []
    tags = [
        "authorization",
        "tenant-isolation",
        "transaction",
        "resource-lifecycle",
        "configuration",
        "test-strategy",
    ]
    return [
        {
            "unit_id": f"global:{tag}",
            "unit_type": "global_invariant",
            "review_pass": "global",
            "symbol": tag,
            "file": "",
            "line": 1,
            "span": {},
            "node_ids": [],
            "paths": [],
            "entrypoints": [],
            "affected_tests": [],
            "risk_tags": ["global-invariant", tag],
            "context_files": [],
            "repository_tests": [],
            "unresolved_edges": [],
            "coverage": {"inventory_files": len(inventory.get("files", []) or []), "packages": len(census.get("packages", []) or []), "graph_nodes": len(graph.get("nodes", []) or [])},
            "context": {"source": "full_repository_summary", "files": [], "path_summary": [f"Global invariant review: {tag}"], "query": {"result": {"summary": [tag], "nodes": [], "path_summary": [tag]}}},
        }
        for tag in tags
    ]


def _unit(index: MemoryGraphIndex, graph: dict, unit_type: str, raw_id: str, node_ids: list[str], risk_tags: list[str]) -> dict:
    node_ids = _bounded_unique(node_ids, 100)
    context_files = _context_files(index, node_ids)
    paths = _paths(index, node_ids)
    primary = index.get_node(node_ids[0]) if node_ids else {}
    unit_id = f"{unit_type}:{_hash(raw_id)}"
    return {
        "unit_id": unit_id,
        "unit_type": unit_type,
        "review_pass": "baseline",
        "node_ids": node_ids,
        "paths": paths,
        "entrypoints": [node_id for node_id in node_ids if (index.get_node(node_id) or {}).get("kind") in ENTRYPOINT_KINDS],
        "affected_tests": [node_id for node_id in node_ids if (index.get_node(node_id) or {}).get("kind") in {"test_file", "test_case"}],
        "risk_tags": sorted(dict.fromkeys(risk_tags or ["source"])),
        "unresolved_edges": _unresolved_for_nodes(graph, node_ids),
        "context_files": context_files,
        "file": str((primary or {}).get("file") or ""),
        "symbol": str((primary or {}).get("qualified_name") or (primary or {}).get("name") or unit_type),
        "line": int(((primary or {}).get("span") or {}).get("start_line") or 1) if isinstance((primary or {}).get("span"), dict) else 1,
        "span": (primary or {}).get("span") or {},
        "repository_tests": [],
        "context": _context(index, unit_id, unit_type, node_ids, paths, context_files),
    }


def _context(index: MemoryGraphIndex, unit_id: str, unit_type: str, node_ids: list[str], paths: list[list[str]], context_files: list[dict]) -> dict:
    nodes = []
    for node_id in node_ids:
        node = index.get_node(node_id) or {}
        span = node.get("span") if isinstance(node.get("span"), dict) else {}
        nodes.append({"id": node_id, "kind": node.get("kind"), "symbol": node.get("qualified_name") or node.get("name"), "file": node.get("file"), "line": span.get("start_line")})
    path_summary = [" -> ".join(path) for path in paths] or [unit_id]
    result = {"unit_id": unit_id, "unit_type": unit_type, "summary": path_summary[:12], "files": [item["path"] for item in context_files], "path_summary": path_summary, "nodes": nodes[:100], "edges": [], "callers": [], "callees": [], "impact": nodes[:100]}
    return {"source": "full_repository_evidence_graph", "query": {"query": unit_id, "command": ["codereview", "unit", "context"], "result": result, "attempts": []}, "files": result["files"], "path_summary": path_summary}


def _nodes(graph: dict, *, kinds: set[str]) -> list[dict]:
    return [node for node in graph.get("nodes", []) or [] if isinstance(node, dict) and node.get("kind") in kinds]


def _production_nodes(index: MemoryGraphIndex, node_ids: list[str]) -> set[str]:
    return {node_id for node_id in node_ids if (index.get_node(node_id) or {}).get("kind") in PRODUCTION_SYMBOL_KINDS}


def _risk_tags(index: MemoryGraphIndex, node_ids: list[str]) -> list[str]:
    tags: list[str] = []
    for node_id in node_ids:
        node = index.get_node(node_id)
        if node and isinstance(node.get("attributes"), list):
            tags.extend(str(tag) for tag in node.get("attributes", []) if str(tag))
    return sorted(dict.fromkeys(tags or ["source"]))


def _context_files(index: MemoryGraphIndex, node_ids: list[str]) -> list[dict]:
    ranges: dict[str, list[str]] = {}
    for node_id in node_ids:
        node = index.get_node(node_id) or {}
        file_path = str(node.get("file") or "")
        span = node.get("span") if isinstance(node.get("span"), dict) else {}
        start = int(span.get("start_line") or 1)
        end = int(span.get("end_line") or start)
        if file_path:
            ranges.setdefault(file_path, []).append(f"{start}-{end}")
    return [{"path": path, "ranges": sorted(set(values))[:20]} for path, values in sorted(ranges.items())]


def _paths(index: MemoryGraphIndex, node_ids: list[str]) -> list[list[str]]:
    labels = [_node_label(index, node_id) for node_id in node_ids[:12]]
    return [labels] if labels else []


def _node_label(index: MemoryGraphIndex, node_id: str) -> str:
    node = index.get_node(node_id) or {}
    return str(node.get("qualified_name") or node.get("name") or node_id)


def _unresolved_for_nodes(graph: dict, node_ids: list[str]) -> list[dict]:
    ids = set(node_ids)
    return [ref for ref in graph.get("unresolved_refs", []) if isinstance(ref, dict) and ref.get("source_node") in ids][:50]


def _dedupe_units(units: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for unit in units:
        key = str(unit.get("unit_id") or "")
        if key and key not in seen:
            seen.add(key)
            deduped.append(unit)
    return deduped


def _bounded_unique(values: list[str], limit: int) -> list[str]:
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _package(file_path: object) -> str:
    parts = Path(str(file_path or "")).parts
    return "/".join(parts[:2]) if len(parts) >= 2 else (parts[0] if parts else "")


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:12]
