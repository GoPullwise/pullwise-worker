from __future__ import annotations

from pathlib import Path

from ..repro.event_parser import command_mentioned, event_stream_text


def verify_repro_events_and_paths(repro: dict) -> dict:
    result = repro.get("result") if isinstance(repro.get("result"), dict) else {}
    process = repro.get("process") if isinstance(repro.get("process"), dict) else {}
    events_path = Path(str(process.get("events_path") or ""))
    commands = result.get("commands_run") if isinstance(result.get("commands_run"), list) else []
    if not commands:
        return {"status": "passed", "reason": "no reproduced commands to verify", "events_path": str(events_path)}
    text = event_stream_text(events_path)
    if not text:
        return {"status": "rejected", "reason": "codex event stream is missing or empty", "events_path": str(events_path)}
    if not _has_command_event_marker(text):
        return {"status": "unverified", "reason": "codex event stream contains no recognizable command event markers", "events_path": str(events_path)}
    missing = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        cmd = str(command.get("cmd") or command.get("command") or "")
        if cmd and not command_mentioned(text, cmd):
            missing.append(cmd)
    if missing:
        return {
            "status": "rejected",
            "reason": f"result command was not found in codex event stream: {missing[0]}",
            "events_path": str(events_path),
        }
    return {"status": "passed", "reason": "commands are present in codex event stream", "events_path": str(events_path)}


def _has_command_event_marker(events_text: str) -> bool:
    markers = ("exec_command", "command", "cmd", "exit_code", "returncode", "tool_call")
    lowered = events_text.lower()
    return any(marker in lowered for marker in markers)
