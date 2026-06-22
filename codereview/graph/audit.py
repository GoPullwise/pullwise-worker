from __future__ import annotations

import json
from pathlib import Path

from ..codex_runner import base_env, run_codex_turn
from ..config import ReviewConfig, auxiliary_codex_config
from ..inventory.git_inventory import analyzable_files
from ..utils.jsonl import write_json
from .validate import validate_graph


CRITICAL_UNRESOLVED_TAGS = {
    "authentication",
    "authorization",
    "secret-handling",
    "payment",
    "state-write",
    "transaction",
    "cache",
    "public-entrypoint",
    "job-handler",
}
REVIEW_RELEVANT_UNRESOLVED_TAGS = CRITICAL_UNRESOLVED_TAGS | {
    "validation",
    "database-write",
    "external-service",
    "filesystem",
}


def audit_graph(graph: dict, inventory: dict, checkout: Path) -> dict:
    analyzable = {path for item in analyzable_files(inventory) if (path := str(item.get("path") or ""))}
    mapped = set(str(path) for path in (graph.get("coverage") or {}).get("mapped_files", []) if str(path))
    review_files = set(analyzable)
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
    node_ids = {str(node.get("id") or "") for node in nodes}
    nodes_by_id = {str(node.get("id") or ""): node for node in nodes}
    files_with_file_nodes = {
        str(node.get("file") or "")
        for node in nodes
        if node.get("kind") in {"file", "test_file"} and str(node.get("file") or "")
    }
    dual_conflicts = graph.get("conflicts") if isinstance(graph.get("conflicts"), list) else []
    dangling = []
    for edge in graph.get("edges", []):
        if isinstance(edge, dict) and (str(edge.get("from") or "") not in node_ids or str(edge.get("to") or "") not in node_ids):
            dangling.append(edge.get("id") or edge)
    span_violations = validate_graph(graph, checkout)
    review_files_mapped = len(review_files & mapped)
    missing_mapped = sorted(analyzable - mapped)
    missing_file_nodes = sorted(analyzable - files_with_file_nodes)
    unresolved_classes = classify_unresolved_refs(graph.get("unresolved_refs") or [], nodes_by_id)
    critical_unresolved = unresolved_classes["critical_unresolved"]
    stale_evidence = sum(1 for item in span_violations if "stale" in str(item).lower())
    quality_errors = []
    if missing_mapped:
        quality_errors.append("not all analyzable files were mapped")
    if missing_file_nodes:
        quality_errors.append("not all analyzable files have a file node")
    if dangling:
        quality_errors.append("dangling graph edges exist")
    if span_violations:
        quality_errors.append("graph evidence validation failed")
    if critical_unresolved:
        quality_errors.append("critical unresolved graph references exist")
    quality_gate = "passed" if not quality_errors else "failed"
    return {
        "inventory_files": len(inventory.get("files", []) or []),
        "analyzable_files": len(analyzable),
        "mapped_files": len(mapped & analyzable),
        "missing_mapped_files": missing_mapped[:100],
        "missing_file_nodes": missing_file_nodes[:100],
        "review_scope": "full-repository",
        "review_files": len(review_files),
        "review_files_mapped": review_files_mapped,
        "nodes": len(graph.get("nodes", []) or []),
        "edges": len(graph.get("edges", []) or []),
        "unresolved_refs": len(graph.get("unresolved_refs", []) or []),
        "benign_unresolved": len(unresolved_classes["benign_unresolved"]),
        "review_relevant_unresolved": len(unresolved_classes["review_relevant_unresolved"]),
        "critical_unresolved": len(critical_unresolved),
        "unresolved_classification": {
            "benign": unresolved_classes["benign_unresolved"][:100],
            "review_relevant": unresolved_classes["review_relevant_unresolved"][:100],
            "critical": critical_unresolved[:100],
        },
        "dangling_edges": len(dangling),
        "stale_evidence": stale_evidence,
        "dual_map_conflicts": len(dual_conflicts),
        "dual_map_conflicts_resolved": 0 if dual_conflicts else len(dual_conflicts),
        "span_violations": span_violations[:100],
        "quality_errors": quality_errors,
        "quality_gate": quality_gate,
        "quality_gate_passed": quality_gate == "passed",
        "repairs": _repair_tasks(sorted(set(missing_mapped) | set(missing_file_nodes)), critical_unresolved),
    }


def run_agent_graph_audit(checkout: Path, run: Path, graph: dict, inventory: dict, deterministic_audit: dict, config: ReviewConfig) -> dict:
    prompt_file = checkout / ".codereview" / "prompts" / "graph-auditor.md"
    schema = checkout / ".codereview" / "schemas" / "graph-audit.schema.json"
    worker = run / "workers" / "graph-audit-0001"
    worker.mkdir(parents=True, exist_ok=True)
    if not prompt_file.is_file() or not schema.is_file():
        return {"status": "skipped", "reason": "graph auditor prompt or schema missing", "repairs": []}
    prompt = "\n\n".join(
        [
            prompt_file.read_text(encoding="utf-8"),
            "Deterministic audit JSON:",
            json.dumps(deterministic_audit, ensure_ascii=False, indent=2, sort_keys=True),
            "Full repository scan scope JSON:",
            json.dumps({"mode": "full-repository"}, ensure_ascii=False, indent=2, sort_keys=True)[:20000],
            "Inventory summary JSON:",
            json.dumps(inventory.get("summary") or {}, ensure_ascii=False, indent=2, sort_keys=True),
            "Graph summary JSON:",
            json.dumps(_graph_audit_summary(graph), ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )
    (worker / "prompt.md").write_text(prompt, encoding="utf-8")
    write_json(worker / "task.json", {"scan": {"mode": "full-repository"}, "deterministic_audit": deterministic_audit})
    output = worker / "result.json"
    events = worker / "events.jsonl"
    codex_config = auxiliary_codex_config(config)
    process = run_codex_turn(
        cd=checkout,
        prompt=prompt,
        output_schema=schema,
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.graph.graph_timeout_seconds,
        config=codex_config,
        env=base_env(checkout, codex_config),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    if process.returncode != 0 or not output.is_file():
        return {"status": "blocked", "reason": f"graph auditor exited {process.returncode}", "repairs": [], "process": process_payload}
    try:
        parsed = json.loads(output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"status": "blocked", "reason": f"graph auditor produced invalid JSON: {exc}", "repairs": [], "process": process_payload}
    if not isinstance(parsed, dict):
        return {"status": "blocked", "reason": "graph auditor produced non-object JSON", "repairs": [], "process": process_payload}
    parsed["status"] = "ok"
    parsed["process"] = process_payload
    return parsed


def _graph_audit_summary(graph: dict) -> dict:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    unresolved = graph.get("unresolved_refs") if isinstance(graph.get("unresolved_refs"), list) else []
    high_value_nodes = [
        {
            "id": node.get("id"),
            "kind": node.get("kind"),
            "qualified_name": node.get("qualified_name"),
            "file": node.get("file"),
            "span": node.get("span"),
            "attributes": node.get("attributes"),
        }
        for node in nodes
        if isinstance(node, dict) and (node.get("kind") in {"http_route", "job_handler", "test_file"} or node.get("attributes"))
    ][:200]
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "unresolved_count": len(unresolved),
        "high_value_nodes": high_value_nodes,
        "sample_unresolved_refs": unresolved[:100],
    }


def _repair_tasks(missing: list[str], critical_unresolved: list[dict] | None = None) -> list[dict]:
    repairs = []
    missing_set = set(missing)
    if missing:
        repairs.append({"type": "remap_files", "files": missing[:50], "reason": "Analyzable files were not covered by mapper output."})
    critical_files = []
    for item in critical_unresolved or []:
        path = str(item.get("source_file") or "")
        if path and path not in missing_set and path not in critical_files:
            critical_files.append(path)
    if critical_files:
        repairs.append(
            {
                "type": "remap_files",
                "files": critical_files[:50],
                "reason": "Critical unresolved graph references need remapping or link-resolution evidence.",
            }
        )
    return repairs


def classify_unresolved_refs(refs: object, nodes_by_id: dict[str, dict]) -> dict[str, list[dict]]:
    classes = {
        "benign_unresolved": [],
        "review_relevant_unresolved": [],
        "critical_unresolved": [],
    }
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict):
            continue
        source = nodes_by_id.get(str(ref.get("source_node") or "")) or {}
        tags = {str(tag) for tag in source.get("attributes", []) if str(tag)}
        kind = str(source.get("kind") or "")
        item = {
            "source_node": ref.get("source_node"),
            "source_file": ref.get("source_file"),
            "source_line": ref.get("source_line"),
            "reference_kind": ref.get("reference_kind"),
            "raw_reference": ref.get("raw_reference"),
            "reason": ref.get("reason"),
        }
        if kind in {"http_route", "rpc_endpoint", "cli_command", "job_handler", "event_handler", "public_api"}:
            item["classification_reason"] = "unresolved reference originates from an entrypoint"
            classes["critical_unresolved"].append(item)
        elif tags & CRITICAL_UNRESOLVED_TAGS:
            item["classification_reason"] = f"source node has critical risk tags: {sorted(tags & CRITICAL_UNRESOLVED_TAGS)}"
            classes["critical_unresolved"].append(item)
        elif tags & REVIEW_RELEVANT_UNRESOLVED_TAGS:
            item["classification_reason"] = f"source node has review-relevant risk tags: {sorted(tags & REVIEW_RELEVANT_UNRESOLVED_TAGS)}"
            classes["review_relevant_unresolved"].append(item)
        else:
            item["classification_reason"] = "unresolved reference is not on a known high-risk node"
            classes["benign_unresolved"].append(item)
    return classes
