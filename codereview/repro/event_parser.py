from __future__ import annotations

import json
import os
import stat
from pathlib import Path


MAX_EVENT_STREAM_BYTES = 2 * 1024 * 1024


def event_stream_text(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return ""
    if not stat.S_ISREG(mode) or _has_symlink_ancestor(path):
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                data = handle.read(MAX_EVENT_STREAM_BYTES)
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _has_symlink_ancestor(path: Path) -> bool:
    current = path.parent
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent

def command_mentioned(events_text: str, command: str) -> bool:
    command = str(command or "").strip()
    if not command:
        return False
    if command in events_text:
        return True
    first_token = command.split()[0] if command.split() else ""
    if first_token and first_token in events_text:
        return True
    for line in events_text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _json_contains(payload, command) or (first_token and _json_contains(payload, first_token)):
            return True
    return False


def _json_contains(value: object, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_json_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains(item, needle) for item in value)
    return needle in str(value)
