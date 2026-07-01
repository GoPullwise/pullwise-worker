from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from codereview.utils.jsonl import read_json, write_json
from codereview.utils.paths import ensure_dir, is_within, safe_relative_path

CACHE_SCHEMA_VERSION = "pullwise-review-cache/1"


class PipelineCache:
    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = root.resolve(strict=False)
        self.enabled = enabled

    @classmethod
    def from_checkout(cls, checkout: Path) -> "PipelineCache":
        root = _default_cache_root(checkout)
        enabled = str(os.environ.get("PULLWISE_REVIEW_CACHE_DISABLE") or "").strip().lower() not in {"1", "true", "yes"}
        return cls(root, enabled=enabled)

    def key(self, *, engine: str, source_state: dict, mode: str, scan_mode: str, config: dict | None = None) -> str:
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "engine": engine,
            "sourceManifest": str((source_state or {}).get("manifest_hash") or ""),
            "mode": mode,
            "scanMode": scan_mode,
            "config": config or {},
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def read_json(self, key: str, name: str, default: Any = None) -> Any:
        if not self.enabled:
            return default
        path = self._path(key, name)
        if not _safe_regular_file(path, self.root):
            return default
        return read_json(path, default=default)

    def write_json(self, key: str, name: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(key, name)
        ensure_dir(path.parent)
        write_json(path, value)

    def _path(self, key: str, name: str) -> Path:
        clean_key = _safe_cache_key(key)
        clean_name = safe_relative_path(name)
        if not clean_name:
            raise ValueError("cache artifact name must be a safe relative path")
        path = self.root / clean_key / clean_name
        if not is_within(path, self.root):
            raise RuntimeError(f"refusing cache path outside cache root: {path}")
        return path


def _default_cache_root(checkout: Path) -> Path:
    configured = str(os.environ.get("PULLWISE_REVIEW_CACHE_ROOT") or "").strip()
    if configured:
        return Path(configured) / "v1"
    worker_work = str(os.environ.get("PULLWISE_WORKER_WORK_DIR") or "").strip()
    if worker_work:
        return Path(worker_work) / "review-cache" / "v1"
    for parent in checkout.resolve(strict=False).parents:
        if parent.name in {"checkouts", "checkout"}:
            return parent.parent / "review-cache" / "v1"
    return checkout / ".codereview" / "cache" / "v1"


def _safe_cache_key(value: object) -> str:
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:2] + "/" + digest[2:]


def _safe_regular_file(path: Path, root: Path) -> bool:
    try:
        if path.is_symlink() or not is_within(path, root):
            return False
        metadata = path.stat()
        return stat.S_ISREG(metadata.st_mode)
    except OSError:
        return False
