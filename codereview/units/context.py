from __future__ import annotations

import json
from pathlib import Path

from ..utils.jsonl import write_json, write_text
from ..utils.paths import safe_path_component


def unit_file_stem(unit_id: object) -> str:
    return safe_path_component(unit_id, default="review_unit", max_length=96)


def write_review_units(run: Path, units: list[dict]) -> None:
    units_dir = run / "artifacts" / "review-units"
    units_dir.mkdir(parents=True, exist_ok=True)
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
            _compact_json(
                {
                    "node_ids": node_ids,
                    "paths": paths,
                    "context_files": context_files,
                    "unresolved_edges": unresolved,
                    "coverage": coverage,
                }
            ),
            "```",
            "",
            "## Repository Context Evidence",
            "```json",
            _compact_json(context),
            "```",
            "",
            "## Repository Tests",
            "```json",
            _compact_json(unit.get("repository_tests") or []),
            "```",
        ]
    )


def _compact_json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return text[:12000]
