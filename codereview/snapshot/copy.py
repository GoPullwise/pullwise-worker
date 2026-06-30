from __future__ import annotations

import shutil
from pathlib import Path

from ..inventory.file_hashes import sha256_file
from ..utils.jsonl import write_json
from ..utils.paths import ensure_dir, is_within, safe_relative_path
from .integrity import source_state_from_inventory


def create_immutable_snapshot(checkout: Path, inventory: dict, run: Path) -> dict:
    snapshot_root = run / "workers" / "coordinator" / "snapshot"
    repo = snapshot_root / "repo"
    if repo.is_symlink():
        repo.unlink()
    elif repo.exists():
        shutil.rmtree(repo)
    ensure_dir(repo)
    copied: list[str] = []
    copied_hashes: dict[str, str] = {}
    missing: list[str] = []
    hash_mismatches: list[str] = []
    for item in inventory.get("files", []):
        if not isinstance(item, dict) or item.get("scope") != "analyze":
            continue
        rel = safe_relative_path(item.get("path"))
        if not rel:
            missing.append(str(item.get("path") or ""))
            continue
        source = _safe_regular_checkout_file(checkout, rel)
        if source is None:
            missing.append(rel)
            continue
        target = repo / rel
        ensure_dir(target.parent)
        shutil.copy2(source, target)
        expected_hash = str(item.get("content_hash") or "")
        actual_hash = sha256_file(target)
        if not expected_hash or actual_hash != expected_hash:
            hash_mismatches.append(f"{rel} expected {expected_hash or 'missing'} got {actual_hash or 'missing'}")
            continue
        copied.append(rel)
        copied_hashes[rel] = actual_hash
    if missing:
        sample = ", ".join(missing[:20])
        more = f" and {len(missing) - 20} more" if len(missing) > 20 else ""
        raise RuntimeError(f"immutable snapshot missing analyzable inventory files: {sample}{more}")
    if hash_mismatches:
        sample = "; ".join(hash_mismatches[:10])
        more = f" and {len(hash_mismatches) - 10} more" if len(hash_mismatches) > 10 else ""
        raise RuntimeError(f"immutable snapshot inventory hash mismatch: {sample}{more}")
    copied_assets = _copy_codereview_assets(checkout, repo)
    source_state = source_state_from_inventory(inventory)
    manifest = {
        "snapshot_repo": str(repo),
        "copied_files": copied,
        "copied_files_count": len(copied),
        "copied_file_hashes": copied_hashes,
        "inventory_manifest_hash": source_state.get("manifest_hash") or "",
        "copied_codereview_assets": copied_assets,
        "source_checkout": str(checkout),
    }
    write_json(snapshot_root / "manifest.json", manifest)
    return manifest


def _copy_codereview_assets(checkout: Path, repo: Path) -> list[str]:
    source = _safe_checkout_dir(checkout, ".codereview")
    if source is None:
        return []
    target = repo / ".codereview"
    ensure_dir(target)
    copied: list[str] = []
    source_file = _safe_regular_checkout_file(checkout, ".codereview/config.json")
    if source_file is not None:
        shutil.copy2(source_file, target / "config.json")
        copied.append(".codereview/config.json")
    for name in ("prompts", "schemas"):
        rel = f".codereview/{name}"
        source_dir = _safe_checkout_dir(checkout, rel)
        target_dir = target / name
        if source_dir is None:
            continue
        if target_dir.is_symlink():
            target_dir.unlink()
        elif target_dir.exists():
            shutil.rmtree(target_dir)
        _copy_regular_tree(source_dir, target_dir)
        copied.append(f"{rel}/")
    return copied


def _copy_regular_tree(source: Path, target: Path) -> None:
    ensure_dir(target)
    try:
        source_root = source.resolve(strict=True)
    except OSError:
        return
    for path in source.rglob("*"):
        rel = safe_relative_path(path.relative_to(source).as_posix())
        if not rel or _path_has_symlink_component(source, rel):
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if not is_within(resolved, source_root):
            continue
        destination = target / rel
        if path.is_dir():
            ensure_dir(destination)
            continue
        if not path.is_file():
            continue
        ensure_dir(destination.parent)
        shutil.copy2(path, destination)


def _safe_regular_checkout_file(checkout: Path, rel: object) -> Path | None:
    path = _safe_checkout_path(checkout, rel)
    if path is None or path.is_symlink() or not path.is_file():
        return None
    return path


def _safe_checkout_dir(checkout: Path, rel: object) -> Path | None:
    path = _safe_checkout_path(checkout, rel)
    if path is None or path.is_symlink() or not path.is_dir():
        return None
    return path


def _safe_checkout_path(checkout: Path, rel: object) -> Path | None:
    safe = safe_relative_path(rel)
    if not safe or _path_has_symlink_component(checkout, safe):
        return None
    path = checkout / safe
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not is_within(resolved, checkout):
        return None
    return path


def _path_has_symlink_component(root: Path, rel: object) -> bool:
    safe = safe_relative_path(rel)
    if not safe:
        return False
    current = root
    for part in Path(safe).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False