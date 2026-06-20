from __future__ import annotations

import shutil
from pathlib import Path

from ..utils.jsonl import write_json


def create_immutable_snapshot(checkout: Path, inventory: dict, run: Path) -> dict:
    snapshot_root = run / "workers" / "coordinator" / "snapshot"
    repo = snapshot_root / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in inventory.get("files", []):
        if not isinstance(item, dict) or item.get("scope") != "analyze":
            continue
        rel = str(item.get("path") or "")
        if not rel:
            continue
        source = checkout / rel
        target = repo / rel
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(rel)
    copied_assets = _copy_codereview_assets(checkout, repo)
    manifest = {
        "snapshot_repo": str(repo),
        "copied_files": copied,
        "copied_files_count": len(copied),
        "copied_codereview_assets": copied_assets,
        "source_checkout": str(checkout),
    }
    write_json(snapshot_root / "manifest.json", manifest)
    return manifest


def _copy_codereview_assets(checkout: Path, repo: Path) -> list[str]:
    source = checkout / ".codereview"
    if not source.is_dir():
        return []
    target = repo / ".codereview"
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ("config.json",):
        source_file = source / name
        if source_file.is_file():
            shutil.copy2(source_file, target / name)
            copied.append(f".codereview/{name}")
    for name in ("prompts", "schemas"):
        source_dir = source / name
        target_dir = target / name
        if source_dir.is_dir():
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.copytree(source_dir, target_dir)
            copied.append(f".codereview/{name}/")
    return copied
