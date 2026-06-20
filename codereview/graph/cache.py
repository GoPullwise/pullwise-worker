from __future__ import annotations

from pathlib import Path

from ..utils.jsonl import read_json, write_json
from .ids import short_hash


def graph_cache_key(file_info: dict, *, schema_version: str, prompt_version: str, language: str, profile_name: str = "default") -> str:
    return short_hash(
        {
            "file_hash": file_info.get("content_hash") or "",
            "schema_version": schema_version,
            "prompt_version": prompt_version,
            "language": language,
            "profile": profile_name,
        },
        length=32,
    )


def load_graph_cache(root: Path) -> dict:
    value = read_json(root / ".codereview" / "graph-cache" / "graph-state.json", default={})
    return value if isinstance(value, dict) else {}


def save_graph_cache(root: Path, cache: dict) -> None:
    write_json(root / ".codereview" / "graph-cache" / "graph-state.json", cache)
