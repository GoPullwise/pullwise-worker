from __future__ import annotations

import json
from pathlib import Path

from .logs import read_worker_log_text, worker_log_path_error
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
        return {"status": "rejected", "reason": "codex event stream contains no recognizable command event markers", "events_path": str(events_path)}
    parsed_events = _parse_jsonl_events(text)
    missing = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        cmd = str(command.get("cmd") or command.get("command") or "")
        if cmd and not command_mentioned(text, cmd):
            missing.append(f"command not found: {cmd}")
            continue
        cwd = str(command.get("cwd") or "").strip()
        if cwd and not _value_present(parsed_events, text, cwd):
            missing.append(f"command cwd not found for {cmd or '<unknown command>'}: {cwd}")
        if "exit_code" in command:
            exit_code = str(command.get("exit_code"))
            if not _value_present(parsed_events, text, exit_code):
                missing.append(f"command exit code not found for {cmd or '<unknown command>'}: {exit_code}")
        log_path = str(command.get("log_path") or "").strip()
        if log_path and not _value_present(parsed_events, text, log_path):
            missing.append(f"command log path not found for {cmd or '<unknown command>'}: {log_path}")
    proof = result.get("proof") if isinstance(result.get("proof"), dict) else {}
    log_excerpt = str(proof.get("log_excerpt") or "").strip()
    if log_excerpt and not _snippet_present(parsed_events, text, log_excerpt):
        missing.append("proof log_excerpt not found in codex event stream")
    actual = str(proof.get("actual") or "").strip()
    if actual and actual != log_excerpt and not _snippet_present(parsed_events, text, actual):
        missing.append("proof actual output not found in codex event stream")
    log_mismatch = _log_excerpt_mismatch(repro, commands, log_excerpt)
    if log_mismatch:
        missing.append(log_mismatch)
    if missing:
        return {
            "status": "rejected",
            "reason": f"result command was not verified in codex event stream: {missing[0]}",
            "events_path": str(events_path),
        }
    return {"status": "passed", "reason": "commands are present in codex event stream", "events_path": str(events_path)}


def _has_command_event_marker(events_text: str) -> bool:
    markers = ("exec_command", "command", "cmd", "exit_code", "returncode", "tool_call")
    lowered = events_text.lower()
    return any(marker in lowered for marker in markers)


def _parse_jsonl_events(events_text: str) -> list[object]:
    events: list[object] = []
    for line in events_text.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _value_present(events: list[object], events_text: str, needle: str) -> bool:
    if not needle:
        return False
    if needle in events_text:
        return True
    return any(_json_contains(event, needle) for event in events)


def _json_contains(value: object, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_json_contains(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains(item, needle) for item in value)
    return needle == str(value) or needle in str(value)


def _snippet_present(events: list[object], events_text: str, snippet: str) -> bool:
    snippet = " ".join(str(snippet or "").split())
    if not snippet:
        return False
    normalized = " ".join(events_text.split())
    if snippet[:500] in normalized:
        return True
    return any(_json_contains(event, snippet[:500]) for event in events)


def _log_excerpt_mismatch(repro: dict, commands: list[object], log_excerpt: str) -> str:
    if not log_excerpt:
        return ""
    worker_text = str(repro.get("worker") or "")
    if not worker_text:
        return ""
    worker = Path(worker_text).resolve(strict=False)
    for command in commands:
        if not isinstance(command, dict):
            continue
        log_path = str(command.get("log_path") or "").strip()
        if not log_path:
            continue
        _resolved, path_error = worker_log_path_error(worker, log_path)
        if path_error:
            return path_error.replace("log path outside worker directory", "command log path outside worker").replace(
                "log path missing",
                "command log path missing",
            )
        text, read_error = read_worker_log_text(worker, log_path)
        if read_error:
            return read_error.replace("log path missing", "command log path missing")
        if log_excerpt[:500] not in text and text[:500] not in log_excerpt:
            return f"proof log_excerpt is not supported by command log: {log_path}"
    return ""
