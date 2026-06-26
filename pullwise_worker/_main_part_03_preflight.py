from __future__ import annotations

# Imported by main.py and re-exported from the aggregate module.

from ._main_part_01_bootstrap import *  # noqa: F403
from ._main_part_02_worker_checkout import *  # noqa: F403

def collect_preflight_metadata(config: WorkerConfig, job: dict, checkout_dir: Path) -> dict:
    repository = repository_preflight_metadata(checkout_dir)
    repository_stats = repository_resource_stats(checkout_dir)
    repository_limits = repository_limits_metadata(config)
    tool_versions = worker_tool_versions(config, repository["packageManagers"])
    return {
        "mode": "static",
        "execution": "no_project_scripts",
        "summary": "Static preflight captured repository manifests, worker environment, and tool versions; no project scripts were executed.",
        "repo": str(job.get("repo") or ""),
        "branch": str(job.get("branch") or "main"),
        "commit": str(job.get("commit") or "pending"),
        "workerVersion": __version__,
        "provider": str(getattr(config, "provider", "") or ""),
        "environment": worker_environment_metadata(checkout_dir),
        "languages": repository["languages"],
        "packageManagers": repository["packageManagers"],
        "manifests": repository["manifests"],
        "availableScripts": repository["availableScripts"],
        "repositoryStats": repository_stats,
        "repositoryLimits": repository_limits,
        "toolVersions": tool_versions,
        "limitations": [
            "Dependency installation, build, tests, lint, and typecheck were not executed in this preflight.",
            "Runtime reproduction is handled by the GraphVerified review after repository context generation.",
        ],
    }


class RepositoryTooLargeError(RuntimeError):
    def __init__(self, message: str, preflight: dict) -> None:
        super().__init__(message)
        self.error_code = REPOSITORY_TOO_LARGE_ERROR_CODE
        self.preflight = preflight


def repository_limits_metadata(config: WorkerConfig) -> dict:
    return {
        "maxFiles": bounded_positive_int(
            getattr(config, "max_repo_files", _DEFAULT_MAX_REPO_FILES),
            default=_DEFAULT_MAX_REPO_FILES,
            maximum=_MAX_REPO_LIMIT_FILES,
        ),
        "maxBytes": bounded_positive_int(
            getattr(config, "max_repo_bytes", _DEFAULT_MAX_REPO_BYTES),
            default=_DEFAULT_MAX_REPO_BYTES,
            maximum=_MAX_REPO_LIMIT_BYTES,
        ),
    }


def repository_resource_stats(checkout_dir: Path, limits: dict | None = None) -> dict:
    file_count = 0
    total_bytes = 0
    if not checkout_dir.is_dir():
        return {"fileCount": 0, "totalBytes": 0}
    max_files = positive_limit_int(limits.get("maxFiles"), 0) if isinstance(limits, dict) else 0
    max_bytes = positive_limit_int(limits.get("maxBytes"), 0) if isinstance(limits, dict) else 0
    stopped_early = False
    stack = [checkout_dir]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                stat_result = entry.stat(follow_symlinks=False)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
            except OSError:
                continue
            if entry.is_file(follow_symlinks=False) or entry.is_symlink():
                file_count += 1
                total_bytes += max(0, int(stat_result.st_size))
                if (max_files and file_count > max_files) or (max_bytes and total_bytes > max_bytes):
                    stopped_early = True
                    stack.clear()
                    break
    stats = {"fileCount": file_count, "totalBytes": total_bytes}
    if stopped_early:
        stats["scanStoppedEarly"] = True
    return stats


def repository_limit_exceeded(stats: dict, limits: dict) -> list[str]:
    exceeded = []
    if positive_limit_int(stats.get("fileCount"), 0) > positive_limit_int(limits.get("maxFiles"), 0):
        exceeded.append("file_count")
    if positive_limit_int(stats.get("totalBytes"), 0) > positive_limit_int(limits.get("maxBytes"), 0):
        exceeded.append("total_bytes")
    return exceeded


def positive_limit_int(value: object, default: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        return max(minimum, int(default or 0))
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default or 0)
    return max(minimum, parsed)


def repository_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def read_repository_text_file(path: Path, max_bytes: int = _REPOSITORY_TEXT_READ_MAX_BYTES) -> str | None:
    if not repository_regular_file(path):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            return None
        byte_limit = positive_limit_int(max_bytes, _REPOSITORY_TEXT_READ_MAX_BYTES, minimum=1)
        if stat_result.st_size > byte_limit:
            return None
        chunks = []
        remaining = byte_limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > byte_limit:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def repository_path_exists_without_following_symlink(path: Path) -> bool:
    try:
        path.lstat()
    except OSError:
        return False
    return True


def repository_limit_preflight_metadata(config: WorkerConfig, job: dict, checkout_dir: Path) -> dict:
    limits = repository_limits_metadata(config)
    stats = repository_resource_stats(checkout_dir, limits=limits)
    exceeded = repository_limit_exceeded(stats, limits)
    summary = (
        "Repository checkout exceeds Pullwise worker repository limits; GraphVerified review was not executed."
        if exceeded
        else "Repository checkout is within Pullwise worker repository limits."
    )
    return {
        "mode": "static",
        "execution": "repository_limit_check",
        "summary": summary,
        "repo": str(job.get("repo") or ""),
        "branch": str(job.get("branch") or "main"),
        "commit": str(job.get("commit") or "pending"),
        "workerVersion": __version__,
        "provider": str(getattr(config, "provider", "") or ""),
        "repositoryStats": stats,
        "repositoryLimits": limits,
        "repositoryLimitExceeded": bool(exceeded),
        "repositoryLimitReasons": exceeded,
        "limitations": [
            "GraphVerified review was not executed before this repository size check.",
        ],
    }


def repository_tree_resource_stats(git_dir: Path, ref: str, limits: dict | None = None) -> dict:
    file_count = 0
    total_bytes = 0
    max_files = positive_limit_int(limits.get("maxFiles"), 0) if isinstance(limits, dict) else 0
    max_bytes = positive_limit_int(limits.get("maxBytes"), 0) if isinstance(limits, dict) else 0
    stopped_early = False
    timeout_seconds = env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600)
    command = ["git", "-C", str(git_dir), "ls-tree", "-r", "-l", "-z", str(ref)]
    log_worker_git_event("tree-limit", "start", command=command, detail=f"timeout={timeout_seconds}s")
    started = time.monotonic()
    pending = b""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            if process.stdout is None:
                returncode = process.wait()
            else:
                stdout_fd = process.stdout.fileno()
                while True:
                    if time.monotonic() - started > timeout_seconds:
                        process.kill()
                        raise RuntimeError(f"git tree-limit timed out after {timeout_seconds}s")
                    ready, _, _ = select.select([stdout_fd], [], [], 0.2)
                    if not ready:
                        if process.poll() is not None:
                            break
                        continue
                    chunk = os.read(stdout_fd, 65536)
                    if not chunk:
                        break
                    pending += chunk
                    records = pending.split(b"\x00")
                    pending = records.pop()
                    for record in records:
                        if not record:
                            continue
                        header = record.split(b"\t", 1)[0].decode("utf-8", errors="replace")
                        parts = header.split()
                        if len(parts) < 4:
                            continue
                        file_count += 1
                        try:
                            size = int(parts[3])
                        except ValueError:
                            size = 0
                        total_bytes += max(0, size)
                        if (max_files and file_count > max_files) or (max_bytes and total_bytes > max_bytes):
                            stopped_early = True
                            process.kill()
                            break
                    if stopped_early:
                        break
                returncode = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
    if returncode != 0 and not stopped_early:
        log_worker_git_event("tree-limit", "failed", command=command, detail=f"git exited with status {returncode}")
        raise RuntimeError(f"git tree-limit failed: git exited with status {returncode}")
    log_worker_git_event("tree-limit", "done", command=command)
    stats = {"fileCount": file_count, "totalBytes": total_bytes}
    if stopped_early:
        stats["scanStoppedEarly"] = True
    return stats


def repository_tree_limit_preflight_metadata(config: WorkerConfig, job: dict, git_dir: Path, ref: str) -> dict:
    limits = repository_limits_metadata(config)
    stats = repository_tree_resource_stats(git_dir, ref, limits=limits)
    exceeded = repository_limit_exceeded(stats, limits)
    summary = (
        "Repository git tree exceeds Pullwise worker repository limits; checkout was not materialized."
        if exceeded
        else "Repository git tree is within Pullwise worker repository limits."
    )
    return {
        "mode": "static",
        "execution": "repository_tree_limit_check",
        "summary": summary,
        "repo": str(job.get("repo") or ""),
        "branch": str(job.get("branch") or "main"),
        "commit": str(ref or job.get("commit") or "pending"),
        "workerVersion": __version__,
        "provider": str(getattr(config, "provider", "") or ""),
        "repositoryStats": stats,
        "repositoryLimits": limits,
        "repositoryLimitExceeded": bool(exceeded),
        "repositoryLimitReasons": exceeded,
        "limitations": [
            "The worker checked the fetched git tree before materializing the checkout.",
        ],
    }


def enforce_repository_tree_limits(config: WorkerConfig, job: dict, git_dir: Path, ref: str) -> dict:
    preflight = repository_tree_limit_preflight_metadata(config, job, git_dir, ref)
    exceeded = preflight.get("repositoryLimitReasons") if isinstance(preflight.get("repositoryLimitReasons"), list) else []
    if not exceeded:
        return preflight
    stats = preflight["repositoryStats"]
    limits = preflight["repositoryLimits"]
    raise RepositoryTooLargeError(
        (
            "Repository is too large for Pullwise scanning before checkout "
            f"({stats['fileCount']} files / {stats['totalBytes']} bytes; "
            f"limits {limits['maxFiles']} files / {limits['maxBytes']} bytes)."
        ),
        preflight,
    )


def enforce_repository_limits(config: WorkerConfig, job: dict, checkout_dir: Path) -> dict:
    preflight = repository_limit_preflight_metadata(config, job, checkout_dir)
    exceeded = preflight.get("repositoryLimitReasons") if isinstance(preflight.get("repositoryLimitReasons"), list) else []
    if not exceeded:
        return preflight
    stats = preflight["repositoryStats"]
    limits = preflight["repositoryLimits"]
    raise RepositoryTooLargeError(
        (
            "Repository is too large for Pullwise scanning "
            f"({stats['fileCount']} files / {stats['totalBytes']} bytes; "
            f"limits {limits['maxFiles']} files / {limits['maxBytes']} bytes)."
        ),
        preflight,
    )


def worker_environment_metadata(checkout_dir: Path) -> dict:
    return {
        "os": platform.system(),
        "osRelease": platform.release(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "pythonVersion": platform.python_version(),
        "pythonExecutable": Path(sys.executable).name or "python",
        "checkoutRoot": "repository-root",
    }


def repository_preflight_metadata(checkout_dir: Path) -> dict:
    manifests = repository_manifests(checkout_dir)
    package_json = read_package_json(checkout_dir / "package.json")
    scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
    available_scripts = sorted(
        script for script in _PACKAGE_SCRIPT_NAMES if isinstance(scripts, dict) and script in scripts
    )
    package_managers = package_managers_for_repository(checkout_dir, package_json)
    languages = language_hints_for_repository(checkout_dir, manifests)
    return {
        "languages": languages,
        "packageManagers": package_managers,
        "manifests": manifests,
        "availableScripts": available_scripts,
    }


def repository_manifests(checkout_dir: Path) -> list[dict]:
    manifests: list[dict] = []
    for filename, manifest_type in sorted(_MANIFEST_TYPES.items()):
        path = checkout_dir / filename
        if repository_regular_file(path):
            manifests.append({"file": filename, "type": manifest_type})
    for filename, manifest_type in sorted(_CONFIG_MANIFEST_TYPES.items()):
        path = checkout_dir / filename
        if repository_regular_file(path):
            manifests.append({"file": filename, "type": manifest_type})
    for filename, manager in sorted(_LOCKFILE_PACKAGE_MANAGERS.items()):
        path = checkout_dir / filename
        if repository_regular_file(path):
            manifests.append({"file": filename, "type": f"{manager}-lock"})
    manifests.extend(github_actions_workflow_manifests(checkout_dir))
    manifests.extend(dockerfile_manifests(checkout_dir))
    return dedupe_manifests(manifests)[:50]


def github_actions_workflow_manifests(checkout_dir: Path) -> list[dict]:
    workflows_dir = checkout_dir / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    manifests = []
    for path in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        if repository_regular_file(path):
            manifests.append({"file": path.relative_to(checkout_dir).as_posix(), "type": "github-actions-workflow"})
    return manifests[:20]


def dockerfile_manifests(checkout_dir: Path) -> list[dict]:
    manifests = []
    for path in iter_dockerfiles(checkout_dir):
        manifests.append({"file": path.relative_to(checkout_dir).as_posix(), "type": "dockerfile"})
    return manifests[:20]


def dedupe_manifests(manifests: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for manifest in manifests:
        key = (manifest.get("file"), manifest.get("type"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(manifest)
    return deduped


def read_package_json(path: Path) -> dict:
    text = read_repository_text_file(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def package_managers_for_repository(checkout_dir: Path, package_json: dict) -> list[str]:
    managers = []
    package_manager = str(package_json.get("packageManager") or "").strip()
    if package_manager:
        managers.append(package_manager.split("@", 1)[0])
    for filename, manager in sorted(_LOCKFILE_PACKAGE_MANAGERS.items()):
        if repository_regular_file(checkout_dir / filename):
            managers.append(manager)
    if repository_regular_file(checkout_dir / "package.json") and not managers:
        managers.append("npm")
    return list(dict.fromkeys(manager for manager in managers if manager))


def language_hints_for_repository(checkout_dir: Path, manifests: list[dict]) -> list[str]:
    hints = []
    manifest_types = {item.get("type") for item in manifests}
    if "node" in manifest_types:
        hints.append("JavaScript/TypeScript")
    if {"python", "python-lock"} & manifest_types:
        hints.append("Python")
    if "go" in manifest_types:
        hints.append("Go")
    if "rust" in manifest_types:
        hints.append("Rust")
    extension_hints = [
        ("*.ts", "TypeScript"),
        ("*.tsx", "TypeScript"),
        ("*.js", "JavaScript"),
        ("*.jsx", "JavaScript"),
        ("*.py", "Python"),
        ("*.go", "Go"),
        ("*.rs", "Rust"),
    ]
    for pattern, label in extension_hints:
        if label not in hints and any(checkout_dir.glob(pattern)):
            hints.append(label)
    return hints[:8]


def worker_tool_versions(config: WorkerConfig, package_managers: list[str] | None = None) -> list[dict]:
    checks = [
        ("git", ["git", "--version"]),
        ("node", ["node", "--version"]),
        ("python", [sys.executable, "--version"]),
    ]
    for package_manager in package_managers or []:
        if package_manager in {"npm", "pnpm", "yarn", "bun"}:
            checks.append((package_manager, [package_manager, "--version"]))
    results = [safe_tool_version(name, command) for name, command in checks]
    if "codex" in config.provider_chain:
        scope_ok, scope_detail = provider_command_scope_check(config.codex_command, config, "Codex")
        if scope_ok:
            results.append(safe_tool_version("codex", [config.codex_command, "--version"], env=provider_process_env(config)))
        else:
            results.append(scoped_tool_version_failure("codex", [config.codex_command, "--version"], scope_detail))
    return results


def public_tool_version_command(command: list[str]) -> str:
    return " ".join(public_tool_version_command_part(part) for part in command)


def public_tool_version_command_part(part: str) -> str:
    text = str(part)
    if not text:
        return text
    posix_path = PurePosixPath(text)
    if posix_path.is_absolute():
        return posix_path.name or "[path]"
    windows_path = PureWindowsPath(text)
    if windows_path.is_absolute() or text.startswith("\\"):
        return windows_path.name or "[path]"
    return text


def scoped_tool_version_failure(name: str, command: list[str], detail: str) -> dict:
    return {
        "name": name,
        "command": public_tool_version_command(command),
        "available": False,
        "exitCode": 127,
        "output": str(detail)[:200],
    }


def safe_tool_version(name: str, command: list[str], *, env: dict[str, str] | None = None) -> dict:
    command_text = public_tool_version_command(command)
    run_kwargs = {}
    if env is not None:
        run_kwargs["env"] = env
    try:
        with tempfile.TemporaryFile("w+b") as stdout, tempfile.TemporaryFile("w+b") as stderr:
            completed = subprocess.run(
                command,
                check=False,
                stdout=stdout,
                stderr=stderr,
                timeout=5,
                **run_kwargs,
            )
            output = bounded_tool_output(stdout, stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": name,
            "command": command_text,
            "available": False,
            "exitCode": 127,
            "output": str(exc)[:200],
        }
    return {
        "name": name,
        "command": command_text,
        "available": completed.returncode == 0,
        "exitCode": completed.returncode,
        "output": output[:200],
    }


def bounded_tool_output(stdout, stderr, max_bytes: int = 16 * 1024) -> str:
    parts = []
    for handle in (stdout, stderr):
        handle.seek(0)
        data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[:max_bytes]
        text = data.decode("utf-8", errors="replace").strip()
        if text:
            parts.append(text)
    return " ".join(parts)



def iter_dockerfiles(checkout_dir: Path):
    root_dockerfile = checkout_dir / "Dockerfile"
    yielded = 0
    seen = set()
    for path in iter_dockerfile_candidates(checkout_dir):
        if path in seen:
            continue
        seen.add(path)
        yielded += 1
        yield path
        if yielded >= _DOCKERFILE_SCAN_MAX_FILES:
            return


def iter_dockerfile_candidates(checkout_dir: Path):
    root_dockerfile = checkout_dir / "Dockerfile"
    if repository_regular_file(root_dockerfile):
        yield root_dockerfile
    stack = [checkout_dir]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git":
                continue
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if dockerfile_directory_scan_allowed(path, checkout_dir):
                        stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if path == root_dockerfile or not dockerfile_name_matches(path):
                continue
            if dockerfile_scan_allowed(path, checkout_dir):
                yield path


def dockerfile_name_matches(path: Path) -> bool:
    name = path.name
    return name == "Dockerfile" or name.endswith(".Dockerfile")


def dockerfile_directory_scan_allowed(path: Path, checkout_dir: Path) -> bool:
    try:
        relative = path.relative_to(checkout_dir)
    except ValueError:
        return False
    parts = [part.lower() for part in relative.parts]
    return not any(part in _DOCKERFILE_SKIP_DIRS for part in parts)


def dockerfile_scan_allowed(path: Path, checkout_dir: Path) -> bool:
    try:
        relative = path.relative_to(checkout_dir)
    except ValueError:
        return False
    parts = [part.lower() for part in relative.parts]
    return not any(part in _DOCKERFILE_SKIP_DIRS for part in parts[:-1])

__all__ = [name for name in globals() if name == "__version__" or not (name.startswith("__") and name.endswith("__"))]
