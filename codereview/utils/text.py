from __future__ import annotations

import os
from pathlib import Path


def read_bounded_text(path: Path, *, max_bytes: int, errors: str = "replace") -> str:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if path.is_symlink():
        raise OSError(f"refusing to follow symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        size = os.fstat(fd).st_size
        if size > max_bytes:
            raise OSError(f"refusing to read oversized text file: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(max_bytes + 1)
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise
    if len(data) > max_bytes:
        raise OSError(f"refusing to read oversized text file: {path}")
    return data.decode("utf-8", errors=errors)
