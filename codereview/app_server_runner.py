from __future__ import annotations

import atexit
import errno
import json
import os
import subprocess
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CodexConfig
from .utils.jsonl import write_text
from .utils.paths import ensure_dir
from .utils.process import ProcessCancelled, ProcessResult, process_cancel_requested

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - msvcrt is unavailable off Windows.
    _msvcrt = None

@dataclass
class AppServerRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None


@dataclass
class AppServerTurn:
    thread_id: str
    started_at: float = field(default_factory=time.monotonic)
    completed: threading.Event = field(default_factory=threading.Event)
    turn_id: str = ""
    status: str = ""
    error: str = ""
    assistant_messages: list[str] = field(default_factory=list)
    deltas: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


_APP_SERVER_CLIENTS_LOCK = threading.Lock()
_APP_SERVER_CLIENTS: dict[tuple[str, str, str, str, str], "CodexAppServerClient"] = {}
_APP_SERVER_LOCK_FILE = ".pullwise-app-server.lock"
MAX_STORED_TURN_EVENTS = 2000
MAX_STORED_COMMAND_OUTPUT_CHARS = 32_000
MAX_STORED_AGENT_MESSAGE_CHARS = 64_000


def app_server_key(command: str, env: dict[str, str] | None) -> tuple[str, str, str, str, str]:
    source = env or {}
    return (
        str(command or "codex"),
        str(source.get("HOME") or source.get("USERPROFILE") or ""),
        str(source.get("CODEX_HOME") or ""),
        str(source.get("CODEX_SQLITE_HOME") or ""),
        str(source.get("PATH") or ""),
    )


_LOCK_BUSY_ERRNOS = {
    errno.EACCES,
    getattr(errno, "EAGAIN", errno.EACCES),
    getattr(errno, "EWOULDBLOCK", errno.EACCES),
    getattr(errno, "EDEADLK", errno.EACCES),
}
_APP_SERVER_HELD_LOCKS_LOCK = threading.Lock()
_APP_SERVER_HELD_LOCKS: set[Path] = set()


class AppServerStateLock:
    def __init__(self, path: Path | None, *, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = max(0.0, float(timeout_seconds or 0))
        self._handle = None
        self._lock_key: Path | None = None

    def acquire(self) -> None:
        if self.path is None:
            return
        if not app_server_state_lock_supported():
            raise RuntimeError(
                "codex app-server state locking is not available on this platform; "
                "refusing to start shared app-server without a lock"
            )
        ensure_dir(self.path.parent)
        handle = _open_lock_file(self.path)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            locked = False
            lock_key: Path | None = None
            try:
                _acquire_handle_lock(handle)
                locked = True
                lock_key = _register_process_lock(self.path)
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()} started_at={int(time.time())}\n")
                handle.flush()
                self._handle = handle
                self._lock_key = lock_key
                return
            except BlockingIOError as exc:
                if locked:
                    _release_handle_lock_quietly(handle)
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeError(f"deferred: codex is running; app-server state lock is busy at {self.path}") from exc
                time.sleep(0.25)
            except Exception:
                if lock_key is not None:
                    _unregister_process_lock(lock_key)
                if locked:
                    _release_handle_lock_quietly(handle)
                handle.close()
                raise

    def release(self) -> None:
        handle = self._handle
        lock_key = self._lock_key
        self._handle = None
        self._lock_key = None
        if handle is None:
            if lock_key is not None:
                _unregister_process_lock(lock_key)
            return
        try:
            _release_handle_lock(handle)
        finally:
            if lock_key is not None:
                _unregister_process_lock(lock_key)
            handle.close()


def app_server_state_lock_supported() -> bool:
    return _fcntl is not None or _msvcrt is not None


def _open_lock_file(path: Path):
    if path.is_symlink():
        raise OSError(f"refusing to follow symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"refusing to lock non-regular file: {path}")
        return os.fdopen(fd, "a+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _acquire_handle_lock(handle) -> None:
    if _fcntl is not None:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if _lock_error_is_busy(exc):
                raise BlockingIOError(getattr(exc, "errno", errno.EAGAIN), "app-server state lock is busy") from exc
            raise
        return
    if _msvcrt is not None:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _lock_error_is_busy(exc):
                raise BlockingIOError(getattr(exc, "errno", errno.EACCES), "app-server state lock is busy") from exc
            raise
        return
    raise RuntimeError("codex app-server state locking is not available on this platform")


def _release_handle_lock(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        return


def _release_handle_lock_quietly(handle) -> None:
    try:
        _release_handle_lock(handle)
    except OSError:
        pass


def _lock_error_is_busy(exc: OSError) -> bool:
    return getattr(exc, "errno", None) in _LOCK_BUSY_ERRNOS or getattr(exc, "winerror", None) == 33


def _lock_identity(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _register_process_lock(path: Path) -> Path:
    key = _lock_identity(path)
    with _APP_SERVER_HELD_LOCKS_LOCK:
        if key in _APP_SERVER_HELD_LOCKS:
            raise BlockingIOError(errno.EAGAIN, f"app-server state lock is busy at {path}")
        _APP_SERVER_HELD_LOCKS.add(key)
    return key


def _unregister_process_lock(key: Path) -> None:
    with _APP_SERVER_HELD_LOCKS_LOCK:
        _APP_SERVER_HELD_LOCKS.discard(key)

def prepare_app_server_state(env: dict[str, str] | None) -> None:
    source = env or {}
    for key in (
        "HOME",
        "USERPROFILE",
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        value = str(source.get(key) or "").strip()
        if value:
            ensure_dir(Path(value))
    codex_home = str(source.get("CODEX_HOME") or "").strip()
    if not codex_home:
        return
    config_path = Path(codex_home) / "config.toml"
    if config_path.is_symlink() or not config_path.exists():
        write_text(config_path, "")


def app_server_state_lock_path(env: dict[str, str] | None) -> Path | None:
    source = env or {}
    for key in ("CODEX_HOME", "CODEX_SQLITE_HOME", "HOME", "USERPROFILE"):
        value = str(source.get(key) or "").strip()
        if value:
            return Path(value) / _APP_SERVER_LOCK_FILE
    return None


def app_server_process_cwd(env: dict[str, str] | None, fallback: Path) -> Path:
    source = env or {}
    for key in ("HOME", "CODEX_HOME", "USERPROFILE"):
        value = str(source.get(key) or "").strip()
        if not value:
            continue
        path = Path(value)
        if path.is_dir():
            return path
    return fallback

def app_server_lock_timeout_seconds(env: dict[str, str] | None) -> int:
    return parse_non_negative_int((env or {}).get("PULLWISE_CODEX_APP_SERVER_LOCK_TIMEOUT_SECONDS"), 60)


def get_codex_app_server_client(command: str, env: dict[str, str] | None, cwd: Path) -> "CodexAppServerClient":
    key = app_server_key(command, env)
    with _APP_SERVER_CLIENTS_LOCK:
        client = _APP_SERVER_CLIENTS.get(key)
        if client is None or client.is_closed():
            client = CodexAppServerClient(command=command, env=env, cwd=app_server_process_cwd(env, cwd))
            _APP_SERVER_CLIENTS[key] = client
    client.ensure_started()
    return client


def run_codex_app_server_turn(
    *,
    cd: Path,
    prompt: str,
    output_schema: Path,
    output_file: Path,
    sandbox: str,
    timeout_seconds: int,
    config: CodexConfig,
    env: dict[str, str] | None = None,
    events_file: Path | None = None,
) -> ProcessResult:
    started = time.monotonic()
    command = config.command or "codex"
    process_command = [command, "app-server", "turn/start"]
    client: CodexAppServerClient | None = None
    try:
        schema = json.loads(output_schema.read_text(encoding="utf-8")) if output_schema.is_file() else None
    except json.JSONDecodeError as exc:
        return ProcessResult(process_command, str(cd), 2, "", f"invalid output schema JSON: {exc}", 0)
    for attempt in range(2):
        try:
            client = get_codex_app_server_client(command, env, cd)
            turn = client.run_turn(
                cwd=cd,
                prompt=prompt,
                output_schema=schema,
                sandbox=sandbox,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
                timeout_seconds=timeout_seconds,
            )
            break
        except ProcessCancelled:
            raise
        except Exception as exc:
            if client is not None and app_server_error_requires_restart(exc):
                client.close(str(exc))
            if attempt == 0 and app_server_safe_to_retry(exc):
                time.sleep(app_server_retry_delay_seconds(attempt))
                continue
            return ProcessResult(
                process_command,
                str(cd),
                2,
                "",
                f"codex app-server turn failed: {exc}",
                int((time.monotonic() - started) * 1000),
            )
    else:
        return ProcessResult(process_command, str(cd), 2, "", "codex app-server turn failed without result", 0)
    duration_ms = int((time.monotonic() - started) * 1000)
    events_text = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in turn.events)
    if events_file is not None:
        write_text(events_file, events_text)
    if turn.error:
        error_lower = turn.error.lower()
        auth_error = any(
            marker in error_lower
            for marker in (
                "unauthorized",
                "failed to refresh token",
                "access token could not be refreshed",
                "refresh token was already used",
                "not authenticated",
                "authentication required",
                "login required",
                "please log out and sign in again",
            )
        )
        if client is not None and auth_error:
            client.close(f"codex app-server terminal auth error: {turn.error}")
        return ProcessResult(process_command, str(cd), 1, events_text[-65536:], turn.error, duration_ms, stdout_path=str(events_file or ""))
    text = final_assistant_text(turn)
    if not text:
        return ProcessResult(process_command, str(cd), 1, events_text[-65536:], "codex app-server turn completed without assistant output", duration_ms, stdout_path=str(events_file or ""))
    write_text(output_file, text)
    return ProcessResult(process_command, str(cd), 0, events_text[-65536:], "", duration_ms, stdout_path=str(events_file or ""))


def stored_app_server_event(message: dict) -> dict | None:
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method in {"turn/started", "turn/completed"}:
        return message
    if method != "item/completed":
        return None
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_type = item.get("type")
    if item_type == "commandExecution":
        stored_item = dict(item)
        stored_item["aggregatedOutput"] = str(stored_item.get("aggregatedOutput") or "")[-MAX_STORED_COMMAND_OUTPUT_CHARS:]
    elif item_type == "agentMessage":
        stored_item = {"type": "agentMessage", "text": str(item.get("text") or "")[-MAX_STORED_AGENT_MESSAGE_CHARS:]}
    else:
        return None
    stored_params = {
        "threadId": params.get("threadId"),
        "turnId": params.get("turnId"),
        "item": stored_item,
    }
    return {"method": method, "params": stored_params}

def final_assistant_text(turn: AppServerTurn) -> str:
    for text in reversed(turn.assistant_messages):
        if text.strip():
            return text
    return "".join(turn.deltas).strip()


class CodexAppServerClient:
    def __init__(self, *, command: str, env: dict[str, str] | None, cwd: Path) -> None:
        self.command = command or "codex"
        self.env = env
        self.cwd = cwd
        self.process: subprocess.Popen[str] | None = None
        self._start_lock = threading.Lock()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, AppServerRequest] = {}
        self._turns: dict[str, AppServerTurn] = {}
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self._started_at = 0.0
        self._completed_turns = 0
        self._ready = False
        self._closed = False
        self._state_lock: AppServerStateLock | None = None
        self._terminal_error = ""

    def ensure_started(self) -> None:
        with self._start_lock:
            if self._ready and self.is_alive() and not self.should_recycle():
                return
            self._start_locked()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self._closed

    def is_closed(self) -> bool:
        return self._closed

    def should_recycle(self) -> bool:
        with self._lock:
            if self._turns:
                return False
            started_at = self._started_at
            completed_turns = self._completed_turns
        max_age = app_server_max_age_seconds(self.env)
        if max_age and started_at and time.monotonic() - started_at >= max_age:
            return True
        max_turns = app_server_max_turns(self.env)
        return bool(max_turns and completed_turns >= max_turns)

    def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        output_schema: object,
        sandbox: str,
        model: str,
        reasoning_effort: str,
        timeout_seconds: int,
    ) -> AppServerTurn:
        self.ensure_started()
        control_timeout = app_server_control_timeout_seconds(timeout_seconds)
        if process_cancel_requested():
            raise ProcessCancelled("codex app-server turn cancelled")
        thread_result = self.request(
            "thread/start",
            {
                "approvalPolicy": "never",
                "cwd": str(cwd),
                "ephemeral": True,
                "model": model or None,
            },
            timeout_seconds=control_timeout,
        )
        thread_id = str((((thread_result or {}).get("thread") or {}).get("id")) or "")
        if not thread_id:
            thread_id = str((thread_result or {}).get("threadId") or "")
        if not thread_id:
            raise RuntimeError(f"thread/start did not return a thread id: {thread_result!r}")
        if process_cancel_requested():
            raise ProcessCancelled("codex app-server turn cancelled")
        turn = AppServerTurn(thread_id=thread_id)
        with self._lock:
            self._turns[thread_id] = turn
        try:
            turn_result = self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "approvalPolicy": "never",
                    "cwd": str(cwd),
                    "effort": reasoning_effort or None,
                    "input": [{"type": "text", "text": prompt}],
                    "model": model or None,
                    "outputSchema": output_schema,
                    "sandboxPolicy": app_server_sandbox_policy(sandbox, cwd),
                },
                timeout_seconds=control_timeout,
            )
            turn.turn_id = turn.turn_id or str((((turn_result or {}).get("turn") or {}).get("id")) or "")
            self.wait_for_turn(turn, timeout_seconds)
            self.mark_turn_completed()
            return turn
        finally:
            with self._lock:
                self._turns.pop(thread_id, None)

    def wait_for_turn(self, turn: AppServerTurn, timeout_seconds: int) -> None:
        timeout = max(1, int(timeout_seconds or 600))
        deadline = time.monotonic() + timeout
        while True:
            if process_cancel_requested():
                self.interrupt_turn(turn)
                raise ProcessCancelled("codex app-server turn cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.interrupt_turn(turn)
                raise TimeoutError(f"codex app-server turn timed out after {timeout_seconds}s")
            if turn.completed.wait(min(0.2, max(0.01, remaining))):
                return

    def mark_turn_completed(self) -> None:
        with self._lock:
            self._completed_turns += 1

    def interrupt_turn(self, turn: AppServerTurn) -> None:
        if not turn.turn_id:
            return
        try:
            self.request("turn/interrupt", {"threadId": turn.thread_id, "turnId": turn.turn_id}, timeout_seconds=5)
        except Exception:
            return

    def request(self, method: str, params: dict | None = None, *, timeout_seconds: int = 30) -> dict:
        request_id = self._request_id()
        pending = AppServerRequest()
        with self._lock:
            self._pending[request_id] = pending
        self._send({"method": method, "id": request_id, "params": params or {}})
        if not pending.event.wait(max(1, int(timeout_seconds or 30))):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"codex app-server request {method} timed out")
        response = pending.response or {}
        if isinstance(response.get("error"), dict):
            error = response["error"]
            raise RuntimeError(f"{method} failed: {error.get('message') or error}")
        return response.get("result") if isinstance(response.get("result"), dict) else {}

    def _start_locked(self) -> None:
        self.close("codex app-server restarting")
        self._closed = False
        self._terminal_error = ""
        prepare_app_server_state(self.env)
        state_lock = AppServerStateLock(
            app_server_state_lock_path(self.env),
            timeout_seconds=app_server_lock_timeout_seconds(self.env),
        )
        launch_args = app_server_launch_args(self.command, self.env)
        try:
            state_lock.acquire()
            self._state_lock = state_lock
            self.process = subprocess.Popen(
                launch_args,
                cwd=str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            self._reader = threading.Thread(target=self._reader_loop, name="codex-app-server-reader", daemon=True)
            self._reader.start()
            self._stderr_reader = threading.Thread(target=self._stderr_loop, name="codex-app-server-stderr", daemon=True)
            self._stderr_reader.start()
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "pullwise_worker", "title": "Pullwise Worker", "version": "0.1.0"},
                },
                timeout_seconds=30,
            )
            self._send({"method": "initialized", "params": {}})
            with self._lock:
                self._started_at = time.monotonic()
                self._completed_turns = 0
            self._ready = True
        except Exception as exc:
            details = self.stderr_text()
            self.close()
            if details:
                raise RuntimeError(f"failed to initialize codex app-server: {exc}; stderr: {details}") from exc
            raise

    def close(self, reason: str = "codex app-server closed") -> None:
        self._closed = True
        self._ready = False
        self._fail_pending(reason)
        with self._lock:
            self._started_at = 0.0
            self._completed_turns = 0
        process = self.process
        self.process = None
        try:
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            for stream in (process.stdout, process.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            current = threading.current_thread()
            for reader in (self._reader, self._stderr_reader):
                if reader is not None and reader is not current and reader.is_alive():
                    reader.join(timeout=0.5)
            self._reader = None
            self._stderr_reader = None
        finally:
            state_lock = self._state_lock
            self._state_lock = None
            if state_lock is not None:
                state_lock.release()

    def stderr_text(self) -> str:
        with self._lock:
            return "\n".join(self._stderr_lines[-20:])

    def _request_id(self) -> int:
        with self._lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _send(self, message: dict) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("codex app-server is not running")
        with self._send_lock:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _reader_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_message(message)
        except (OSError, ValueError):
            pass
        with self._lock:
            self._terminal_error = "codex app-server stdout closed"
        self._fail_pending("codex app-server stdout closed")

    def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                line = line.rstrip()
                if not line:
                    continue
                with self._lock:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > 100:
                        del self._stderr_lines[: len(self._stderr_lines) - 100]
        except (OSError, ValueError):
            pass

    def _handle_message(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = int(message.get("id"))
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.response = message
                pending.event.set()
            return
        if "id" in message and "method" in message:
            self._send({"id": message.get("id"), "error": {"code": -32601, "message": "unsupported server request"}})
            return
        self._handle_notification(message)

    def _handle_notification(self, message: dict) -> None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            return
        with self._lock:
            turn = self._turns.get(thread_id)
        if turn is None:
            return
        stored_event = stored_app_server_event(message)
        if stored_event is not None:
            turn.events.append(stored_event)
            if len(turn.events) > MAX_STORED_TURN_EVENTS:
                del turn.events[: len(turn.events) - MAX_STORED_TURN_EVENTS]
        method = str(message.get("method") or "")
        if method == "turn/started":
            turn.turn_id = str((params.get("turn") or {}).get("id") or turn.turn_id)
        elif method == "item/agentMessage/delta":
            turn.turn_id = str(params.get("turnId") or turn.turn_id)
            turn.deltas.append(str(params.get("delta") or ""))
        elif method == "item/completed":
            turn.turn_id = str(params.get("turnId") or turn.turn_id)
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "agentMessage":
                turn.assistant_messages.append(str(item.get("text") or ""))
        elif method == "turn/completed":
            turn_data = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn.turn_id = str(turn_data.get("id") or turn.turn_id)
            turn.status = str(turn_data.get("status") or "completed")
            error = turn_data.get("error") or turn_data.get("lastError")
            if error:
                turn.error = json.dumps(error, ensure_ascii=False) if isinstance(error, (dict, list)) else str(error)
            turn.completed.set()

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            turns = list(self._turns.values())
        for item in pending:
            item.response = {"error": {"message": reason}}
            item.event.set()
        for turn in turns:
            turn.error = turn.error or reason
            turn.completed.set()


def app_server_sandbox_policy(sandbox: str, cwd: Path) -> dict:
    if sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if sandbox == "danger-full-access":
        return {"type": "dangerFullAccess"}
    return {"type": "workspaceWrite", "networkAccess": False, "writableRoots": [str(cwd)]}


def app_server_control_timeout_seconds(timeout_seconds: int) -> int:
    return max(60, min(180, int(timeout_seconds or 600)))


def app_server_safe_to_retry(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "request initialize timed out" in text
        or "request thread/start timed out" in text
        or "initialize failed" in text
        or "stdout closed" in text
        or "database is locked" in text
        or "disk i/o error" in text
        or "failed to load configuration" in text
        or "overloaded" in text
        or "too many requests" in text
        or "429" in text
    )


def app_server_error_requires_restart(exc: Exception) -> bool:
    text = str(exc).lower()
    terminal_markers = (
        "stdout closed",
        "database is locked",
        "disk i/o error",
        "failed to load configuration",
        "failed to initialize codex app-server",
        "unauthorized",
        "failed to refresh token",
        "access token could not be refreshed",
        "refresh token was already used",
        "not authenticated",
        "authentication required",
        "login required",
    )
    return any(marker in text for marker in terminal_markers)


def app_server_retry_delay_seconds(attempt: int) -> float:
    return min(8.0, 1.0 * (2 ** max(0, int(attempt)))) + (0.05 * (os.getpid() % 7))


def app_server_max_age_seconds(env: dict[str, str] | None) -> int:
    return parse_non_negative_int((env or {}).get("PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS"), 14400)


def app_server_max_turns(env: dict[str, str] | None) -> int:
    return parse_non_negative_int((env or {}).get("PULLWISE_CODEX_APP_SERVER_MAX_TURNS"), 512)


def parse_non_negative_int(value: object, default: int) -> int:
    try:
        return max(0, int(str(value if value is not None else default).strip() or default))
    except (TypeError, ValueError):
        return default


def app_server_launch_args(command: str, env: dict[str, str] | None) -> list[str]:
    executable = resolve_app_server_command(command, env)
    args = [executable, "app-server"]
    if os.name != "nt":
        return args
    suffix = Path(executable).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        shell = (env or {}).get("ComSpec") or os.environ.get("ComSpec") or "cmd.exe"
        return [shell, "/d", "/s", "/c", subprocess.list2cmdline(args)]
    if suffix == ".ps1":
        shell = (env or {}).get("SystemRoot") or os.environ.get("SystemRoot") or r"C:\Windows"
        powershell = str(Path(shell) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable, "app-server"]
    return args


def resolve_app_server_command(command: str, env: dict[str, str] | None) -> str:
    value = command or "codex"
    if os.name != "nt":
        return value
    path = Path(value)
    if path.suffix:
        if path.suffix.lower() in {".cmd", ".ps1"}:
            vendor = resolve_npm_codex_vendor(path.parent, path.stem)
            if vendor is not None:
                return str(vendor)
        return str(path)
    if path.parent != Path("."):
        vendor = resolve_npm_codex_vendor(path.parent, path.name)
        if vendor is not None:
            return str(vendor)
        sibling = resolve_windows_candidate(path.parent, path.name, (".exe", ".cmd", ".bat", ".ps1", ""))
        return str(sibling or path)
    search_path = (env or {}).get("PATH") or os.environ.get("PATH") or ""
    path_entries = [Path(entry) for entry in search_path.split(os.pathsep) if entry]
    for entry in path_entries:
        vendor = resolve_npm_codex_vendor(entry, value)
        if vendor is not None:
            return str(vendor)
    for suffixes in ((".exe",), (".cmd", ".bat", ".ps1", "")):
        for entry in path_entries:
            candidate = resolve_windows_candidate(entry, value, suffixes)
            if candidate is not None:
                return str(candidate)
    return value


def resolve_windows_candidate(directory: Path, stem: str, suffixes: tuple[str, ...]) -> Path | None:
    for suffix in suffixes:
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def resolve_npm_codex_vendor(directory: Path, stem: str) -> Path | None:
    if not (directory / f"{stem}.cmd").is_file() and not (directory / f"{stem}.ps1").is_file():
        return None
    codex_package = directory / "node_modules" / "@openai" / "codex"
    if not codex_package.is_dir():
        return None
    matches = sorted(codex_package.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
    matches.extend(sorted(codex_package.glob("node_modules/@openai/codex-win32-*/vendor/*/codex.exe")))
    for candidate in matches:
        if candidate.is_file():
            return candidate
    return None


def reset_app_server_clients_for_tests() -> None:
    with _APP_SERVER_CLIENTS_LOCK:
        clients = list(_APP_SERVER_CLIENTS.values())
        _APP_SERVER_CLIENTS.clear()
    for client in clients:
        client.close()


atexit.register(reset_app_server_clients_for_tests)
