from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class RepositorySnapshot:
    files: list[str]
    spans: list[dict]


class RepositorySnapshotError(RuntimeError):
    pass


def analyze_repository_snapshot(checkout: Path, inventory: dict) -> RepositorySnapshot:
    files = [
        str(item.get("path") or "")
        for item in inventory.get("files", [])
        if isinstance(item, dict) and item.get("scope") == "analyze" and str(item.get("path") or "")
    ]
    spans: list[dict] = []
    for file_path in files:
        path = checkout / file_path
        if not path.is_file():
            continue
        line_count = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        spans.append(
            {
                "file": file_path,
                "start": 1,
                "lines": line_count,
                "end": max(1, line_count),
                "kind": "repository",
            }
        )
    return RepositorySnapshot(files=files, spans=spans)
