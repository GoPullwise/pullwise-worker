from __future__ import annotations

import os
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_within(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    return parent_resolved == child_resolved or parent_resolved in child_resolved.parents


def repo_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return ""


def safe_relative_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or "\x00" in text or text.startswith("/") or text.startswith("//"):
        return ""
    if len(text) >= 2 and text[1] == ":":
        return ""
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return ""
    if any(part.casefold() == ".git" for part in parts):
        return ""
    normalized = os.path.normpath("/".join(parts)).replace("\\", "/")
    return "" if normalized.startswith("../") else normalized
