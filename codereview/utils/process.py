from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }


def run_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    check: bool = False,
) -> ProcessResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        result = ProcessResult(
            command=command,
            cwd=str(cwd),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        result = ProcessResult(
            command=command,
            cwd=str(cwd),
            returncode=124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
    except FileNotFoundError as exc:
        result = ProcessResult(
            command=command,
            cwd=str(cwd),
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if check and result.returncode != 0:
        raise RuntimeError(f"{command[0]} exited {result.returncode}: {(result.stderr or result.stdout)[-500:]}")
    return result
