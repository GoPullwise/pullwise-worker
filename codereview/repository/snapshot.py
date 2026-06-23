from __future__ import annotations

import os
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
        try:
            line_count = count_text_lines_no_follow(path)
        except OSError:
            continue
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


def count_text_lines_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        line_count = 0
        saw_data = False
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            saw_data = True
            line_count += chunk.count(b"\n")
        if saw_data:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                line_count += 1
        return line_count
