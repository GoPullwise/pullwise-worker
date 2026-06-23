from __future__ import annotations

import os
import stat
from pathlib import Path

from ..utils.paths import is_within


MAX_REPRO_LOG_BYTES = 1024 * 1024


def worker_log_path_error(worker: Path, log_path: str) -> tuple[Path | None, str]:
    resolved = (worker / log_path).resolve(strict=False)
    if not is_within(resolved, worker):
        return None, f"log path outside worker directory: {log_path}"
    try:
        mode = resolved.lstat().st_mode
    except OSError:
        return None, f"log path missing: {log_path}"
    if not stat.S_ISREG(mode):
        return None, f"log path missing: {log_path}"
    return resolved, ""


def read_worker_log_text(worker: Path, log_path: str) -> tuple[str, str]:
    resolved, error = worker_log_path_error(worker, log_path)
    if error or resolved is None:
        return "", error
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags)
    except OSError:
        return "", f"log path missing: {log_path}"
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_REPRO_LOG_BYTES)
    return data.decode("utf-8", errors="replace"), ""
