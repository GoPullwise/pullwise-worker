from __future__ import annotations

import re
from pathlib import Path


_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)|^\s*([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\("
)


def map_rough_symbols(checkout: Path, diff: object) -> list[dict]:
    hunks = getattr(diff, "hunks", []) or []
    by_file: dict[str, list[dict]] = {}
    for hunk in hunks:
        by_file.setdefault(str(hunk.get("file") or ""), []).append(hunk)
    results: list[dict] = []
    for file_path, file_hunks in by_file.items():
        path = checkout / file_path
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        discovered: dict[str, dict] = {}
        for hunk in file_hunks:
            start = max(1, int(hunk.get("new_start") or 1))
            end = max(start, int(hunk.get("new_end") or start))
            window_start = max(1, start - 80)
            best = None
            for index in range(min(len(lines), end), window_start - 1, -1):
                match = _SYMBOL_RE.search(lines[index - 1])
                if match:
                    best = {
                        "file": file_path,
                        "symbol": match.group(1) or match.group(2) or "<module>",
                        "line": index,
                        "hunk": hunk,
                    }
                    break
            best = best or {"file": file_path, "symbol": "<module>", "line": start, "hunk": hunk}
            key = f"{best['file']}:{best['symbol']}:{best['line']}"
            discovered[key] = best
        results.extend(discovered.values())
    return results
