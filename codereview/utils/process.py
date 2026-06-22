from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


_PROCESS_CANCEL_STATE = threading.local()


class ProcessCancelled(RuntimeError):
    pass


@dataclass
class ProcessResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    stdout_path: str = ""
    stderr_path: str = ""
    queue_wait_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "queueWaitMs": self.queue_wait_ms,
            "execDurationMs": self.duration_ms,
        }


def _tail_text(path: Path, limit: int = 65536) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(-limit, 2)
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def compact_process_output(result: object, *, limit: int = 700) -> str:
    parts = []
    for label in ("stderr", "stdout"):
        text = str(getattr(result, label, "") or "").strip()
        if text:
            parts.append(f"{label}: {text}")
    detail = "\n".join(parts).strip()
    if len(detail) > limit:
        detail = detail[-limit:].lstrip()
    return detail or "no stderr/stdout"


def set_process_cancel_event(event: object | None) -> None:
    _PROCESS_CANCEL_STATE.event = event


def clear_process_cancel_event() -> None:
    if hasattr(_PROCESS_CANCEL_STATE, "event"):
        delattr(_PROCESS_CANCEL_STATE, "event")


def process_cancel_event() -> object | None:
    return getattr(_PROCESS_CANCEL_STATE, "event", None)


def process_cancel_requested() -> bool:
    event = process_cancel_event()
    return bool(event is not None and getattr(event, "is_set", lambda: False)())


def raise_if_cancelled_callback_exception(exc: BaseException) -> None:
    if isinstance(exc, ProcessCancelled):
        raise exc
    if process_cancel_requested() or exc.__class__.__name__ == "WorkerJobCancelled":
        raise ProcessCancelled(str(exc) or "operation cancelled") from exc


def run_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    check: bool = False,
    log_dir: Path | None = None,
    queue_wait_ms: int = 0,
    stdin_text: str | None = None,
) -> ProcessResult:
    started = time.monotonic()
    cwd_key = hashlib.sha256(str(cwd.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:16]
    log_root = log_dir or (Path(tempfile.gettempdir()) / "pullwise-codereview-process-logs" / cwd_key)
    log_root.mkdir(parents=True, exist_ok=True)
    prefix = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in (command[0] if command else "process"))
    stamp = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
    stdout_path = log_root / f"{prefix}-{stamp}.stdout.log"
    stderr_path = log_root / f"{prefix}-{stamp}.stderr.log"
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            stdin_pipe = subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=stdin_pipe,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            if process_cancel_event() is None:
                try:
                    if stdin_text is not None:
                        process.communicate(stdin_text.encode("utf-8"), timeout=timeout)
                        returncode = process.returncode
                    else:
                        returncode = process.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    process.kill()
                    if stdin_text is not None:
                        process.communicate()
                        returncode = process.returncode
                    else:
                        returncode = process.wait()
                    timed_out = True
            else:
                if stdin_text is not None and process.stdin is not None:
                    try:
                        process.stdin.write(stdin_text.encode("utf-8"))
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                deadline = started + max(1, int(timeout or 600))
                timed_out = False
                cancelled = False
                while True:
                    returncode = process.poll()
                    if returncode is not None:
                        break
                    if process_cancel_requested():
                        cancelled = True
                        process.kill()
                        returncode = process.wait()
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        process.kill()
                        returncode = process.wait()
                        break
                    time.sleep(min(0.2, max(0.01, remaining)))
                if cancelled:
                    raise ProcessCancelled(f"{command[0] if command else 'process'} cancelled")
        result = ProcessResult(
            command=command,
            cwd=str(cwd),
            returncode=124 if timed_out else returncode,
            stdout=_tail_text(stdout_path),
            stderr=_tail_text(stderr_path),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            queue_wait_ms=max(0, int(queue_wait_ms or 0)),
        )
    except FileNotFoundError as exc:
        result = ProcessResult(
            command=command,
            cwd=str(cwd),
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            queue_wait_ms=max(0, int(queue_wait_ms or 0)),
        )
    if check and result.returncode != 0:
        raise RuntimeError(f"{command[0]} exited {result.returncode}: {(result.stderr or result.stdout)[-500:]}")
    return result
