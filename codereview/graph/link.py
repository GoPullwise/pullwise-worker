from __future__ import annotations

import json
from pathlib import Path

from ..codex_runner import base_env, run_codex_turn
from ..config import ReviewConfig, auxiliary_codex_config
from ..utils.jsonl import write_json, write_text
from .ids import stable_edge_id
from .merge import build_inline_indexes


def apply_cross_shard_linking(checkout: Path, run: Path, graph: dict, config: ReviewConfig) -> dict:
    linked = attach_link_candidates(graph)
    prompt_file = checkout / ".codereview" / "prompts" / "graph-linker.md"
    schema = checkout / ".codereview" / "schemas" / "graph-link.schema.json"
    if not prompt_file.is_file() or not schema.is_file():
        return linked
    nodes_by_id = {str(node.get("id") or ""): node for node in linked.get("nodes", []) if isinstance(node, dict)}
    edges = list(linked.get("edges") or [])
    remaining = []
    for index, ref in enumerate(linked.get("unresolved_refs", []) or []):
        if not isinstance(ref, dict):
            continue
        candidates = [str(item) for item in ref.get("candidate_targets", []) if str(item)]
        if not candidates:
            remaining.append(ref)
            continue
        result = _run_linker(checkout, run, prompt_file, schema, ref, candidates, nodes_by_id, config, index)
        if _valid_link_result(result, candidates):
            target = str(result["target"])
            source = str(ref.get("source_node") or "")
            edge = {
                "from": source,
                "to": target,
                "type": "calls" if ref.get("reference_kind") == "call" else "references",
                "status": "resolved",
                "evidence_kind": "agent_link_resolution",
                "confidence": "high",
                "evidence": [
                    {
                        "file": ref.get("source_file"),
                        "start_line": ref.get("source_line"),
                        "end_line": ref.get("source_line"),
                        "evidence_kind": "agent_link_resolution",
                    }
                ],
                "generated_by": {"worker_id": "graph-link-agent", "prompt_version": "graph-link-v3"},
                "verification": {"independent_mappers": 1, "audited": False, "linker_status": result.get("status")},
            }
            edge["id"] = stable_edge_id(source, target, str(edge["type"]), edge.get("evidence"))
            edges.append(edge)
        else:
            kept = dict(ref)
            kept["linker_result"] = result
            remaining.append(kept)
    updated = {**linked, "edges": edges, "unresolved_refs": remaining}
    updated["indexes"] = build_inline_indexes(updated)
    updated["manifest"] = {
        **(updated.get("manifest") or {}),
        "edge_count": len(edges),
        "unresolved_count": len(remaining),
    }
    return updated


def attach_link_candidates(graph: dict) -> dict:
    nodes_by_name: dict[str, list[str]] = {}
    for node in graph.get("nodes", []):
        if isinstance(node, dict):
            nodes_by_name.setdefault(str(node.get("name") or ""), []).append(str(node.get("id") or ""))
    remaining = []
    for ref in graph.get("unresolved_refs", []):
        if not isinstance(ref, dict):
            continue
        raw = str(ref.get("raw_reference") or "").split(".")[-1]
        candidates = [node_id for node_id in nodes_by_name.get(raw, []) if node_id]
        ref = dict(ref)
        ref["candidate_targets"] = candidates[:20]
        if candidates:
            ref["reason"] = "Candidate targets were generated from the symbol index and require linker proof."
        remaining.append(ref)
    updated = {**graph, "unresolved_refs": remaining}
    updated["indexes"] = build_inline_indexes(updated)
    updated["manifest"] = {
        **(updated.get("manifest") or {}),
        "edge_count": len(updated.get("edges") or []),
        "unresolved_count": len(remaining),
    }
    return updated


def apply_unique_link_results(graph: dict) -> dict:
    return attach_link_candidates(graph)


def _run_linker(
    checkout: Path,
    run: Path,
    prompt_file: Path,
    schema: Path,
    ref: dict,
    candidates: list[str],
    nodes_by_id: dict[str, dict],
    config: ReviewConfig,
    index: int,
) -> dict:
    worker = run / "workers" / f"graph-link-{index + 1:04d}"
    worker.mkdir(parents=True, exist_ok=True)
    payload = {
        "unresolved_reference": ref,
        "candidate_targets": [
            {
                "id": candidate,
                "kind": (nodes_by_id.get(candidate) or {}).get("kind"),
                "qualified_name": (nodes_by_id.get(candidate) or {}).get("qualified_name"),
                "file": (nodes_by_id.get(candidate) or {}).get("file"),
                "span": (nodes_by_id.get(candidate) or {}).get("span"),
                "signature": (nodes_by_id.get(candidate) or {}).get("signature"),
            }
            for candidate in candidates
        ],
    }
    prompt = "\n\n".join(
        [
            prompt_file.read_text(encoding="utf-8"),
            "Link task JSON:",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )
    write_json(worker / "task.json", payload)
    write_text(worker / "prompt.md", prompt)
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
        return {"status": "still_unresolved", "reason": f"linker exited {process.returncode}", "process": process_payload}
    try:
        parsed = json.loads(output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"status": "still_unresolved", "reason": f"invalid linker JSON: {exc}", "process": process_payload}
    if not isinstance(parsed, dict):
        return {"status": "still_unresolved", "reason": "non-object linker JSON", "process": process_payload}
    parsed["process"] = process_payload
    return parsed


def _valid_link_result(result: dict, candidates: list[str]) -> bool:
    if result.get("status") != "resolved":
        return False
    if result.get("target") not in candidates:
        return False
    return bool(str(result.get("reason") or "").strip())
