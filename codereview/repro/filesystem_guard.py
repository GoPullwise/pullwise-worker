from __future__ import annotations

from pathlib import Path

from ..utils.paths import is_within


def guard_worker_result(worker: Path, result: dict) -> list[str]:
    violations = []
    files = result.get("files_written") if isinstance(result.get("files_written"), list) else []
    for item in files:
        path = (worker / str(item)).resolve(strict=False)
        if not is_within(path, worker):
            violations.append(f"file written outside worker directory: {item}")
    log_paths = result_log_paths(result)
    if log_paths:
        for log_path in log_paths:
            violations.extend(validate_log_path(worker, log_path))
    else:
        violations.append("log path missing")
    return violations


def result_log_paths(result: dict) -> list[str]:
    paths = []
    commands = result.get("commands_run")
    if isinstance(commands, list):
        for command in commands:
            if isinstance(command, dict) and command.get("log_path"):
                paths.append(str(command.get("log_path")))
    legacy = result.get("log_path") or result.get("logPath")
    if legacy:
        paths.append(str(legacy))
    return paths


def validate_log_path(worker: Path, log_path: str) -> list[str]:
    violations = []
    if log_path:
        resolved_log = (worker / log_path).resolve(strict=False)
        if not is_within(resolved_log, worker):
            violations.append(f"log path outside worker directory: {log_path}")
        elif not resolved_log.is_file():
            violations.append(f"log path missing: {log_path}")
    else:
        violations.append("log path missing")
    return violations
