from __future__ import annotations

from pathlib import Path

from ..utils.jsonl import write_json


def write_slices(slices_dir: Path, slices: list[dict]) -> None:
    slices_dir.mkdir(parents=True, exist_ok=True)
    for item in slices:
        slice_id = str(item.get("slice_id") or "slice")
        write_json(slices_dir / f"{slice_id}.json", item)
        (slices_dir / f"{slice_id}.context.md").write_text(render_context_pack(item), encoding="utf-8")


def render_context_pack(item: dict) -> str:
    graph = item.get("codegraph") if isinstance(item.get("codegraph"), dict) else {}
    span = item.get("span") if isinstance(item.get("span"), dict) else {}
    return "\n".join(
        [
            f"# Context Pack: {item.get('slice_id')}",
            "",
            f"File: `{item.get('file')}`",
            f"Symbol: `{item.get('symbol')}` at line {item.get('line')}",
            f"Repository span: {span.get('start')}..{span.get('end')}",
            f"Risk tags: {', '.join(item.get('risk_tags') or [])}",
            "",
            "## CodeGraph Evidence",
            "```json",
            _compact_json(graph),
            "```",
            "",
            "## Repository Tests",
            "```json",
            _compact_json(item.get("repository_tests") or []),
            "```",
        ]
    )


def _compact_json(value: object) -> str:
    import json

    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return text[:12000]
