from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..utils.process import run_process


@dataclass
class RepositorySnapshot:
    files: list[str]
    spans: list[dict]


class RepositorySnapshotError(RuntimeError):
    pass


def analyze_repository_snapshot(checkout: Path, head_ref: str) -> RepositorySnapshot:
    names = run_process(["git", "ls-tree", "-r", "--name-only", head_ref], cwd=checkout, timeout=120)
    if names.returncode != 0:
        raise RepositorySnapshotError(f"git ls-tree failed: {(names.stderr or names.stdout)[-500:]}")
    files = [line.strip().replace("\\", "/") for line in names.stdout.splitlines() if line.strip()]
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
