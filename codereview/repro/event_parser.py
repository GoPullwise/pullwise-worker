from __future__ import annotations

import json
from pathlib import Path


def event_stream_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


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
