from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

def service_action(action: str, *, dry_run: bool = False, no_block: bool = False) -> int:
    command = ["systemctl"]
    if no_block:
        command.append("--no-block")
    command.extend([action, "pullwise-worker"])
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
    if not any(path_same_or_within(log_dir, target) for target in targets):
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


def service_user_doctor_command(bin_path: Path) -> list[str]:
    service_user = os.environ.get("PULLWISE_SERVICE_USER", "").strip() or "pullwise-worker"
    service_home = os.environ.get("PULLWISE_SERVICE_HOME", "").strip() or "/var/lib/pullwise-worker"
    service_path = (
        os.environ.get("PULLWISE_SERVICE_PATH", "").strip()
        or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    service_bin = str(bin_path).replace("\\", "/")
    doctor_command = f'cd "$HOME" && exec {shlex.quote(service_bin)} doctor'
    return [
        "runuser",
        "-u",
        service_user,
        "--",
        "env",
        f"HOME={service_home}",
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
export PATH="${{PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}}"
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
    env_path = Path(os.environ.get("PULLWISE_WORKER_ENV_FILE") or "/etc/pullwise-worker/worker.env")
    backup_path = Path(os.environ.get("PULLWISE_WORKER_ENV_BACKUP_FILE") or "/etc/pullwise-worker/worker.env.bak")
    bin_path = Path(os.environ.get("PULLWISE_WORKER_BIN_PATH") or "/usr/local/bin/pullwise-worker")
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
        ["systemctl", "stop", "pullwise-worker"],
        install_command,
        ["systemctl", "restart", "pullwise-worker"],
        service_user_doctor_command(bin_path),
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
            subprocess.run(["systemctl", "restart", "pullwise-worker"])
            return completed.returncode
        if command is install_command:
            try:
                write_worker_wrapper(bin_path, env_path)
            except OSError as exc:
                print(f"failed to write worker wrapper: {exc}", file=sys.stderr)
                if backup_path.exists():
                    shutil.copy2(backup_path, env_path)
                subprocess.run(["systemctl", "restart", "pullwise-worker"])
                return 1
    return 0


def uninstall_worker(
    *,
    remove_config: bool = False,
    remove_logs: bool = False,
    dry_run: bool = False,
) -> int:
    commands = [
        ["systemctl", "stop", "pullwise-worker"],
        ["systemctl", "disable", "pullwise-worker"],
    ]
    for command in commands:
        if dry_run:
            print(" ".join(command))
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            return completed.returncode
    if dry_run:
        print("remove /etc/systemd/system/pullwise-worker.service")
        if remove_config:
            print("remove /etc/pullwise-worker")
        if remove_logs:
            print("remove /var/log/pullwise-worker")
        print("systemctl daemon-reload")
    else:
        safe_unlink(Path("/etc/systemd/system/pullwise-worker.service"))
        if remove_config:
            safe_rmtree(Path("/etc/pullwise-worker"), Path("/etc/pullwise-worker"))
        if remove_logs:
            safe_rmtree(Path("/var/log/pullwise-worker"), Path("/var/log/pullwise-worker"))
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


def safe_unlink(path: Path) -> None:
    if str(path) != "/etc/systemd/system/pullwise-worker.service":
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
