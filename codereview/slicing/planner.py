from __future__ import annotations

import hashlib
from pathlib import Path

from ..context_adapter import symbol_context
from ..config import ReviewConfig
from .risk_tags import risk_tags_for_symbol


_RISK_ORDER = [
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
_RISK_RANK = {tag: index for index, tag in enumerate(_RISK_ORDER)}


def build_slices_with_context(
    *,
    checkout: Path,
    run: Path,
    rough_symbols: list[dict],
    repository_tests: list[dict],
    config: ReviewConfig,
) -> list[dict]:
    slices = []
    tests_by_file = {str(item.get("file") or ""): item for item in repository_tests}
    for item in prioritize_rough_symbols(rough_symbols)[: config.max_slices]:
        file_path = str(item.get("file") or "")
        symbol = str(item.get("symbol") or "<module>")
        context = symbol_context(
            checkout,
            run,
            config,
            symbol,
            file_path,
            int(item.get("line") or 0),
            f"slice-{len(slices)}",
        )
        slice_id = hashlib.sha1(f"{file_path}:{symbol}:{item.get('line')}".encode("utf-8")).hexdigest()[:12]
        tags = risk_tags_for_symbol(item)
        slices.append(
            {
                "slice_id": f"slice_{slice_id}",
                "file": file_path,
                "symbol": symbol,
                "line": int(item.get("line") or 0),
                "span": item.get("span") or {},
                "risk_tags": tags,
                "repository_tests": [tests_by_file[file_path]] if file_path in tests_by_file else [],
                "context": context,
            }
        )
    return prioritize_slices(slices)[: config.max_slices]


def prioritize_rough_symbols(rough_symbols: list[dict]) -> list[dict]:
    return sorted(rough_symbols, key=_risk_key)


def prioritize_slices(slices: list[dict]) -> list[dict]:
    return sorted(slices, key=_risk_key)


def _risk_key(item: dict) -> tuple[int, str, int]:
    tags = item.get("risk_tags") if isinstance(item.get("risk_tags"), list) else risk_tags_for_symbol(item)
    line = int(item.get("line") or 0)
    return (min([_RISK_RANK.get(str(tag), 99) for tag in tags] or [99]), str(item.get("file") or ""), line)
