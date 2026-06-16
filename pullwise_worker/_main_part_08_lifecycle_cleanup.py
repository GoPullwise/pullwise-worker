from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

def service_action(
    action: str,
    *,
    dry_run: bool = False,
    no_block: bool = False,
    config: WorkerConfig | None = None,
) -> int:
    service_name = str(getattr(config, "service_name", None) or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
    command = ["systemctl"]
    if no_block:
        command.append("--no-block")
    command.extend([action, service_name])
    if dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command).returncode


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
    marker.write_text(f"{getattr(config, 'worker_id', '')}\n", encoding="utf-8")
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
    if safe_worker_instance_log_target(log_dir) and not any(path_same_or_within(log_dir, target) for target in targets):
        targets.append(log_dir)
    return dedupe_cleanup_targets(targets)


def safe_remote_service_home_target(service_home: Path, work_dir: Path) -> bool:
    if not path_same_or_within(work_dir, service_home):
        return False
    if path_is_root(service_home):
        return False
    if os.environ.get("PULLWISE_SERVICE_HOME", "").strip() == str(service_home):
        return True
    return service_home.resolve(strict=False).name not in {"", "pullwise-worker"}


def safe_worker_instance_log_target(log_dir: Path) -> bool:
    if path_is_root(log_dir):
        return False
    return log_dir.resolve(strict=False).name not in {"", "pullwise-worker"}


def safe_worker_instance_config_target(config_dir: Path) -> bool:
    if path_is_root(config_dir):
        return False
    return config_dir.resolve(strict=False).name not in {"", "pullwise-worker"}


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
export XDG_CONFIG_HOME="$SERVICE_HOME/.config"
export XDG_CACHE_HOME="$SERVICE_HOME/.cache"
export XDG_DATA_HOME="$SERVICE_HOME/.local/share"
SERVICE_PATH="${{PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}}"
export PATH="$SERVICE_HOME/.local/bin:$SERVICE_HOME/.codex/bin:$SERVICE_PATH"
PYTHON_BIN="${{PULLWISE_PYTHON_BIN:-python3}}"
exec "$PYTHON_BIN" -m pullwise_worker.main "$@"
"""


def write_worker_wrapper(bin_path: Path, env_path: Path) -> None:
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text(worker_wrapper_script(env_path), encoding="utf-8")
    bin_path.chmod(0o755)


def update_worker(config: WorkerConfig, *, dry_run: bool = False) -> int:
    package = default_worker_package()
    python_bin = os.environ.get("PULLWISE_PYTHON_BIN", "").strip() or "python3"
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
    service_name = config.service_name
    service_file = Path(config.service_file)
    config_dir = Path(config.worker_env_file).parent
    log_dir = Path(config.log_dir)
    service_home = Path(config.service_home)
    wrapper = Path(config.worker_bin_path)
    logrotate = Path(config.logrotate_file)
    commands = []
    if stop_service:
        commands.append(["systemctl", "stop", service_name])
    commands.append(["systemctl", "disable", service_name])
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
        if remove_service_home:
            print(f"remove {service_home}")
        if remove_config and safe_worker_instance_config_target(config_dir):
            print(f"remove {config_dir}")
        if remove_logs and safe_worker_instance_log_target(log_dir):
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
        if remove_service_home and safe_remote_service_home_target(service_home, Path(config.work_dir)):
            safe_worker_instance_rmtree(service_home)
        if remove_config and safe_worker_instance_config_target(config_dir):
            safe_rmtree(config_dir, config_dir)
        if remove_logs and safe_worker_instance_log_target(log_dir):
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
        try:
            expires_at = int(marker.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            expires_at = 0
        if expires_at <= now_ts:
            shutil.rmtree(checkout, ignore_errors=True)
            marker.unlink(missing_ok=True)
    entries = sorted(
        [path for path in config.work_dir.iterdir() if path.is_dir() and path.name not in protected],
        key=lambda path: path.stat().st_mtime,
    )
    while directory_size(config.work_dir) > config.max_checkout_bytes and entries:
        checkout = entries.pop(0)
        shutil.rmtree(checkout, ignore_errors=True)
        failed_checkout_marker(checkout).unlink(missing_ok=True)


def cleanup_logs(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    active = set(active_job_ids or set())
    config.log_dir.mkdir(parents=True, exist_ok=True)
    now_ts = int(time.time())
    files: list[tuple[float, Path]] = []
    for path in config.log_dir.rglob("*"):
        try:
            if not path.is_file() or log_path_has_active_job_id(path, config.log_dir, active):
                continue
            stat = path.stat()
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
        [item for item in root.rglob("*") if item.is_dir()],
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            continue


def trim_file_to_last_bytes(path: Path, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
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
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


if __name__ == "__main__":
    main()
