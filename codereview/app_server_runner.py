from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CodexConfig
from .utils.process import ProcessResult


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


def app_server_key(command: str, env: dict[str, str] | None) -> tuple[str, str, str, str, str]:
    source = env or {}
    return (
        str(command or "codex"),
        str(source.get("HOME") or source.get("USERPROFILE") or ""),
        str(source.get("CODEX_HOME") or ""),
        str(source.get("CODEX_SQLITE_HOME") or ""),
        str(source.get("PATH") or ""),
    )


def get_codex_app_server_client(command: str, env: dict[str, str] | None, cwd: Path) -> "CodexAppServerClient":
    key = app_server_key(command, env)
    with _APP_SERVER_CLIENTS_LOCK:
        client = _APP_SERVER_CLIENTS.get(key)
        if client is None or client.is_closed():
            client = CodexAppServerClient(command=command, env=env, cwd=cwd)
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
        except Exception as exc:
            if client is not None:
                client.close()
            if attempt == 0 and app_server_safe_to_retry(exc):
                time.sleep(1)
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
        events_file.parent.mkdir(parents=True, exist_ok=True)
        events_file.write_text(events_text, encoding="utf-8")
    if turn.error:
        return ProcessResult(process_command, str(cd), 1, events_text[-65536:], turn.error, duration_ms, stdout_path=str(events_file or ""))
    text = final_assistant_text(turn)
    if not text:
        return ProcessResult(process_command, str(cd), 1, events_text[-65536:], "codex app-server turn completed without assistant output", duration_ms, stdout_path=str(events_file or ""))
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(text, encoding="utf-8")
    return ProcessResult(process_command, str(cd), 0, events_text[-65536:], "", duration_ms, stdout_path=str(events_file or ""))


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
            if not turn.completed.wait(max(1, int(timeout_seconds or 600))):
                self.interrupt_turn(turn)
                raise TimeoutError(f"codex app-server turn timed out after {timeout_seconds}s")
            self.mark_turn_completed()
            return turn
        finally:
            with self._lock:
                self._turns.pop(thread_id, None)

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
        self.close()
        self._closed = False
        launch_args = app_server_launch_args(self.command, self.env)
        try:
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

    def close(self) -> None:
        self._closed = True
        self._ready = False
        self._fail_pending("codex app-server closed")
        with self._lock:
            self._started_at = 0.0
            self._completed_turns = 0
        process = self.process
        self.process = None
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
        turn.events.append(message)
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
    text = str(exc)
    return (
        "request initialize timed out" in text
        or "request thread/start timed out" in text
        or "initialize failed" in text
        or "stdout closed" in text
        or "database is locked" in text
        or "disk I/O error" in text
    )


def app_server_max_age_seconds(env: dict[str, str] | None) -> int:
    return parse_non_negative_int((env or {}).get("PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS"), 2400)


def app_server_max_turns(env: dict[str, str] | None) -> int:
    return parse_non_negative_int((env or {}).get("PULLWISE_CODEX_APP_SERVER_MAX_TURNS"), 48)


def parse_non_negative_int(value: object, default: int) -> int:
    try:
        return max(0, int(str(value if value is not None else default).strip() or default))
    except (TypeError, ValueError):
        return default


def app_server_launch_args(command: str, env: dict[str, str] | None) -> list[str]:
    executable = resolve_app_server_command(command, env)
    args = [executable, "app-server", "--stdio"]
    if os.name != "nt":
        return args
    suffix = Path(executable).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        shell = (env or {}).get("ComSpec") or os.environ.get("ComSpec") or "cmd.exe"
        return [shell, "/d", "/s", "/c", subprocess.list2cmdline(args)]
    if suffix == ".ps1":
        shell = (env or {}).get("SystemRoot") or os.environ.get("SystemRoot") or r"C:\Windows"
        powershell = str(Path(shell) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable, "app-server", "--stdio"]
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
