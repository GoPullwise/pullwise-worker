from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..inventory.git_inventory import build_git_inventory


def capture_source_state(checkout: Path, *, include_untracked: bool = True) -> dict:
    inventory = build_git_inventory(checkout, include_untracked=include_untracked)
    return source_state_from_inventory(inventory)


def source_state_from_inventory(inventory: dict) -> dict:
    files = [
        {
            "path": item.get("path"),
            "content_hash": item.get("content_hash"),
            "scope": item.get("scope"),
        }
        for item in inventory.get("files", [])
        if isinstance(item, dict) and item.get("scope") == "analyze"
    ]
    manifest_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "manifest_hash": f"sha256:{manifest_hash}",
        "files": files,
        "summary": inventory.get("summary") or {},
    }


def source_state_changed(before: dict, after: dict) -> bool:
    return str(before.get("manifest_hash") or "") != str(after.get("manifest_hash") or "")
