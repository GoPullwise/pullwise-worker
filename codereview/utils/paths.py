from __future__ import annotations

import os
import re
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


def safe_path_component(value: object, *, default: str = "item", max_length: int = 80) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = text.split("/")[-1] if "/" in text else text
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-")
    if not text or text in {".", ".."}:
        text = default
    return text[:max_length] or default
