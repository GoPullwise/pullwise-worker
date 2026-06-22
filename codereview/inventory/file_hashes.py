from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _open_no_follow(path: Path, mode: str, **kwargs):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    return os.fdopen(fd, mode, **kwargs)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_no_follow(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def looks_binary(path: Path, *, sample_size: int = 4096) -> bool:
    try:
        with _open_no_follow(path, "rb") as handle:
            sample = handle.read(sample_size)
    except OSError:
        return True
    return b"\x00" in sample


def line_count(path: Path) -> int:
    try:
        with _open_no_follow(path, "r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0
