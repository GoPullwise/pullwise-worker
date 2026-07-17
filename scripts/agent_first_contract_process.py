"""Credential-isolated, bounded subprocess execution for contract probes."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any


DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _probe_environment(scratch_root: Path) -> dict[str, str]:
    allowed = {
        "CI",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    scratch = str(scratch_root)
    for key in (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        env[key] = scratch
    env.update(
        {
            "CI": "1",
            "NPM_CONFIG_CACHE": scratch,
            "NPM_CONFIG_USERCONFIG": str(scratch_root / "npmrc"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def _force_stop_process(process: Any) -> None:
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return


def _windows_kill_process_tree(
    process: Any, *, taskkill: Path | None = None
) -> bool:
    if taskkill is None:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
    if not taskkill.is_file() and taskkill.name.lower() != "taskkill.exe":
        _force_stop_process(process)
        return False
    try:
        result = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _force_stop_process(process)
        return False
    _force_stop_process(process)
    return result.returncode == 0 and process.poll() is not None


def _kill_process_tree(process: Any) -> bool:
    if os.name == "nt":
        return _windows_kill_process_tree(process)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        _force_stop_process(process)
        return False
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


def _capture_output(
    pipe: Any,
    *,
    max_bytes: int,
    state: dict[str, Any],
    overflow: threading.Event,
) -> None:
    digest = hashlib.sha256()
    captured = bytearray()
    total = 0
    try:
        while True:
            chunk = pipe.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > max_bytes:
                overflow.set()
            remaining = max(0, max_bytes + 1 - len(captured))
            if remaining:
                captured.extend(chunk[:remaining])
    finally:
        state.update(
            output=bytes(captured),
            output_sha256=digest.hexdigest(),
            output_too_large=total > max_bytes,
        )


def run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    scratch_root: Path,
    timeout_seconds: int,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": _probe_environment(scratch_root),
        "shell": False,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(argv, **options)
    except OSError:
        return {"status": "start_failed", "returncode": None}
    assert process.stdout is not None
    capture: dict[str, Any] = {}
    overflow = threading.Event()
    reader = threading.Thread(
        target=_capture_output,
        kwargs={
            "pipe": process.stdout,
            "max_bytes": max_output_bytes,
            "state": capture,
            "overflow": overflow,
        },
        daemon=True,
    )
    reader.start()
    timed_out = False
    output_limited = False
    cleanup_confirmed = True
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            cleanup_confirmed = _kill_process_tree(process)
            break
        if overflow.wait(timeout=min(0.05, remaining)):
            output_limited = True
            cleanup_confirmed = _kill_process_tree(process)
            break
    reader.join(timeout=5)
    if reader.is_alive():
        cleanup_confirmed = _kill_process_tree(process) and cleanup_confirmed
        reader.join(timeout=5)
    if reader.is_alive():
        cleanup_confirmed = False
    process.stdout.close()
    status = "completed"
    if timed_out:
        status = "timeout" if cleanup_confirmed else "cleanup_unconfirmed"
    elif output_limited:
        status = "output_limit" if cleanup_confirmed else "cleanup_unconfirmed"
    elif not cleanup_confirmed:
        status = "cleanup_unconfirmed"
    return {
        "status": status,
        "returncode": process.returncode,
        "output": capture.get("output", b""),
        "output_sha256": capture.get("output_sha256", hashlib.sha256().hexdigest()),
        "output_too_large": output_limited or capture.get("output_too_large", False),
    }
