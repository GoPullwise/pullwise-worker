from __future__ import annotations

import json
from pathlib import Path

from ..utils.jsonl import write_json, write_text
from ..utils.paths import ensure_dir
from ..utils.paths import safe_path_component


CONTEXT_NODE_ID_LIMIT = 80
CONTEXT_PATH_LIMIT = 12
CONTEXT_FILE_LIMIT = 50
CONTEXT_UNRESOLVED_LIMIT = 20


def unit_file_stem(unit_id: object) -> str:
    return safe_path_component(unit_id, default="review_unit", max_length=96)


def write_review_units(run: Path, units: list[dict]) -> None:
    units_dir = run / "artifacts" / "review-units"
    ensure_dir(units_dir)
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        stem = unit_file_stem(unit_id)
        write_json(units_dir / f"{stem}.json", unit)
        write_text(units_dir / f"{stem}.context.md", render_review_unit_context_pack(unit))


def render_review_unit_context_pack(unit: dict) -> str:
    context = unit.get("context") if isinstance(unit.get("context"), dict) else {}
    span = unit.get("span") if isinstance(unit.get("span"), dict) else {}
    context_files = unit.get("context_files") if isinstance(unit.get("context_files"), list) else []
    node_ids = unit.get("node_ids") if isinstance(unit.get("node_ids"), list) else []
    paths = unit.get("paths") if isinstance(unit.get("paths"), list) else []
    unresolved = unit.get("unresolved_edges") if isinstance(unit.get("unresolved_edges"), list) else []
    coverage = unit.get("coverage") if isinstance(unit.get("coverage"), dict) else {}
    scope = {
        "node_count": len(node_ids),
        "sample_node_ids": _sample_list(node_ids, CONTEXT_NODE_ID_LIMIT),
        "path_count": len(paths),
        "sample_paths": _sample_list(paths, CONTEXT_PATH_LIMIT),
        "context_file_count": len(context_files),
        "context_files": _sample_list(context_files, CONTEXT_FILE_LIMIT),
        "unresolved_edge_count": len(unresolved),
        "sample_unresolved_edges": _sample_list(unresolved, CONTEXT_UNRESOLVED_LIMIT),
        "coverage": coverage,
    }
    return "\n".join(
        [
            f"# Review Unit Context Pack: {unit.get('unit_id')}",
            "",
            f"Unit ID: `{unit.get('unit_id')}`",
            f"Unit type: `{unit.get('unit_type')}`",
            f"Review pass: `{unit.get('review_pass') or 'baseline'}`",
            f"File: `{unit.get('file')}`",
            f"Symbol: `{unit.get('symbol')}` at line {unit.get('line')}",
            f"Repository span: {span.get('start')}..{span.get('end')}",
            f"Risk tags: {', '.join(unit.get('risk_tags') or [])}",
            "",
            "## Unit Graph Scope",
            "```json",
            _compact_json(scope),
            "```",
            "",
            "## Repository Context Evidence",
            "```json",
            _compact_json(_compact_context(context)),
            "```",
            "",
            "## Repository Tests",
            "```json",
            _compact_json(unit.get("repository_tests") or []),
            "```",
        ]
    )


def _sample_list(values: list, limit: int) -> list:
    return values[: max(0, int(limit or 0))]


def _compact_context(context: dict) -> dict:
    query = context.get("query") if isinstance(context.get("query"), dict) else {}
    result = query.get("result") if isinstance(query.get("result"), dict) else {}
    compact_result = {
        **result,
        "files": _sample_list(result.get("files") if isinstance(result.get("files"), list) else [], CONTEXT_FILE_LIMIT),
        "path_summary": _sample_list(result.get("path_summary") if isinstance(result.get("path_summary"), list) else [], CONTEXT_PATH_LIMIT),
        "nodes": _sample_list(result.get("nodes") if isinstance(result.get("nodes"), list) else [], CONTEXT_NODE_ID_LIMIT),
        "impact": _sample_list(result.get("impact") if isinstance(result.get("impact"), list) else [], CONTEXT_NODE_ID_LIMIT),
    }
    return {
        **context,
        "files": _sample_list(context.get("files") if isinstance(context.get("files"), list) else [], CONTEXT_FILE_LIMIT),
        "path_summary": _sample_list(context.get("path_summary") if isinstance(context.get("path_summary"), list) else [], CONTEXT_PATH_LIMIT),
        "query": {**query, "result": compact_result},
    }


def _compact_json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return text[:12000]
