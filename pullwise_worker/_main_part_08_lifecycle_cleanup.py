from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

def service_action(
    action: str,
    *,
    dry_run: bool = False,
    no_block: bool = False,
    config: WorkerConfig | None = None,
) -> int:
    dependency_ok, dependency_detail = install_ubuntu_2204_dependencies(["systemctl"], dry_run=dry_run)
    if not dependency_ok:
        print(f"dependency install failed: {dependency_detail}", file=sys.stderr)
        return 1
    service_name = str(getattr(config, "service_name", None) or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
    command = ["systemctl"]
    if no_block:
        command.append("--no-block")
    command.extend([action, service_name])
    if dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command).returncode


def tail_text_lines(path: Path, lines: int) -> list[str]:
    if not regular_log_file(path):
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def worker_logs(config: WorkerConfig, *, lines: int = 120, follow: bool = False, dry_run: bool = False) -> int:
    safe_lines = max(1, min(1000, int(lines or 120)))
    service_name = str(getattr(config, "service_name", None) or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
    log_dir = Path(getattr(config, "log_dir", None) or tempfile.gettempdir())
    scan_summary = log_dir / "scan-summary.log"
    journal_command = ["journalctl", "-u", service_name, "-n", str(safe_lines), "--no-pager"]
    if follow:
        journal_command.append("-f")
    if dry_run:
        print(" ".join(shlex.quote(part) for part in journal_command))
        print(f"tail -n {safe_lines} {shlex.quote(str(scan_summary))}")
        return 0
    print(f"== journal: {service_name} ==")
    journal = subprocess.run(journal_command)
    if follow:
        return journal.returncode
    print(f"== scan summary: {scan_summary} ==")
    summary_lines = tail_text_lines(scan_summary, safe_lines)
    if summary_lines:
        print("\n".join(summary_lines))
    else:
        print("scan summary log not found or empty")
    return journal.returncode


def log_stream_text(value: object, limit: int = 4000) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").splitlines()[0].strip()[:limit]


def log_stream_session_id(session: dict | None) -> str:
    if not isinstance(session, dict):
        return ""
    return log_stream_text(session.get("id"), 128)


def log_stream_created_at(session: dict | None) -> int:
    if not isinstance(session, dict):
        return int(time.time())
    try:
        return max(0, int(session.get("created_at") or session.get("createdAt") or time.time()))
    except (TypeError, ValueError):
        return int(time.time())


def journal_log_entry_from_json(raw: str) -> tuple[dict | None, str]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    message = payload.get("MESSAGE")
    if isinstance(message, list):
        message = " ".join(str(part) for part in message)
    line = log_stream_text(message)
    if not line:
        return None, log_stream_text(payload.get("__CURSOR"), 500)
    timestamp = int(time.time())
    raw_timestamp = payload.get("__REALTIME_TIMESTAMP") or payload.get("_SOURCE_REALTIME_TIMESTAMP")
    try:
        timestamp = int(int(raw_timestamp) / 1_000_000)
    except (TypeError, ValueError):
        pass
    return {
        "source": "worker",
        "stream": "journal",
        "timestamp": timestamp,
        "line": line,
    }, log_stream_text(payload.get("__CURSOR"), 500)


class WorkerJournalLogTailer:
    def __init__(self, service_name: str, *, since_timestamp: int) -> None:
        self.service_name = service_name
        self.since_timestamp = max(0, int(since_timestamp or time.time()))
        self.cursor = ""
        self.unavailable_reported = False
        self.retry_after = 0.0

    def unavailable(self, detail: object) -> tuple[list[dict], str]:
        self.retry_after = time.time() + env_int("PULLWISE_LOG_STREAM_JOURNAL_RETRY_SECONDS", 60, minimum=1)
        if self.unavailable_reported:
            return [], self.cursor
        self.unavailable_reported = True
        return [
            {
                "source": "worker",
                "stream": "journal",
                "timestamp": int(time.time()),
                "line": f"journalctl unavailable: {log_stream_text(detail)}",
            }
        ], self.cursor

    def collect(self) -> tuple[list[dict], str]:
        if self.retry_after and time.time() < self.retry_after:
            return [], self.cursor
        command = ["journalctl", "-u", self.service_name, "--no-pager", "-o", "json"]
        if self.cursor:
            command.extend(["--after-cursor", self.cursor])
        else:
            command.extend(["--since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since_timestamp))])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=env_int("PULLWISE_LOG_STREAM_JOURNAL_TIMEOUT_SECONDS", 15, minimum=1),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self.unavailable(exc)
        if completed.returncode != 0:
            detail = log_stream_text(completed.stderr or completed.stdout or f"journalctl exited {completed.returncode}")
            return self.unavailable(detail)
        entries: list[dict] = []
        next_cursor = self.cursor
        for raw in completed.stdout.splitlines():
            entry, cursor = journal_log_entry_from_json(raw)
            if cursor:
                next_cursor = cursor
            if entry:
                entries.append(entry)
        self.retry_after = 0.0
        self.unavailable_reported = False
        return entries, next_cursor

    def commit(self, cursor: str) -> None:
        if cursor:
            self.cursor = cursor


class WorkerFileLogTailer:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.offset = path.lstat().st_size if regular_log_file(path) else 0
        except OSError:
            self.offset = 0
        self.partial = ""

    def collect(self, *, max_bytes: int = 128 * 1024) -> tuple[list[dict], int, str]:
        if not regular_log_file(self.path):
            return [], self.offset, self.partial
        try:
            size = self.path.lstat().st_size
        except OSError:
            return [], self.offset, self.partial
        truncated = not (0 <= self.offset <= size)
        start = 0 if truncated else self.offset
        partial = "" if truncated else self.partial
        try:
            with self.path.open("rb") as stream:
                stream.seek(start)
                chunk = stream.read(max_bytes)
                next_offset = stream.tell()
        except OSError:
            return [], self.offset, self.partial
        if not chunk:
            return [], next_offset, partial
        text = partial + chunk.decode("utf-8", errors="replace")
        parts = text.splitlines(keepends=True)
        next_partial = ""
        if parts and not parts[-1].endswith(("\n", "\r")):
            next_partial = parts.pop()
        timestamp = int(time.time())
        entries = [
            {"source": "worker", "stream": "scan-summary", "timestamp": timestamp, "line": line.rstrip("\r\n")}
            for line in parts
            if line.rstrip("\r\n")
        ]
        return entries, next_offset, next_partial

    def commit(self, offset: int, partial: str) -> None:
        self.offset = offset
        self.partial = partial


def regular_log_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


class WorkerLogStreamTailer:
    def __init__(self, config: WorkerConfig, session: dict) -> None:
        self.session_id = log_stream_session_id(session)
        self.journal = WorkerJournalLogTailer(
            str(getattr(config, "service_name", None) or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME,
            since_timestamp=log_stream_created_at(session),
        )
        self.summary = WorkerFileLogTailer(Path(getattr(config, "log_dir", None) or tempfile.gettempdir()) / "scan-summary.log")
        self.intro_sent = False

    def collect(self) -> tuple[list[dict], dict]:
        diagnostic_entries = []
        if not self.intro_sent:
            try:
                summary_exists = self.summary.path.exists()
                summary_size = self.summary.path.stat().st_size if summary_exists else 0
            except OSError:
                summary_exists = False
                summary_size = 0
            diagnostic_entries.append(
                {
                    "source": "worker",
                    "stream": "diagnostic",
                    "timestamp": int(time.time()),
                    "line": (
                        "log stream connected: "
                        f"session={self.session_id} "
                        f"service={self.journal.service_name} "
                        f"journal_since={self.journal.since_timestamp} "
                        f"summary={self.summary.path} "
                        f"summary_exists={summary_exists} "
                        f"summary_offset={self.summary.offset} "
                        f"summary_size={summary_size}"
                    ),
                }
            )
        journal_entries, journal_cursor = self.journal.collect()
        summary_entries, summary_offset, summary_partial = self.summary.collect()
        ordered_entries = list(enumerate([*diagnostic_entries, *journal_entries, *summary_entries]))
        ordered_entries.sort(key=lambda item: (int(item[1].get("timestamp") or 0), item[0]))
        entries = [entry for _, entry in ordered_entries]
        return entries, {
            "journal_cursor": journal_cursor,
            "summary_offset": summary_offset,
            "summary_partial": summary_partial,
            "intro_sent": self.intro_sent or bool(diagnostic_entries),
        }

    def commit(self, state: dict) -> None:
        self.journal.commit(str(state.get("journal_cursor") or ""))
        self.summary.commit(int(state.get("summary_offset") or self.summary.offset), str(state.get("summary_partial") or ""))
        if state.get("intro_sent"):
            self.intro_sent = True


def execute_lifecycle_command(action: str, config: WorkerConfig | None = None) -> int:
    if action == "stop":
        # Admin-queued lifecycle commands run inside the unprivileged service
        # process. Exit cleanly and let Restart=on-failure keep it stopped.
        return 0
    if action == "uninstall":
        if config is None:
            print("Remote uninstall requires a worker configuration.", file=sys.stderr)
            return 2
        if getattr(config, "remote_uninstall_finalizer", False):
            try:
                write_remote_uninstall_marker(config)
                return 0
            except Exception as exc:
                print(
                    f"remote uninstall finalizer marker failed; falling back to instance cleanup: {redact_secrets(str(exc), config)}",
                    file=sys.stderr,
                )
        try:
            cleanup_worker_instance(config)
        except Exception as exc:
            print(f"remote uninstall cleanup failed: {redact_secrets(str(exc), config)}", file=sys.stderr)
            return 1
        return 0
    return 2


def execute_watcher_lifecycle_command(action: str, config: WorkerConfig) -> int:
    if action == "stop":
        return service_action("stop", config=config)
    if action == "uninstall":
        stop_code = service_action("stop", config=config)
        if stop_code != 0:
            return stop_code
        try:
            write_remote_uninstall_marker(config)
        except Exception as exc:
            print(f"watcher uninstall marker failed: {redact_secrets(str(exc), config)}", file=sys.stderr)
            return 1
        return finalize_worker_uninstall(config)
    return 2


def command_worker_has_active_jobs(worker_state: dict | None) -> bool:
    if not isinstance(worker_state, dict):
        return False
    try:
        return int(worker_state.get("running_jobs") or worker_state.get("runningJobs") or 0) > 0
    except (TypeError, ValueError):
        return False


class WorkerLifecycleWatcher:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.client = PullwiseClient(config)
        self.last_error: str | None = None
        self.log_tailers: dict[str, WorkerLogStreamTailer] = {}

    def run(self, *, once: bool = False) -> int:
        while True:
            handled_uninstall = False
            try:
                payload = self.client.command_poll()
            except PullwiseRequestError as exc:
                self.last_error = f"command poll failed: {redact_secrets(str(exc), self.config)}"[:500]
                payload = {}
            command = payload.get("command") if isinstance(payload.get("command"), dict) else None
            worker_state = payload.get("worker") if isinstance(payload.get("worker"), dict) else None
            if command and self.handle_lifecycle_command(command, worker_state=worker_state):
                handled_uninstall = str(command.get("command") or "").strip().lower() == "uninstall"
            self.handle_log_session(payload.get("logSession") or payload.get("log_session"))
            if handled_uninstall:
                return 0
            if once:
                return 0
            time.sleep(max(1, int(getattr(self.config, "watcher_poll_seconds", 1) or 1)))

    def handle_lifecycle_command(self, command: dict, *, worker_state: dict | None = None) -> bool:
        parsed = lifecycle_command_parts(command)
        if parsed is None:
            return False
        command_id, action = parsed
        if action == "uninstall" and command_worker_has_active_jobs(worker_state):
            return False
        try:
            self.client.command_status(command_id, "running")
        except PullwiseRequestError as exc:
            self.last_error = f"command ack failed: {redact_secrets(str(exc), self.config)}"[:500]
            return False
        code = execute_watcher_lifecycle_command(action, self.config)
        if code == 0:
            try:
                self.client.command_status(command_id, "succeeded")
            except PullwiseRequestError as exc:
                self.last_error = f"command status failed: {redact_secrets(str(exc), self.config)}"[:500]
            return True
        error = f"{action} command exited {code}"
        try:
            self.client.command_status(command_id, "failed", error=error)
        except PullwiseRequestError as exc:
            self.last_error = f"command status failed: {redact_secrets(str(exc), self.config)}"[:500]
        return False

    def handle_log_session(self, session: object) -> None:
        session_id = log_stream_session_id(session if isinstance(session, dict) else None)
        if not session_id:
            self.log_tailers.clear()
            return
        if session_id not in self.log_tailers:
            self.log_tailers = {session_id: WorkerLogStreamTailer(self.config, session)}
        tailer = self.log_tailers[session_id]
        try:
            entries, state = tailer.collect()
        except Exception as exc:
            self.last_error = f"log stream collection failed: {redact_secrets(str(exc), self.config)}"[:500]
            return
        if not entries:
            return
        try:
            upload_log_stream_entries(self.client, session_id, entries)
        except PullwiseRequestError as exc:
            self.last_error = f"log stream upload failed: {redact_secrets(str(exc), self.config)}"[:500]
            return
        try:
            tailer.commit(state)
        except Exception as exc:
            self.last_error = f"log stream checkpoint failed: {redact_secrets(str(exc), self.config)}"[:500]


def upload_log_stream_entries(client: PullwiseClient, session_id: str, entries: list[dict]) -> None:
    for start in range(0, len(entries), 500):
        client.log_stream_lines(session_id, entries[start : start + 500])


def run_lifecycle_watcher(config: WorkerConfig, *, once: bool = False) -> int:
    return WorkerLifecycleWatcher(config).run(once=once)


def cleanup_worker_instance(config: WorkerConfig) -> None:
    targets = worker_instance_cleanup_targets(config)
    for target in targets:
        safe_worker_instance_rmtree(target)


def remote_uninstall_marker_path(config: WorkerConfig) -> Path:
    marker_text = str(getattr(config, "uninstall_marker_file", "") or "").strip()
    if not marker_text:
        raise ValueError("PULLWISE_UNINSTALL_MARKER_FILE is not configured")
    marker = Path(marker_text)
    if not marker.is_absolute() or path_is_root(marker):
        raise ValueError(f"refusing to use unsafe uninstall marker path: {marker}")
    return marker


def write_remote_uninstall_marker(config: WorkerConfig) -> Path:
    marker = remote_uninstall_marker_path(config)
    marker.parent.mkdir(parents=True, exist_ok=True)
    write_no_follow_text_file(marker, f"{getattr(config, 'worker_id', '')}\n")
    return marker


def finalize_worker_uninstall(config: WorkerConfig, *, dry_run: bool = False) -> int:
    marker = remote_uninstall_marker_path(config)
    if dry_run:
        print(f"require uninstall marker {marker}")
    elif not marker.exists():
        return 0
    code = uninstall_worker(
        config,
        remove_config=True,
        remove_logs=True,
        remove_service_home=True,
        remove_wrapper=True,
        remove_logrotate=True,
        remove_service_user=True,
        stop_service=False,
        dry_run=dry_run,
    )
    if code == 0 and not dry_run:
        marker.unlink(missing_ok=True)
    return code


def worker_instance_cleanup_targets(config: WorkerConfig) -> list[Path]:
    targets: list[Path] = []
    work_dir = Path(config.work_dir)
    service_home_text = str(getattr(config, "service_home", "") or "").strip()
    if service_home_text:
        service_home = Path(service_home_text)
        if safe_remote_service_home_target(service_home, work_dir):
            targets.append(service_home)
    if not targets:
        targets.append(work_dir)

    log_dir = Path(config.log_dir)
    if safe_worker_instance_log_target(log_dir, config) and not any(path_same_or_within(log_dir, target) for target in targets):
        targets.append(log_dir)
    return dedupe_cleanup_targets(targets)


def safe_remote_service_home_target(service_home: Path, work_dir: Path) -> bool:
    if not path_same_or_within(work_dir, service_home):
        return False
    if path_is_root(service_home):
        return False
    resolved_home = service_home.resolve(strict=False)
    resolved_work = work_dir.resolve(strict=False)
    if resolved_home.name in {"", "pullwise-worker"}:
        return False
    return resolved_work == resolved_home or resolved_work.parent == resolved_home


def safe_worker_instance_log_target(log_dir: Path, config: WorkerConfig | None = None) -> bool:
    if path_is_root(log_dir):
        return False
    resolved_name = log_dir.resolve(strict=False).name
    if resolved_name in {"", "pullwise-worker"}:
        return False
    return config is None or worker_instance_owned_path(log_dir, config)


def safe_worker_instance_config_target(config_dir: Path, config: WorkerConfig | None = None) -> bool:
    if path_is_root(config_dir):
        return False
    resolved_name = config_dir.resolve(strict=False).name
    if resolved_name in {"", "pullwise-worker"}:
        return False
    return config is None or worker_instance_owned_path(config_dir, config)


def worker_instance_owned_path(path: Path, config: WorkerConfig) -> bool:
    service_home = Path(str(getattr(config, "service_home", "") or ""))
    if str(service_home) and path_same_or_within(path, service_home):
        return True
    worker_id = str(getattr(config, "worker_id", "") or "").strip()
    return bool(worker_id) and path.resolve(strict=False).name == worker_id


def dedupe_cleanup_targets(targets: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for target in targets:
        resolved = str(target.resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(target)
    return deduped


def path_same_or_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def path_is_root(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return resolved == Path(resolved.anchor)


def safe_worker_instance_rmtree(path: Path) -> None:
    if path_is_root(path):
        raise ValueError(f"refusing to remove filesystem root: {path}")
    if path.is_symlink():
        raise ValueError(f"refusing to remove symlinked directory: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise ValueError(f"refusing to remove non-directory worker path: {path}")
    chdir_away_from(path)
    safe_rmtree(path, path)


def chdir_away_from(path: Path) -> None:
    try:
        cwd = Path.cwd()
    except OSError:
        return
    if not path_same_or_within(cwd, path):
        return
    anchor = path.resolve(strict=False).anchor or os.sep
    os.chdir(anchor)


def default_worker_package() -> str:
    package = os.environ.get("PULLWISE_WORKER_PACKAGE")
    if package:
        return package
    return f"{DEFAULT_WORKER_PACKAGE_BASE_URL}/v{__version__}/pullwise_worker-{__version__}-py3-none-any.whl"


def service_user_doctor_command(config: WorkerConfig, bin_path: Path) -> list[str]:
    service_user = str(getattr(config, "service_user", None) or DEFAULT_SERVICE_USER).strip() or DEFAULT_SERVICE_USER
    service_home = str(getattr(config, "service_home", None) or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    service_path = provider_tool_path(config)
    service_bin = str(bin_path).replace("\\", "/")
    doctor_command = f'cd "$HOME" && exec {shlex.quote(service_bin)} doctor'
    return [
        "runuser",
        "-u",
        service_user,
        "--",
        "env",
        f"HOME={service_home}",
        f"USERPROFILE={service_home}",
        f"CODEX_HOME={service_home}/.codex",
        f"CODEX_SQLITE_HOME={service_home}/.codex-sqlite",
        f"XDG_CONFIG_HOME={service_home}/.config",
        f"XDG_CACHE_HOME={service_home}/.cache",
        f"XDG_DATA_HOME={service_home}/.local/share",
        f"PATH={service_path}",
        "sh",
        "-lc",
        doctor_command,
    ]


def worker_wrapper_script(env_path: Path) -> str:
    env_file = shlex.quote(str(env_path))
    return f"""#!/usr/bin/env bash
set -euo pipefail
load_worker_env() {{
  local env_file="$1"
  local key value
  [ -f "$env_file" ] || return 0
  while IFS="=" read -r key value || [ -n "$key" ]; do
    [[ -z "$key" || "$key" == \\#* ]] && continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key=$value"
  done < "$env_file"
}}
load_worker_env {env_file}
SERVICE_HOME="${{PULLWISE_SERVICE_HOME:-/var/lib/pullwise-worker}}"
export HOME="$SERVICE_HOME"
export USERPROFILE="$SERVICE_HOME"
export CODEX_HOME="$SERVICE_HOME/.codex"
export CODEX_SQLITE_HOME="$SERVICE_HOME/.codex-sqlite"
export XDG_CONFIG_HOME="$SERVICE_HOME/.config"
export XDG_CACHE_HOME="$SERVICE_HOME/.cache"
export XDG_DATA_HOME="$SERVICE_HOME/.local/share"
SERVICE_PATH="${{PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}}"
export PATH="$SERVICE_HOME/.local/bin:$SERVICE_HOME/.codex/bin:$SERVICE_PATH"
PYTHON_BIN="${{PULLWISE_PYTHON_BIN:-python3.10}}"
exec "$PYTHON_BIN" -m pullwise_worker.main "$@"
"""


def write_worker_wrapper(bin_path: Path, env_path: Path) -> None:
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text(worker_wrapper_script(env_path), encoding="utf-8")
    bin_path.chmod(0o755)


def watcher_service_unit(config: WorkerConfig, *, env_path: Path | None = None, bin_path: Path | None = None) -> str:
    service_name = str(getattr(config, "watcher_service_name", "") or "").strip()
    if not service_name:
        raise ValueError("PULLWISE_WATCHER_SERVICE_NAME is required")
    worker_name = str(getattr(config, "service_name", None) or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
    env_file = str(env_path or getattr(config, "worker_env_file", "") or "").strip()
    worker_bin = str(bin_path or getattr(config, "worker_bin_path", "") or "").strip()
    if not env_file or not worker_bin:
        raise ValueError("worker env file and bin path are required")
    return f"""[Unit]
Description=Pullwise Worker Watcher {worker_name}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/
EnvironmentFile={env_file}
ExecStart={worker_bin} watch
Restart=on-failure
RestartSec=5
NoNewPrivileges=false
RuntimeDirectory={service_name}
RuntimeDirectoryMode=0750

[Install]
WantedBy=multi-user.target
"""


def append_missing_env_values(env_path: Path, values: dict[str, str], *, dry_run: bool = False) -> None:
    existing_keys: set[str] = set()
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, _value = line.partition("=")
            if sep and key:
                existing_keys.add(key)
    missing = [(key, value) for key, value in values.items() if key not in existing_keys]
    if dry_run:
        for key, value in missing:
            print(f"append env {key}={value}")
        return
    if not missing:
        return
    with env_path.open("a", encoding="utf-8") as handle:
        for key, value in missing:
            if "\n" in value or "\r" in value:
                raise ValueError(f"environment value for {key} must be single-line")
            handle.write(f"{key}={value}\n")


def ensure_lifecycle_watcher(
    config: WorkerConfig,
    *,
    env_path: Path | None = None,
    bin_path: Path | None = None,
    dry_run: bool = False,
) -> int:
    dependency_ok, dependency_detail = install_ubuntu_2204_dependencies(["systemctl"], dry_run=dry_run)
    if not dependency_ok:
        print(f"dependency install failed: {dependency_detail}", file=sys.stderr)
        return 1
    env_file = Path(env_path or config.worker_env_file)
    worker_bin = Path(bin_path or config.worker_bin_path)
    watcher_service_name = str(getattr(config, "watcher_service_name", "") or "").strip()
    watcher_service_file = Path(str(getattr(config, "watcher_service_file", "") or ""))
    if not watcher_service_name or path_is_root(watcher_service_file):
        print("watcher service name/file is not configured safely", file=sys.stderr)
        return 2
    env_values = {
        "PULLWISE_LIFECYCLE_WATCHER_ENABLED": "1",
        "PULLWISE_WATCHER_SERVICE_NAME": watcher_service_name,
        "PULLWISE_WATCHER_SERVICE_FILE": str(watcher_service_file),
        "PULLWISE_WATCHER_POLL_SECONDS": str(max(1, int(getattr(config, "watcher_poll_seconds", 5) or 5))),
    }
    if dry_run:
        append_missing_env_values(env_file, env_values, dry_run=True)
        print(f"write watcher service {watcher_service_file}")
        print("systemctl daemon-reload")
        print(f"systemctl enable {watcher_service_name}")
        print(f"systemctl restart {watcher_service_name}")
        return 0
    try:
        append_missing_env_values(env_file, env_values)
        watcher_service_file.parent.mkdir(parents=True, exist_ok=True)
        watcher_service_file.write_text(watcher_service_unit(config, env_path=env_file, bin_path=worker_bin), encoding="utf-8")
        watcher_service_file.chmod(0o644)
    except (OSError, ValueError) as exc:
        print(f"failed to write watcher service: {exc}", file=sys.stderr)
        return 1
    for command in (
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", watcher_service_name],
        ["systemctl", "restart", watcher_service_name],
    ):
        completed = subprocess.run(command)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def update_worker(config: WorkerConfig, *, dry_run: bool = False) -> int:
    dependency_ok, dependency_detail = install_ubuntu_2204_dependencies(
        ["python3.10", "python3-pip", "systemctl", "runuser"],
        dry_run=dry_run,
    )
    if not dependency_ok:
        print(f"dependency install failed: {dependency_detail}", file=sys.stderr)
        return 1
    package = default_worker_package()
    python_bin = os.environ.get("PULLWISE_PYTHON_BIN", "").strip() or "python3.10"
    env_path = Path(os.environ.get("PULLWISE_WORKER_ENV_FILE", "").strip() or config.worker_env_file)
    backup_path = Path(os.environ.get("PULLWISE_WORKER_ENV_BACKUP_FILE", "").strip() or config.worker_env_backup_file)
    bin_path = Path(os.environ.get("PULLWISE_WORKER_BIN_PATH", "").strip() or config.worker_bin_path)
    service_name = os.environ.get("PULLWISE_SERVICE_NAME", "").strip() or config.service_name
    install_command = [
        python_bin,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "--no-cache-dir",
        package,
    ]
    commands = [
        ["systemctl", "stop", service_name],
        install_command,
        ["systemctl", "restart", service_name],
        service_user_doctor_command(config, bin_path),
    ]
    if dry_run:
        print(f"backup {env_path} to {backup_path}")
    else:
        try:
            if env_path.exists():
                shutil.copy2(env_path, backup_path)
        except OSError as exc:
            print(f"failed to back up env file: {exc}", file=sys.stderr)
            return 1
    for command in commands:
        if dry_run:
            print(" ".join(command))
            if command is install_command:
                print(f"write env-loading wrapper {bin_path}")
                ensure_lifecycle_watcher(config, env_path=env_path, bin_path=bin_path, dry_run=True)
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            if backup_path.exists():
                shutil.copy2(backup_path, env_path)
            subprocess.run(["systemctl", "restart", service_name])
            return completed.returncode
        if command is install_command:
            try:
                write_worker_wrapper(bin_path, env_path)
                watcher_code = ensure_lifecycle_watcher(config, env_path=env_path, bin_path=bin_path, dry_run=False)
                if watcher_code != 0:
                    if backup_path.exists():
                        shutil.copy2(backup_path, env_path)
                    subprocess.run(["systemctl", "restart", service_name])
                    return watcher_code
            except OSError as exc:
                print(f"failed to write worker wrapper: {exc}", file=sys.stderr)
                if backup_path.exists():
                    shutil.copy2(backup_path, env_path)
                subprocess.run(["systemctl", "restart", service_name])
                return 1
    return 0


def uninstall_worker(
    config: WorkerConfig | None = None,
    *,
    remove_config: bool = False,
    remove_logs: bool = False,
    remove_service_home: bool = True,
    remove_wrapper: bool = True,
    remove_logrotate: bool = True,
    remove_service_user: bool = True,
    stop_service: bool = True,
    dry_run: bool = False,
) -> int:
    if config is None:
        config = WorkerConfig(argparse.Namespace(), require_worker_token=False, validate_server_url=False)
    dependency_ok, dependency_detail = install_ubuntu_2204_dependencies(["systemctl"], dry_run=dry_run)
    if not dependency_ok:
        print(f"dependency install failed: {dependency_detail}", file=sys.stderr)
        return 1
    service_name = config.service_name
    service_file = Path(config.service_file)
    config_dir = Path(config.worker_env_file).parent
    log_dir = Path(config.log_dir)
    service_home = Path(config.service_home)
    wrapper = Path(config.worker_bin_path)
    logrotate = Path(config.logrotate_file)
    watcher_enabled = bool(getattr(config, "lifecycle_watcher_enabled", False))
    watcher_service_name = str(getattr(config, "watcher_service_name", "") or "").strip()
    watcher_service_file = Path(str(getattr(config, "watcher_service_file", "") or ""))
    commands = []
    if stop_service:
        commands.append(["systemctl", "stop", service_name])
    commands.append(["systemctl", "disable", service_name])
    if (
        watcher_enabled
        and
        watcher_service_name
        and watcher_service_name != service_name
        and not path_is_root(watcher_service_file)
        and (dry_run or watcher_service_file.exists())
    ):
        commands.append(["systemctl", "disable", watcher_service_name])
    for command in commands:
        if dry_run:
            print(" ".join(command))
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            return completed.returncode
    if dry_run:
        print(f"remove {service_file}")
        if remove_wrapper:
            print(f"remove {wrapper}")
        if remove_logrotate:
            print(f"remove {logrotate}")
        if watcher_enabled and watcher_service_name and watcher_service_name != service_name:
            print(f"remove {watcher_service_file}")
        if remove_service_home:
            print(f"remove {service_home}")
        if remove_config and safe_worker_instance_config_target(config_dir, config):
            print(f"remove {config_dir}")
        if remove_logs and safe_worker_instance_log_target(log_dir, config):
            print(f"remove {log_dir}")
        if remove_service_user and removable_service_user(config.service_user):
            print(f"userdel {config.service_user}")
        print("systemctl daemon-reload")
    else:
        safe_unlink(service_file, service_name=service_name)
        if remove_wrapper:
            safe_worker_file_unlink(wrapper, Path("/usr/local/bin"), service_name)
        if remove_logrotate:
            safe_worker_file_unlink(logrotate, Path("/etc/logrotate.d"), service_name)
        if watcher_enabled and watcher_service_name and watcher_service_name != service_name and watcher_service_file.exists():
            safe_unlink(watcher_service_file, service_name=watcher_service_name)
        if remove_service_home and safe_remote_service_home_target(service_home, Path(config.work_dir)):
            safe_worker_instance_rmtree(service_home)
        if remove_config and safe_worker_instance_config_target(config_dir, config):
            safe_rmtree(config_dir, config_dir)
        if remove_logs and safe_worker_instance_log_target(log_dir, config):
            safe_worker_instance_rmtree(log_dir)
        if remove_service_user and removable_service_user(config.service_user):
            completed = subprocess.run(["userdel", config.service_user])
            if completed.returncode != 0:
                return completed.returncode
        completed = subprocess.run(["systemctl", "daemon-reload"])
        if completed.returncode != 0:
            return completed.returncode
    print("Worker disabled locally.")
    return 0


def cleanup_worker_resources(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    cleanup_checkouts(config, active_job_ids=active_job_ids)
    cleanup_logs(config, active_job_ids=active_job_ids)


def cleanup_checkouts(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    now_ts = int(time.time())
    active = set(active_job_ids or set())
    protected = active | _CHECKOUT_RUNTIME_DIR_NAMES
    config.work_dir.mkdir(parents=True, exist_ok=True)
    if not checkout_root_is_owned(config.work_dir):
        return
    for marker in config.work_dir.glob(f"*{_FAILED_CHECKOUT_MARKER_SUFFIX}"):
        checkout = checkout_dir_from_failed_marker(marker)
        if checkout.name in protected:
            continue
        if marker.is_symlink():
            _unlink_path_ignore_errors(marker)
            continue
        try:
            expires_at = int(marker.read_text(encoding="utf-8").strip() or "0")
        except (OSError, UnicodeDecodeError, ValueError):
            expires_at = 0
        if expires_at <= now_ts:
            if cleanup_checkout_path(checkout):
                _unlink_path_ignore_errors(marker)
    entries = sorted(
        [path for path in config.work_dir.iterdir() if path.is_dir() and path.name not in protected],
        key=lambda path: path.stat().st_mtime,
    )
    while directory_size(config.work_dir) > config.max_checkout_bytes and entries:
        checkout = entries.pop(0)
        if cleanup_checkout_path(checkout):
            _unlink_path_ignore_errors(failed_checkout_marker(checkout))


def cleanup_checkout_path(checkout: Path) -> bool:
    try:
        if checkout.is_symlink():
            checkout.unlink(missing_ok=True)
            return True
        remove_checkout_dir(checkout)
        return True
    except Exception:
        return False


def _unlink_path_ignore_errors(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def cleanup_logs(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    active = set(active_job_ids or set())
    if config.log_dir.is_symlink():
        return
    config.log_dir.mkdir(parents=True, exist_ok=True)
    now_ts = int(time.time())
    files: list[tuple[float, Path]] = []
    for path in config.log_dir.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file() or log_path_has_active_job_id(path, config.log_dir, active):
                continue
            stat = path.lstat()
        except OSError:
            continue
        if config.log_retention_seconds and stat.st_mtime < now_ts - config.log_retention_seconds:
            path.unlink(missing_ok=True)
            continue
        files.append((stat.st_mtime, path))
    files.sort(key=lambda item: item[0])
    while directory_size(config.log_dir) > config.max_log_bytes and files:
        _mtime, path = files.pop(0)
        try:
            if not log_path_has_active_job_id(path, config.log_dir, active):
                path.unlink(missing_ok=True)
        except OSError:
            continue
    prune_empty_directories(config.log_dir)


def log_path_has_active_job_id(path: Path, log_dir: Path, active_job_ids: set[str]) -> bool:
    if not active_job_ids:
        return False
    try:
        parts = path.resolve(strict=False).relative_to(log_dir.resolve(strict=False)).parts
    except ValueError:
        return True
    return any(part in active_job_ids for part in parts)


def prune_empty_directories(root: Path) -> None:
    directories = sorted(
        [item for item in root.rglob("*") if item.is_dir() and not item.is_symlink()],
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            continue


def trim_file_to_last_bytes(path: Path, max_bytes: int) -> None:
    if not regular_log_file(path):
        return
    try:
        size = path.lstat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    keep = max(1, max_bytes)
    with path.open("rb") as handle:
        handle.seek(-keep, os.SEEK_END)
        data = handle.read()
    newline = data.find(b"\n")
    if newline >= 0 and newline + 1 < len(data):
        data = data[newline + 1 :]
    with path.open("wb") as handle:
        handle.write(data)


def safe_worker_file_unlink(path: Path, allowed_root: Path, service_name: str) -> None:
    safe_service_name = str(service_name or "").strip()
    if not safe_service_name.startswith(DEFAULT_SERVICE_NAME):
        raise ValueError(f"refusing to remove unexpected worker file for service: {service_name}")
    resolved = path.resolve(strict=False)
    allowed = allowed_root.resolve(strict=False)
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"refusing to remove unexpected file: {path}") from exc
    if path.name != safe_service_name:
        raise ValueError(f"refusing to remove unexpected file: {path}")
    path.unlink(missing_ok=True)


def removable_service_user(service_user: str) -> bool:
    return str(service_user or "").strip().startswith("pw-worker-")


def safe_unlink(path: Path, *, service_name: str = DEFAULT_SERVICE_NAME) -> None:
    safe_service_name = str(service_name or "").strip()
    expected = Path("/etc/systemd/system") / f"{safe_service_name}.service"
    if not safe_service_name.startswith(DEFAULT_SERVICE_NAME) or path.resolve(strict=False) != expected.resolve(strict=False):
        raise ValueError(f"refusing to remove unexpected file: {path}")
    path.unlink(missing_ok=True)


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to remove symlinked directory: {path}")
    resolved = path.resolve(strict=False)
    allowed = allowed_root.resolve(strict=False)
    if resolved != allowed:
        raise ValueError(f"refusing to remove unexpected directory: {path}")
    if not path.exists():
        return
    shutil.rmtree(path)
    if path.exists():
        raise OSError(f"failed to remove directory: {path}")


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            stat_result = item.lstat()
        except OSError:
            continue
        if stat.S_ISREG(stat_result.st_mode):
            total += stat_result.st_size
    return total


if __name__ == "__main__":
    main()
