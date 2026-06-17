from __future__ import annotations

import hashlib
from pathlib import Path

from ..codegraph_adapter import codegraph_symbol_context
from ..config import ReviewConfig
from .risk_tags import risk_tags_for_symbol


def build_slices_with_codegraph(
    *,
    checkout: Path,
    run: Path,
    rough_symbols: list[dict],
    affected_tests: list[dict],
    config: ReviewConfig,
) -> list[dict]:
    slices = []
    tests_by_file = {str(item.get("file") or ""): item for item in affected_tests}
    for item in rough_symbols:
        file_path = str(item.get("file") or "")
        symbol = str(item.get("symbol") or "<module>")
        graph = codegraph_symbol_context(checkout, run, config.codegraph, symbol, file_path, f"slice-{len(slices)}")
        slice_id = hashlib.sha1(f"{file_path}:{symbol}:{item.get('line')}".encode("utf-8")).hexdigest()[:12]
        tags = risk_tags_for_symbol(item)
        slices.append(
            {
                "slice_id": f"slice_{slice_id}",
                "file": file_path,
                "symbol": symbol,
                "line": int(item.get("line") or 0),
                "hunk": item.get("hunk") or {},
                "risk_tags": tags,
                "affected_tests": [tests_by_file[file_path]] if file_path in tests_by_file else [],
                "codegraph": graph,
            }
        )
    return prioritize_slices(slices)[: config.max_slices]


def prioritize_slices(slices: list[dict]) -> list[dict]:
    order = [
        "auth",
        "authorization",
        "public-entrypoint",
        "db-write",
        "transaction",
        "cache",
        "serialization",
        "api-contract",
        "filesystem",
        "network",
        "test-only",
        "isolated-helper",
    ]
    rank = {tag: index for index, tag in enumerate(order)}

    def key(item: dict) -> tuple[int, str]:
        tags = item.get("risk_tags") if isinstance(item.get("risk_tags"), list) else []
        return (min([rank.get(str(tag), 99) for tag in tags] or [99]), str(item.get("file") or ""))

    return sorted(slices, key=key)
