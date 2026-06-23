from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .paths import ensure_dir


READ_TEXT_MAX_BYTES = 32 * 1024 * 1024


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def read_json(path: Path, default: object = None) -> object:
    if not path.is_file():
        return default
    try:
        text = read_text(path)
    except OSError:
        return default
    return json.loads(text)


def read_json_strict(path: Path) -> object:
    return json.loads(read_text(path))


def write_jsonl(path: Path, rows: list[object]) -> None:
    write_text(
        path,
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
    )


def read_jsonl(path: Path) -> list[object]:
    if not path.is_file():
        return []
    rows = []
    try:
        text = read_text(path)
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def read_text(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        size = os.fstat(fd).st_size
        if size > READ_TEXT_MAX_BYTES:
            raise OSError(f"refusing to read oversized JSON file: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(READ_TEXT_MAX_BYTES + 1)
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise
    if len(data) > READ_TEXT_MAX_BYTES:
        raise OSError(f"refusing to read oversized JSON file: {path}")
    return data.decode("utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
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
