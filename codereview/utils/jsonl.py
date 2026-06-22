from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_no_follow(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def read_json(path: Path, default: object = None) -> object:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_no_follow(
        path,
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
    )


def read_jsonl(path: Path) -> list[object]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_text_no_follow(path: Path, text: str) -> None:
    if path.parent.is_symlink():
        raise OSError(f"refusing to write through symlinked directory: {path.parent}")
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
        temp_path.replace(path)
    except Exception:
        try:
            if fd >= 0:
                os.close(fd)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
