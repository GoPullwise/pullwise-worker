from __future__ import annotations

import os
import re
from pathlib import Path


_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)|^\s*([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\("
)


def map_repository_symbols(checkout: Path, snapshot: object) -> list[dict]:
    spans = getattr(snapshot, "spans", []) or []
    by_file: dict[str, list[dict]] = {}
    for span in spans:
        by_file.setdefault(str(span.get("file") or ""), []).append(span)
    results: list[dict] = []
    for file_path, file_spans in by_file.items():
        path = checkout / file_path
        if not path.is_file():
            continue
        try:
            symbols = _map_file_symbols(file_path, file_spans, iter_text_lines_no_follow(path))
        except OSError:
            continue
        results.extend(symbols.values())
    return results


def iter_text_lines_no_follow(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            yield line.rstrip("\r\n")


def _map_file_symbols(file_path: str, file_spans: list[dict], lines) -> dict[str, dict]:
    repository_span = file_spans[0] if file_spans else {}
    discovered: dict[str, dict] = {}
    for index, line in enumerate(lines, start=1):
        match = _SYMBOL_RE.search(line)
        if not match:
            continue
        symbol = match.group(1) or match.group(2) or "<module>"
        item = {
            "file": file_path,
            "symbol": symbol,
            "line": index,
            "span": {
                **repository_span,
                "start": index,
                "lines": 1,
                "end": index,
                "kind": "repository",
            },
        }
        discovered[f"{file_path}:{symbol}:{index}"] = item
    if not discovered:
        item = {
            "file": file_path,
            "symbol": "<module>",
            "line": 1,
            "span": {
                **repository_span,
                "start": 1,
                "lines": 1,
                "end": 1,
                "kind": "repository",
            },
        }
        discovered[f"{file_path}:<module>:1"] = item
    return discovered
